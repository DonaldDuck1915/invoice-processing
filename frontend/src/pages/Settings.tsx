import { Download, FileSpreadsheet, Mail } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { api } from "../api";
import { Card, ErrorBox, Note, Spinner } from "../components/ui";

const NUMERIC: [string, string, string][] = [
  ["tolerances", "quantity_pct", "Quantity tolerance (%)"],
  ["tolerances", "unit_price_pct", "Unit price tolerance (%)"],
  ["tolerances", "total_amount_pct", "Total amount tolerance (%)"],
  ["tolerances", "rounding_abs", "Line arithmetic rounding (abs)"],
  ["tolerances", "tax_abs_header", "Tax rounding - header (abs)"],
  ["tolerances", "tax_abs_per_line", "Tax rounding - per line (abs)"],
  ["extraction", "min_confidence", "Minimum field confidence (0-1)"],
  ["matching", "select_min_score", "PO auto-select minimum score"],
  ["matching", "select_margin", "PO auto-select margin (points)"],
  ["duplicates", "probable_threshold", "Probable-duplicate threshold (0-1)"],
];
const POLICIES: [string, string, string[]][] = [
  ["unapproved_vendor", "Blocked / pending vendor", ["MANUAL_REVIEW", "REJECT"]],
  ["currency_mismatch", "Currency mismatch", ["MANUAL_REVIEW", "REJECT"]],
  ["qty_exceeds_remaining", "Quantity exceeds remaining PO", ["REJECT", "MANUAL_REVIEW"]],
  ["cancelled_po", "Cancelled PO", ["REJECT", "MANUAL_REVIEW"]],
  ["closed_po_no_balance", "Closed PO with no balance", ["REJECT", "MANUAL_REVIEW"]],
];

const EXAMPLE = `{
  "vendors": [
    {"code": "V100", "name": "Your Vendor Pvt Ltd", "tax_id": "27ABCDE1234F1Z5", "country": "IN",
     "status": "APPROVED", "default_currency": "INR"}
  ],
  "purchase_orders": [
    {"po_number": "PO-5001", "vendor": "V100", "po_date": "2026-10-01", "status": "APPROVED",
     "currency": "INR", "description": "Office chairs",
     "lines": [{"item_code": "CH-01", "description": "Office chair", "uom": "NOS",
                "quantity": "10", "unit_price": "4500", "tax_rate": "18"}]}
  ]
}`;

export default function Settings() {
  const [rules, setRules] = useState<any>(null);
  const [draft, setDraft] = useState<any>(null);
  const [msg, setMsg] = useState<string | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [json, setJson] = useState(EXAMPLE);
  const [replace, setReplace] = useState(false);
  const [csv, setCsv] = useState<File | null>(null);
  const [email, setEmail] = useState<any>(null);
  const csvInput = useRef<HTMLInputElement>(null);

  const load = () => api.get("/config/rules").then((r) => { setRules(r); setDraft(structuredClone(r.effective)); }).catch(setError);
  useEffect(() => { load(); api.get("/admin/email").then(setEmail).catch(() => setEmail({ enabled: false })); }, []);
  if (!draft) return error ? <ErrorBox error={error} /> : <Spinner label="Loading rules..." />;

  function diff(base: any, cur: any): any {
    const out: any = {};
    for (const k of Object.keys(cur)) {
      if (cur[k] && typeof cur[k] === "object" && !Array.isArray(cur[k])) { const d = diff(base[k], cur[k]); if (Object.keys(d).length) out[k] = d; }
      else if (JSON.stringify(base?.[k]) !== JSON.stringify(cur[k])) out[k] = cur[k];
    }
    return out;
  }

  async function save() {
    setError(null); setMsg(null);
    try { await api.put("/config/rules", { overrides: diff(rules.defaults, draft) }); await load(); setMsg("Rules saved. New runs use them; past runs keep the snapshot they used."); } catch (e) { setError(e); }
  }
  async function reset() { setDraft(structuredClone(rules.defaults)); }
  async function doImport() {
    setError(null); setMsg(null);
    try { const body = JSON.parse(json); const r = await api.post("/admin/import", { ...body, replace }); setMsg(`Imported ${r.vendors_added} vendor(s) and ${r.purchase_orders_added} PO(s).`); }
    catch (e) { setError(e instanceof SyntaxError ? new Error("Invalid JSON: " + e.message) : e); }
  }
  async function importCsv() {
    if (!csv) return;
    setError(null); setMsg(null);
    try {
      const r = await api.upload(`/admin/import-csv?replace=${replace}`, csv, csv.name);
      setMsg(`Read ${r.rows_read} row(s) / ${r.pos_in_file} PO(s): imported ${r.purchase_orders_added} PO(s) and ${r.vendors_added} new vendor(s).`);
      setCsv(null); if (csvInput.current) csvInput.current.value = "";
    } catch (e) { setError(e); }
  }
  async function downloadTemplate() {
    const blob = await api.blob("/admin/import-csv/template");
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = "purchase-orders-template.csv";
    a.click();
  }
  async function pollInbox() {
    setError(null); setMsg(null);
    try { const r = await api.post("/admin/email/poll"); setMsg(`Inbox checked: ${r.messages} new message(s), ${r.invoice_ids.length} invoice(s) queued.${r.errors.length ? " Skipped: " + r.errors.join("; ") : ""}`); setEmail(await api.get("/admin/email")); }
    catch (e) { setError(e); }
  }
  async function resetDemo() {
    if (!confirm("Delete ALL invoices, POs and vendors and reload the demo dataset?")) return;
    try { await api.post("/admin/reset-demo"); setMsg("Demo data reset."); await load(); } catch (e) { setError(e); }
  }

  return (
    <div className="space-y-6 max-w-4xl">
      <div>
        <h1 className="text-xl font-semibold text-slate-900">Rules & data</h1>
        <p className="text-sm text-slate-500">Business rules are configurable; every processing run stores a snapshot of the rules it used.</p>
      </div>
      {msg && <Note>{msg}</Note>}
      <ErrorBox error={error} />
      <Card title="Tolerances & thresholds" actions={<div className="flex gap-2"><button className="btn-secondary" onClick={reset}>Defaults</button><button className="btn-primary" onClick={save}>Save rules</button></div>}>
        <div className="grid sm:grid-cols-2 gap-4">
          {NUMERIC.map(([g, k, label]) => (
            <div key={k}><label className="label">{label}</label>
              <input className="input" type="number" step="any" min="0" value={draft[g][k]} onChange={(e) => setDraft({ ...draft, [g]: { ...draft[g], [k]: e.target.value === "" ? 0 : Number(e.target.value) } })} /></div>
          ))}
        </div>
        <h4 className="mt-6 mb-3 text-xs font-semibold uppercase text-slate-500">Policies</h4>
        <div className="grid sm:grid-cols-2 gap-4">
          {POLICIES.map(([k, label, opts]) => (
            <div key={k}><label className="label">{label}</label>
              <select className="input" value={draft.policy[k]} onChange={(e) => setDraft({ ...draft, policy: { ...draft.policy, [k]: e.target.value } })}>
                {opts.map((o) => <option key={o} value={o}>{o.replace("_", " ").toLowerCase()}</option>)}
              </select></div>
          ))}
          <div><label className="label">Inferred PO match without AI signal</label>
            <select className="input" value={String(draft.matching.require_semantic_for_auto_select)} onChange={(e) => setDraft({ ...draft, matching: { ...draft.matching, require_semantic_for_auto_select: e.target.value === "true" } })}>
              <option value="true">always needs human confirmation</option><option value="false">may auto-select on rules alone</option>
            </select></div>
        </div>
        <p className="mt-4 text-xs text-slate-500">Company (buyer) tax IDs excluded from vendor detection: {draft.company.tax_ids.join(", ")}</p>
      </Card>

      <Card title="Import purchase orders from a spreadsheet" subtitle="CSV with one row per PO line (Excel / Google Sheets: save or download as CSV). Existing PO numbers are skipped."
        actions={<button className="btn-secondary" onClick={downloadTemplate}><Download className="h-4 w-4" />Template</button>}>
        <div className="flex flex-wrap items-center gap-4">
          <input ref={csvInput} type="file" accept=".csv,text/csv" className="text-sm" onChange={(e) => setCsv(e.target.files?.[0] ?? null)} />
          <label className="flex items-center gap-2 text-sm"><input type="checkbox" checked={replace} onChange={(e) => setReplace(e.target.checked)} />Replace all existing data</label>
          <button className="btn-primary" disabled={!csv} onClick={importCsv}><FileSpreadsheet className="h-4 w-4" />Import CSV</button>
        </div>
        <p className="mt-3 text-xs text-slate-500">Columns: po_number, vendor_code, po_date, description, quantity, unit_price (required) · vendor_name, vendor_tax_id, vendor_status, currency, po_status, item_code, uom, tax_rate (optional). Common headers like "PO No", "Qty", "GST %" are recognised.</p>
      </Card>

      <Card title="E-mail intake" subtitle="Invoices sent to the AP mailbox are picked up automatically (PDF attachments).">
        {!email ? <Spinner label="Checking..." /> : email.enabled ? (
          <div className="flex flex-wrap items-center justify-between gap-4 text-sm">
            <div className="space-y-0.5 text-slate-600">
              <div>Mailbox <b className="text-slate-800">{email.mailbox}</b> · folder {email.folder} · every {email.poll_seconds}s</div>
              <div className="text-xs">Last check: {email.last_poll ? new Date(email.last_poll).toLocaleString() : "not yet"} · {email.invoices_created} invoice(s) received{email.last_error && <span className="text-rose-600"> · last error: {email.last_error}</span>}</div>
            </div>
            <button className="btn-secondary" onClick={pollInbox}><Mail className="h-4 w-4" />Check inbox now</button>
          </div>
        ) : (
          <p className="text-sm text-slate-600">Not configured. Set <code className="text-xs">IMAP_HOST</code>, <code className="text-xs">IMAP_USER</code> and <code className="text-xs">IMAP_PASSWORD</code> (an app password) on the API to enable it.</p>
        )}
      </Card>

      <Card title="Import your vendors & purchase orders (JSON)" subtitle="Paste JSON in the shape below. Existing codes / PO numbers are skipped.">
        <textarea className="input font-mono text-xs h-64" value={json} onChange={(e) => setJson(e.target.value)} />
        <div className="mt-3 flex items-center gap-4">
          <label className="flex items-center gap-2 text-sm"><input type="checkbox" checked={replace} onChange={(e) => setReplace(e.target.checked)} />Replace all existing data (deletes invoices too)</label>
          <button className="btn-primary" onClick={doImport}>Import</button>
        </div>
      </Card>

      <Card title="Demo data">
        <div className="flex items-center justify-between gap-4">
          <p className="text-sm text-slate-600">Reset the database to the seeded demo dataset (9 vendors, 15 POs, one historical invoice).</p>
          <button className="btn-danger" onClick={resetDemo}>Reset demo data</button>
        </div>
      </Card>
    </div>
  );
}
