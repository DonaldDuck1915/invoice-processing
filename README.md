# Invoice AI — PDF invoice to explainable AP decision

A working prototype that takes one vendor invoice PDF (digital or scanned) and produces **APPROVED / REJECTED / MANUAL REVIEW**. The decision comes with its reasons, and every step in between is visible: extraction with confidence, validation, PO matching with variances, duplicate detection and an audit trail. It also includes a simple human-review workflow.

```
frontend/  React + TypeScript + Vite + Tailwind  → deploy on Vercel
backend/   FastAPI + SQLAlchemy (SQLite locally, Postgres on Render) + Tesseract OCR + Gemini or Claude (optional)
render.yaml  Render blueprint (API as Docker + Postgres)
```

## How a decision is made

```
PDF ─► ingest (magic bytes, size, pages, SHA-256)
    ─► extract: text layer or OCR (Tesseract, deskew + denoise) per page
               rule parser (labels, regex, column-aware tables)   ┐ merged; AI values must quote the
               + AI extraction (optional)                          ┘ document and the quote is verified
    ─► validate: mandatory fields, line math, tax, subtotal, grand total, dates, vendor, confidence, currency
    ─► match PO: explicit number → OCR-tolerant number → ranked candidates (weighted, transparent)
               line alignment (Hungarian) → variances vs REMAINING PO balance (ledger)
    ─► duplicates: identical file / exact rule / weighted similarity + content fingerprint
    ─► decide: every rule evaluated; REJECT > MANUAL_REVIEW > APPROVED;
               a reject that depends on an unreliable reading is downgraded to review
    ─► ledger: approved invoices consume PO quantity/value (one row per PO line per invoice)
    ─► AI summary (optional, labelled "AI and can make mistakes. Please double-check responses.")
```

**Where the AI is (and isn't) used.** The AI (Google Gemini or Anthropic Claude, whichever key you set) reads fields from the PDF, judges what descriptions *mean* (one signal worth 30 of the 100 PO-matching points), writes the plain-language summary, and recommends the reviewer's next step (confirm a PO, ask the vendor, correct fields, escalate ...) with a draft vendor e-mail. Recommendations are advisory: they are checked in code (fixed action list, suggested PO must be a real candidate, never "approve" for a rejected invoice) and nothing is applied without a reviewer click, which is logged. Code does all arithmetic, tolerances, PO selection, duplicate detection and the final decision. The AI never sees amounts when scoring meaning. Without an API key the system runs rules-only and becomes stricter: an inferred PO match then always needs confirmation.

All money is Python `Decimal` and is stored and transmitted as exact strings. Rules live in `backend/app/rules.yaml`, can be overridden on the **Rules & data** page, and are snapshotted into every run.

## Edge cases (all in `backend/samples/`, all covered by tests)

| # | Scenario | Behaviour |
|---|---|---|
| EC1 | PO for 100 chairs, invoiced 60 + 40, then +5 | Both approved; each compared to the *remaining* quantity; ledger can't count an invoice twice (unique row per line, reprocessing updates in place). The +5 invoice is **rejected** (cumulative 105 > 100). |
| EC2 | Same invoice re-sent: new filename, other layout, scanned, invoice no. misread | OCR-aware invoice-number similarity + date + amount + PO + lines + fingerprint → **probable duplicate → review**. The identical file re-uploaded → **confirmed → rejected**. A monthly invoice with the same amount but the next sequence number stays **new**. |
| EC3 | Unit price 1,080 vs PO 1,000, tolerance 5% | Line 1 variance +8.00% shown with expected/actual/abs/%/tolerance → **review**. 3% version → approved. |
| EC4 | No PO number; vendor has PO-101/102/103 | Vendor by GSTIN, candidates ranked on 6 signals with evidence. "Office supplies ₹50,000" is ambiguous → **review** with one-click confirmation. Clear line items → auto-selected when AI is on. |

| EC5 | Prices include GST ("All prices are inclusive of GST", 2,950/stand vs PO 2,500 net) | Detected from the document; tax is checked as *contained* in the totals (gross − gross/1.18), and lines are converted to net before PO matching and the ledger, so it is **approved** instead of looking like an 18% price variance. |

Also included: cancelled PO (reject), PO belonging to another vendor (reject), blocked or pending vendor (flagged review), currency mismatch (review), missing invoice number plus line-math error (review), low-quality scan (review), corrupt and non-PDF files (clean error).

## Live run view, intake and safeguards

- **Live run view** (Process invoices page, and an invoice that is still processing): the pipeline commits after every stage, and the page polls `/invoices/{id}/progress`, so each stage turns green with its own result (fields extracted, checks failed, PO chosen, duplicate score, decision) as it finishes.
- **Batch upload**: drop several PDFs; they are processed in upload order (so split invoices and duplicates are evaluated in sequence) and each has its own live row.
- **E-mail intake** (optional): set `IMAP_HOST`, `IMAP_USER`, `IMAP_PASSWORD` (an app password) and the API polls the AP mailbox, ingests PDF attachments exactly like uploads and records sender + subject in the audit trail. "Check inbox now" triggers a poll.
- **POs from a spreadsheet**: Rules & data → *Import purchase orders from a spreadsheet* takes a CSV (one row per PO line; template downloadable; common headers like "PO No", "Qty", "GST %" are recognised).
- **One invoice at a time through the ledger**: matching → duplicate check → decision → PO ledger is serialised (process lock + Postgres advisory lock), so two simultaneous uploads of the same invoice cannot both be approved and two invoices cannot both consume the last PO balance.
- **Reviewer decisions are kept**: a re-run stores the new system decision next to the reviewer's final decision instead of overwriting it (`HUMAN_DECISION_KEPT` in the audit trail).
- **Human approval re-checks the PO balance**: approving an invoice that would over-bill the PO *now* (e.g. another invoice consumed it while this one waited in review) is refused with the exact lines (HTTP 409).
- **Interrupted runs resume**: invoices left mid-processing by a restart (e.g. the free host sleeping) are re-queued at startup.
- **Hardening**: rule overrides are type- and range-checked (policies must be MANUAL_REVIEW/REJECT, weights must add up to 1); repeated failed sign-ins are throttled; a hosted (Postgres) instance refuses to start with the demo `AUTH_SECRET`; `ALLOW_DEMO_RESET=false` disables the reset button.

## Run locally

```bash
cd backend
python -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt          # also needs Tesseract OCR (below)
python scripts/generate_samples.py           # demo PDFs → backend/samples/
cp .env.example .env                         # optionally add GEMINI_API_KEY (or ANTHROPIC_API_KEY)
uvicorn app.main:app --reload --port 8000
pytest -q                                    # 82 tests (scanned-PDF ones skip without Tesseract)
```

Tesseract (needed for scanned PDFs): Linux `apt install tesseract-ocr`, macOS `brew install tesseract`, Windows `winget install UB-Mannheim.TesseractOCR` (the default install folder is found automatically; otherwise set `TESSERACT_CMD`). Without it, scanned invoices go to manual review with the reason "OCR is not available".

```bash
cd frontend
npm install
echo "VITE_API_URL=http://localhost:8000" > .env.local
npm run dev                                  # http://localhost:5173  (reviewer / review123)
```

## Deploy (Render + Vercel)

1. Push this repo to GitHub.
2. **Render** → New → Blueprint → pick the repo (uses `render.yaml`). It creates the Postgres DB and the Docker API service (Tesseract included). When prompted, set:
   - `GEMINI_API_KEY` (optional; enables AI. `ANTHROPIC_API_KEY` works too)
   - `REVIEWER_PASSWORD` (choose a strong one)
   - `ALLOWED_ORIGINS` = your Vercel URL, e.g. `https://invoice-ai.vercel.app`
3. **Vercel** → New Project → root directory `frontend` (framework: Vite) → env var `VITE_API_URL` = the Render API URL.
4. Open the Vercel URL and sign in as `reviewer`.

Notes: Render's free web service sleeps when idle (the first request can take ~1 minute), and its free Postgres expires after 30 days. Uploaded PDFs are stored in the database, so no disk is needed.

## AI provider (Gemini)

Set `GEMINI_API_KEY` (from [Google AI Studio](https://aistudio.google.com/apikey)) in `backend/.env` locally, or as a secret on Render. `ANTHROPIC_API_KEY` works as an alternative; with both set, Gemini is used unless `LLM_PROVIDER=anthropic`.

| Job | Setting | Default | Why |
|---|---|---|---|
| Read the invoice PDF (extraction) | `GEMINI_MODEL` | `gemini-3.6-flash` | Accurate on PDFs; ~4-8 s per invoice with low thinking |
| Summary + recommendation, description meaning | `GEMINI_FAST_MODEL` | `gemini-3.5-flash-lite` | Short reasoning over facts; ~2 s and the cheapest tier |
| Fallback when a model is overloaded (503/504) | `GEMINI_FALLBACK_MODEL` | the other model | One retry on a different model instead of dropping to rules-only |
| Thinking effort | `GEMINI_THINKING` | `auto` (low) | Higher levels add latency without changing the rule-based outcome |
| Give up on a slow call | `LLM_TIMEOUT_SECONDS` | `60` | The invoice then continues rules-only |

A model that times out or is overloaded is skipped for the rest of that run, so an invoice never waits twice for the same slow model. Every run lists its AI calls (purpose, model, latency, tokens, errors). If the AI fails (bad key, wrong model, quota), the invoice is still processed rules-only and the reason is shown.

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
- A single reviewer account, no payments or ERP integration (deliberately out of scope).
- Tax-inclusive detection relies on the document saying so ("inclusive of GST", "VAT included", ...); a reviewer can still correct lines if a vendor prints gross prices silently.
- Processing runs in the API process (threads), not a separate job queue; interrupted runs are re-queued on restart.
- No database migrations: the schema is created on startup, so a schema change on an existing Postgres database needs a reset or a manual migration.
- Currencies are compared, never converted; supported: INR, EUR, USD.
