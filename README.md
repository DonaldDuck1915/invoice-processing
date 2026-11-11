# Invoice AI — PDF invoice to explainable AP decision

A working prototype that takes one vendor invoice PDF (digital or scanned) and produces **APPROVED / REJECTED / MANUAL REVIEW**. The decision comes with its reasons, and every step in between is visible: extraction with confidence, validation, PO matching with variances, duplicate detection and an audit trail. It also includes a simple human-review workflow.

```
frontend/  React + TypeScript + Vite + Tailwind  → deploy on Vercel
backend/   FastAPI + SQLAlchemy (SQLite locally, Postgres on Render) + Tesseract OCR + Claude (optional)
render.yaml  Render blueprint (API as Docker + Postgres)
```

## How a decision is made

```
PDF ─► ingest (magic bytes, size, pages, SHA-256)
    ─► extract: text layer or OCR (Tesseract, deskew + denoise) per page
               rule parser (labels, regex, column-aware tables)   ┐ merged; AI values must quote the
               + Claude extraction (optional)                      ┘ document and the quote is verified
    ─► validate: mandatory fields, line math, tax, subtotal, grand total, dates, vendor, confidence, currency
    ─► match PO: explicit number → OCR-tolerant number → ranked candidates (weighted, transparent)
               line alignment (Hungarian) → variances vs REMAINING PO balance (ledger)
    ─► duplicates: identical file / exact rule / weighted similarity + content fingerprint
    ─► decide: every rule evaluated; REJECT > MANUAL_REVIEW > APPROVED;
               a reject that depends on an unreliable reading is downgraded to review
    ─► ledger: approved invoices consume PO quantity/value (one row per PO line per invoice)
    ─► AI summary (optional, labelled "AI and can make mistakes. Please double-check responses.")
```

**Where the AI is (and isn't) used.** Claude reads fields from the PDF, judges what descriptions *mean* (one signal worth 30 of the 100 PO-matching points), and writes the plain-language summary. Code does all arithmetic, tolerances, PO selection, duplicate detection and the final decision. The AI never sees amounts when scoring meaning. Without an API key the system runs rules-only and becomes stricter: an inferred PO match then always needs confirmation.

All money is Python `Decimal` and is stored and transmitted as exact strings. Rules live in `backend/app/rules.yaml`, can be overridden on the **Rules & data** page, and are snapshotted into every run.

## Edge cases (all in `backend/samples/`, all covered by tests)

| # | Scenario | Behaviour |
|---|---|---|
| EC1 | PO for 100 chairs, invoiced 60 + 40, then +5 | Both approved; each compared to the *remaining* quantity; ledger can't count an invoice twice (unique row per line, reprocessing updates in place). The +5 invoice is **rejected** (cumulative 105 > 100). |
| EC2 | Same invoice re-sent: new filename, other layout, scanned, invoice no. misread | OCR-aware invoice-number similarity + date + amount + PO + lines + fingerprint → **probable duplicate → review**. The identical file re-uploaded → **confirmed → rejected**. A monthly invoice with the same amount but the next sequence number stays **new**. |
| EC3 | Unit price 1,080 vs PO 1,000, tolerance 5% | Line 1 variance +8.00% shown with expected/actual/abs/%/tolerance → **review**. 3% version → approved. |
| EC4 | No PO number; vendor has PO-101/102/103 | Vendor by GSTIN, candidates ranked on 6 signals with evidence. "Office supplies ₹50,000" is ambiguous → **review** with one-click confirmation. Clear line items → auto-selected when AI is on. |

Also included: cancelled PO (reject), PO belonging to another vendor (reject), blocked or pending vendor (flagged review), currency mismatch (review), missing invoice number plus line-math error (review), low-quality scan (review), corrupt and non-PDF files (clean error).

## Run locally

```bash
cd backend
python -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt          # also needs: apt install tesseract-ocr
python scripts/generate_samples.py           # demo PDFs → backend/samples/
cp .env.example .env                         # optionally add ANTHROPIC_API_KEY
uvicorn app.main:app --reload --port 8000
pytest -q                                    # 36 tests
```

```bash
cd frontend
npm install
echo "VITE_API_URL=http://localhost:8000" > .env.local
npm run dev                                  # http://localhost:5173  (reviewer / review123)
```

## Deploy (Render + Vercel)

1. Push this repo to GitHub.
2. **Render** → New → Blueprint → pick the repo (uses `render.yaml`). It creates the Postgres DB and the Docker API service (Tesseract included). When prompted, set:
   - `ANTHROPIC_API_KEY` (optional; enables AI)
   - `REVIEWER_PASSWORD` (choose a strong one)
   - `ALLOWED_ORIGINS` = your Vercel URL, e.g. `https://invoice-ai.vercel.app`
3. **Vercel** → New Project → root directory `frontend` (framework: Vite) → env var `VITE_API_URL` = the Render API URL.
4. Open the Vercel URL and sign in as `reviewer`.

Notes: Render's free web service sleeps when idle (the first request can take ~1 minute), and its free Postgres expires after 30 days. Uploaded PDFs are stored in the database, so no disk is needed.

## Using your own data

**Rules & data → Import** accepts JSON (example prefilled on the page):

```json
{"vendors": [{"code": "V100", "name": "...", "tax_id": "27ABCDE1234F1Z5", "country": "IN", "status": "APPROVED", "default_currency": "INR"}],
 "purchase_orders": [{"po_number": "PO-5001", "vendor": "V100", "po_date": "2026-10-01", "currency": "INR",
   "lines": [{"item_code": "CH-01", "description": "Office chair", "uom": "NOS", "quantity": "10", "unit_price": "4500", "tax_rate": "18"}]}]}
```

Set your own company's tax ID in `rules.yaml → company.tax_ids` so it is never mistaken for the vendor's.

## Known limitations

- The rule-based parser is tuned on label/table conventions common in Indian GST, EU and US invoices. Unusual layouts rely on the AI extractor or on reviewer correction.
- One invoice at a time, a single reviewer account, no payments or ERP integration (deliberately out of scope).
- Currencies are compared, never converted; supported: INR, EUR, USD.
