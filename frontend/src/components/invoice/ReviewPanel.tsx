import { CheckCircle2, Plus, Trash2, XCircle } from "lucide-react";
import { useMemo, useState } from "react";
import { api } from "../../api";
import { FIELD_LABELS, dateTime, money } from "../../format";
import { Card, Note, StatusChip } from "../ui";

const EDITABLE = ["vendor_name", "vendor_tax_id", "invoice_number", "invoice_date", "due_date", "po_number", "currency",
  "subtotal", "taxable_amount", "discount", "tax_amount", "shipping", "grand_total"];
const LINE_COLS = ["description", "item_code", "quantity", "uom", "unit_price", "tax_rate", "line_total"];

export default function ReviewPanel({ data, busy, act, id }: { data: any; busy: boolean; act: (fn: () => Promise<any>) => void; id: string }) {
  const run = data.run;
  const ex = run.extraction;
  const inv = data.invoice;
  const flagged = Object.entries(ex.fields).filter(([, f]: any) => f.status === "UNCERTAIN" || f.status === "MISSING")
    .filter(([n]) => EDITABLE.includes(n)).map(([n]) => n);
  const [fields, setFields] = useState<Record<string, string>>(() => Object.fromEntries(EDITABLE.map((n) => [n, ex.fields[n]?.value ?? ""])));
  const [lines, setLines] = useState<any[]>(() => ex.line_items.map((li: any) => Object.fromEntries(LINE_COLS.map((c) => [c, li[c] ?? ""]))));
  const [editLines, setEditLines] = useState(false);
  const [corrReason, setCorrReason] = useState("");
  const [po, setPo] = useState(run.matching?.proposed_po || run.matching?.selected_po || "");
  const [poReason, setPoReason] = useState("");
  const [dupReason, setDupReason] = useState("");
  const [finalReason, setFinalReason] = useState("");

  const changed = useMemo(() => Object.fromEntries(EDITABLE.filter((n) => (ex.fields[n]?.value ?? "") !== fields[n]).map((n) => [n, fields[n] === "" ? null : fields[n]])), [fields, ex]);
  const dup = run.duplicates;
  const reasons = run.decision.reasons;

  return (
    <div className="space-y-4">
      <Card title="What needs attention" subtitle={`System decision: ${run.decision.decision.replace("_", " ").toLowerCase()}`}>
        {reasons.length === 0 ? <p className="text-sm text-slate-600">Nothing flagged - the invoice passed every rule.</p> : (
          <ul className="list-disc pl-5 space-y-1 text-sm text-slate-700">{reasons.map((r: any, i: number) => <li key={i}><b>{r.rule}</b> {r.message}</li>)}</ul>
        )}
        <p className="mt-3 text-xs text-slate-500">Every action below requires a reason and is recorded in the audit trail. Corrections, PO confirmation and duplicate resolution re-run the rules; approve/reject records a final human decision.</p>
      </Card>

      <Card title="1. Correct extracted fields" subtitle={flagged.length ? `Flagged: ${flagged.map((f) => FIELD_LABELS[f]).join(", ")}` : "No fields are flagged"}>
        <div className="grid sm:grid-cols-2 gap-3">
          {EDITABLE.map((n) => {
            const f = ex.fields[n] || {};
            return (
              <div key={n}>
                <label className="label flex items-center gap-1.5">{FIELD_LABELS[n]} <StatusChip status={f.status} /></label>
                <input className={`input ${flagged.includes(n) ? "border-amber-400 bg-amber-50/40" : ""} ${n in changed ? "ring-2 ring-indigo-300" : ""}`}
                  value={fields[n]} placeholder={n.endsWith("_date") ? "YYYY-MM-DD" : ""} onChange={(e) => setFields({ ...fields, [n]: e.target.value })} />
              </div>
            );
          })}
        </div>
        <div className="mt-4">
          <button className="text-xs text-indigo-600 hover:underline" onClick={() => setEditLines(!editLines)}>{editLines ? "Hide" : "Edit"} line items</button>
          {editLines && (
            <div className="mt-2 overflow-x-auto">
              <table className="w-full">
                <thead><tr>{LINE_COLS.map((c) => <th key={c} className="th">{c.replace("_", " ")}</th>)}<th className="th" /></tr></thead>
                <tbody>
                  {lines.map((li, i) => (
                    <tr key={i}>{LINE_COLS.map((c) => (
                      <td key={c} className="px-1 py-1"><input className="input py-1 text-xs" value={li[c]} onChange={(e) => { const n = [...lines]; n[i] = { ...li, [c]: e.target.value }; setLines(n); }} /></td>
                    ))}<td><button onClick={() => setLines(lines.filter((_, j) => j !== i))} className="text-slate-400 hover:text-rose-600"><Trash2 className="h-4 w-4" /></button></td></tr>
                  ))}
                </tbody>
              </table>
              <button className="mt-2 btn-secondary py-1 text-xs" onClick={() => setLines([...lines, Object.fromEntries(LINE_COLS.map((c) => [c, ""]))])}><Plus className="h-3.5 w-3.5" />Add line</button>
            </div>
          )}
        </div>
        <div className="mt-4 flex gap-2">
          <input className="input" placeholder="Reason for correction (required)" value={corrReason} onChange={(e) => setCorrReason(e.target.value)} />
          <button className="btn-primary whitespace-nowrap" disabled={busy || corrReason.trim().length < 3 || (Object.keys(changed).length === 0 && !editLines)}
            onClick={() => act(() => api.patch(`/invoices/${id}/fields`, { fields: changed, line_items: editLines ? lines : null, reason: corrReason }))}>Save & re-run</button>
        </div>
      </Card>

      <Card title="2. Confirm the purchase order" subtitle={run.matching?.selected_po ? `Currently matched: ${run.matching.selected_po} (${run.matching.method.replace(/_/g, " ").toLowerCase()})` : "No PO selected by the system"}>
        {data.vendor_pos.length === 0 ? <Note tone="warn">The vendor is not identified, so no POs can be offered. Correct the vendor tax ID first.</Note> : (
          <div className="flex flex-wrap gap-2">
            <select className="input w-auto" value={po} onChange={(e) => setPo(e.target.value)}>
              <option value="">Select a PO...</option>
              {data.vendor_pos.map((p: any) => <option key={p.po_number} value={p.po_number}>{p.po_number} · {p.description} · remaining {money(p.remaining_amount, p.currency)} · {p.status.toLowerCase()}</option>)}
            </select>
            <input className="input flex-1 min-w-[200px]" placeholder="Reason (required)" value={poReason} onChange={(e) => setPoReason(e.target.value)} />
            <button className="btn-primary" disabled={busy || !po || poReason.trim().length < 3} onClick={() => act(() => api.post(`/invoices/${id}/confirm-po`, { po_number: po, reason: poReason }))}>Confirm & re-run</button>
          </div>
        )}
      </Card>

      {(dup?.status !== "NEW" || data.duplicate_resolution) && (
        <Card title="3. Resolve possible duplicate" subtitle={dup?.explanation}>
          <div className="flex flex-wrap gap-2">
            <input className="input flex-1 min-w-[200px]" placeholder="Reason (required)" value={dupReason} onChange={(e) => setDupReason(e.target.value)} />
            <button className="btn-danger" disabled={busy || dupReason.trim().length < 3} onClick={() => act(() => api.post(`/invoices/${id}/resolve-duplicate`, { resolution: "IS_DUPLICATE", reason: dupReason }))}>It is a duplicate</button>
            <button className="btn-secondary" disabled={busy || dupReason.trim().length < 3} onClick={() => act(() => api.post(`/invoices/${id}/resolve-duplicate`, { resolution: "NOT_DUPLICATE", reason: dupReason }))}>Not a duplicate</button>
          </div>
        </Card>
      )}

      <Card title="4. Final decision" subtitle="Records an authorised human decision. Approving consumes PO balance (if a PO is linked); rejecting releases it.">
        <div className="flex flex-wrap gap-2">
          <input className="input flex-1 min-w-[200px]" placeholder="Reason (required, e.g. 'price increase approved by procurement, mail 12-Oct')" value={finalReason} onChange={(e) => setFinalReason(e.target.value)} />
          <button className="btn-success" disabled={busy || finalReason.trim().length < 3 || inv.status !== "PROCESSED"} onClick={() => act(() => api.post(`/invoices/${id}/final-decision`, { decision: "APPROVED", reason: finalReason }))}><CheckCircle2 className="h-4 w-4" />Approve</button>
          <button className="btn-danger" disabled={busy || finalReason.trim().length < 3 || inv.status !== "PROCESSED"} onClick={() => act(() => api.post(`/invoices/${id}/final-decision`, { decision: "REJECTED", reason: finalReason }))}><XCircle className="h-4 w-4" />Reject</button>
        </div>
        {!run.matching?.selected_po && <div className="mt-3"><Note tone="warn">No PO is linked. Approving now will not consume any PO balance - confirm a PO first if one applies.</Note></div>}
      </Card>

      {data.reviews.length > 0 && (
        <Card title="Review history">
          <ul className="space-y-2">
            {data.reviews.map((r: any) => (
              <li key={r.id} className="text-sm"><span className="text-xs text-slate-400 mr-2">{dateTime(r.created_at)}</span><b>{r.user}</b> · {r.action.replace(/_/g, " ").toLowerCase()} · <span className="text-slate-600">{r.reason}</span></li>
            ))}
          </ul>
        </Card>
      )}
    </div>
  );
}
