# Invoice AI — PDF invoice to explainable AP decision

A working prototype that takes one vendor invoice PDF (digital or scanned) and produces **APPROVED / REJECTED / MANUAL REVIEW**. The decision comes with its reasons, and every step in between is visible: extraction with confidence, validation, PO matching with variances, duplicate detection and an audit trail. It also includes a simple human-review workflow.

```
frontend/  React + TypeScript + Vite + Tailwind  → deploy on Vercel
backend/   FastAPI + SQLAlchemy (SQLite locally, Postgres on Render) + Tesseract OCR + OpenAI (optional)
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
    ─► AP controls: approval limit (manager above it), fraud signals, GST e-invoice IRN/QR, TDS;
               credit / debit notes are linked to their original invoice instead of PO-matched
    ─► decide: every rule evaluated; REJECT > MANUAL_REVIEW > APPROVED;
               a reject that depends on an unreliable reading is downgraded to review
    ─► ledger: approved invoices consume PO quantity/value (one row per PO line per invoice)
    ─► AI summary (optional, labelled "AI and can make mistakes. Please double-check responses.")
```

**AI reader only when needed.** A clean digital invoice skips the AI reader: if the rule parser read every critical field (vendor tax ID that validates, number, date, total, currency) with high confidence and every sum reconciles (each line, subtotal, tax, grand total), the arithmetic itself confirms the reading. Scans, photos, anything that does not reconcile, and documents that mention bank details, an IRN or a credit/debit note the rules did not read still go to the AI. On the 31 sample invoices this skipped 21, cut total processing from about 390 s to about 230 s, and changed no decision. The flag `AI not needed` marks such invoices; *Re-run* with re-extract always asks the AI, and `AI_EXTRACTION=always` turns the shortcut off.

**Where the AI is (and isn't) used.** The AI (OpenAI, `gpt-6-luna` by default) reads fields from the PDF, judges what descriptions *mean* (one signal worth 30 of the 100 PO-matching points), writes the plain-language summary, and recommends the reviewer's next step (confirm a PO, ask the vendor, correct fields, escalate ...) with a draft vendor e-mail. Recommendations are advisory: they are checked in code (fixed action list, suggested PO must be a real candidate, never "approve" for a rejected invoice) and nothing is applied without a reviewer click, which is logged. Code does all arithmetic, tolerances, PO selection, duplicate detection and the final decision. The AI never sees amounts when scoring meaning. Without an API key the system runs rules-only and becomes stricter: an inferred PO match then always needs confirmation.

All money is Python `Decimal` and is stored and transmitted as exact strings. Rules live in `backend/app/rules.yaml`, can be overridden on the **Rules & data** page, and are snapshotted into every run.

## Edge cases (all in `backend/samples/`, all covered by tests)

| # | Scenario | Behaviour |
|---|---|---|
| EC1 | PO for 100 chairs, invoiced 60 + 40, then +5 | Both approved; each compared to the *remaining* quantity; ledger can't count an invoice twice (unique row per line, reprocessing updates in place). The +5 invoice is **rejected** (cumulative 105 > 100). |
| EC2 | Same invoice re-sent: new filename, other layout, scanned, invoice no. misread | OCR-aware invoice-number similarity + date + amount + PO + lines + fingerprint → **probable duplicate → review**. The identical file re-uploaded → **confirmed → rejected**. A monthly invoice with the same amount but the next sequence number stays **new**. |
| EC3 | Unit price 1,080 vs PO 1,000, tolerance 5% | Line 1 variance +8.00% shown with expected/actual/abs/%/tolerance → **review**. 3% version → approved. |
| EC4 | No PO number; vendor has PO-101/102/103 | Vendor by GSTIN, candidates ranked on 6 signals with evidence. "Office supplies ₹50,000" is ambiguous → **review** with one-click confirmation. Clear line items → auto-selected when AI is on. |

| EC5 | Prices include GST ("All prices are inclusive of GST", 2,950/stand vs PO 2,500 net) | Detected from the document; tax is checked as *contained* in the totals (gross − gross/1.18), and lines are converted to net before PO matching and the ledger, so it is **approved** instead of looking like an 18% price variance. |

## AP controls (samples 24-33)

| Control | How it works | Sample → result |
|---|---|---|
| **Approval limits** | `approval.auto_approve_limit` per currency (₹5,00,000 / €15,000 / $15,000 gross). Above it the invoice goes to review and **only the `manager` account can approve it** (the API refuses a reviewer's approval with 403; a reviewer can still reject). Within 5% below the limit → *just under limit* flag; a second just-under invoice from the same vendor within 30 days → review as a **possible split**. Credit notes are exempt. | 26 → review, manager only · 27 → approved + flag · 28 → review (split) |
| **Credit notes** | Detected from the title ("CREDIT NOTE"); the "Against Invoice No" reference links it to the original (same vendor). It must be approved, same currency, and may not credit more than is left on it or return more units than were billed. Returned goods (same unit price) go back to the PO line as negative ledger rows; price/discount lines reduce value only. The original's page shows **net owed after notes**. | 24 (after 03) → approved; invoice 03 nets to ₹3,24,500 and PO-2001 shows 5 chairs free again |
| **Debit notes** | Linked the same way; value lines add to the PO's consumed value (checked against the PO value tolerance), extra-quantity lines must fit the remaining PO quantity. Default policy: buyer confirms (review). | 25 (after 01) → review |
| **Bank details** | Bank account / IBAN and IFSC / SWIFT are extracted and compared with the vendor master (or the vendor's earlier invoices). A change → review with "call the vendor on a known number". Account numbers are masked everywhere. Only a manager can change bank details in the vendor master; the change is audited. | 29 → review; 30 → bank matches |
| **Unusual amount** | More than 3x the vendor's median invoice (once 3+ invoices exist) → review. | unit-tested |
| **Invoice before PO** | Invoice date earlier than the matched PO's date (retro PO) → review. | 30 → review |
| **Month-end arrival** | Received in the last 2 days of a month → flag (configurable to review). | unit-tested |
| **GST e-invoice** | For vendors flagged "must e-invoice": IRN must be present and a 64-character hex reference; the signed QR code is decoded (OpenCV) and its seller/buyer GSTIN, document number, type, value and IRN are compared with what is printed. Missing → review; any contradiction → review + high-risk flag (document altered after the IRN was issued). The QR's signature is **not** verified (needs the IRP public key). Any invoice that prints an IRN/QR is checked even if not required. | 31 → approved · 32 → review · 33 → review (QR value ≠ printed total) |
| **TDS** | Vendor master holds the TDS section (194C / 194J / 194I / 194Q); rate and single/annual thresholds come from `rules.yaml`. Base = value excluding GST; financial-year aggregate from approved invoices; 194Q taxes only the part above the annual threshold; rounded to the rupee. The decision's next action says what to pay the vendor and what TDS to deposit; credit notes reverse TDS proportionally. | 31 → 2% of ₹1,80,000 = ₹3,600 withheld, pay ₹2,08,800 |

Every control outcome is a policy in `rules.yaml` (`MANUAL_REVIEW` / `REJECT`, and `FLAG` for the soft ones), shown on the invoice's **Controls & payment** tab and in the live run view (*AP controls* stage). Sign in as `manager` / `manage123` (set `MANAGER_PASSWORD`) to approve above the limit. TDS rates and thresholds are configurable defaults - confirm them with your tax team.

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

## Guest mode (for evaluators)

The sign-in page has **Continue as guest**. Each guest gets a **private sandbox**: a fresh copy of the demo vendors and purchase orders, plus the sample invoices on the *Process invoices* page. Their uploads, reviews, rule changes and profile are invisible to everyone else and never touch the real data, so several evaluators can test at the same time without seeing each other's duplicates or used-up PO balances.

- A switch at the bottom of the sidebar changes between the **reviewer** and **manager** view, so manager-only features (approval limits, vendor bank details) can be tried too.
- *Rules & data → Reset demo* resets only the guest's own sandbox.
- Hosted on Postgres, each sandbox is its own schema (`guest_<id>`); locally on SQLite it is a file in `GUEST_DATA_DIR`. A sandbox unused for `GUEST_TTL_HOURS` (24) is deleted.
- Limits: 5 new sandboxes per client per hour, 100 alive at once, 150 invoices each (guests use the configured OpenAI key, about $0.0008 per invoice). E-mail intake is off for guests. Set `GUEST_MODE=false` to remove the button.

## Run locally

```bash
cd backend
python -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt          # also needs Tesseract OCR (below)
python scripts/generate_samples.py           # demo PDFs → backend/samples/
cp .env.example .env                         # optionally add OPENAI_API_KEY
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
   - `OPENAI_API_KEY` (optional; enables AI - without it the app runs rules-only)
   - `REVIEWER_PASSWORD` (choose a strong one)
   - `ALLOWED_ORIGINS` = your Vercel URL, e.g. `https://invoice-ai.vercel.app`
3. **Vercel** → New Project → root directory `frontend` (framework: Vite) → env var `VITE_API_URL` = the Render API URL.
4. Open the Vercel URL and sign in as `reviewer`.

Notes: Render's free web service sleeps when idle (the first request can take ~1 minute), and its free Postgres expires after 30 days. Uploaded PDFs are stored in the database, so no disk is needed.

## AI provider (OpenAI)

Set `OPENAI_API_KEY` in `backend/.env` locally, or as a secret on Render (get one at https://platform.openai.com/api-keys). Calls use the Responses API with strict JSON-schema output; the PDF is sent inline (`store=false`, nothing is kept on OpenAI's side).

| Setting | Default | Notes |
|---|---|---|
| `OPENAI_MODEL` | `gpt-6-luna` | Cheapest GPT-6 tier; used for every call unless a fast model is set |
| `OPENAI_FAST_MODEL` | (same) | Optional separate model for summary / recommendation / description meaning |
| `OPENAI_FALLBACK_MODEL` | (none) | Tried once when the first model is overloaded or times out |
| `OPENAI_REASONING` | `auto` (low; medium for PDF extraction) | Higher effort costs more output tokens |
| `OPENAI_PDF_DETAIL` | `auto` | `low` renders PDF pages with fewer tokens (cheaper, slightly less robust on scans) |
| `OPENAI_PRICE_*_PER_M` | 0.10 / 0.01 cached / 0.50 | USD per 1M tokens, only used to show cost; update if OpenAI changes prices |

A model that is overloaded or times out (`LLM_TIMEOUT_SECONDS`, default 60) is skipped for the rest of that run and the fallback model, if set, is tried; otherwise the invoice continues rules-only and the reason is shown. Each run records tokens and estimated cost per AI call; the decision report shows the run's total and the dashboard shows the total and average per run.
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
- Two fixed accounts (reviewer, manager) rather than user management; no payments or ERP integration (deliberately out of scope).
- E-invoice QR signatures are not verified against the IRP public key, and the IRN is not checked against the GST portal (no live API access). Amount limits are per currency, not converted.
- Credit/debit notes must cite the original invoice number; a note covering several invoices is not split automatically.
- Tax-inclusive detection relies on the document saying so ("inclusive of GST", "VAT included", ...); a reviewer can still correct lines if a vendor prints gross prices silently.
- Processing runs in the API process (threads), not a separate job queue; interrupted runs are re-queued on restart.
- Schema changes are applied by a small "add missing columns" step at startup (new nullable columns only); renames or type changes would still need a manual migration.
- Currencies are compared, never converted. INR, EUR and USD are built in; a manager can add any other currency (e.g. GBP, AED) by giving it an approval limit on Rules & data. An invoice in a currency with no limit always needs a manager.
