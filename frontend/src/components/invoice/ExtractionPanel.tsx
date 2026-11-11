import { FIELD_LABELS, MONEY_FIELDS, dateOnly, money, num } from "../../format";
import { Card, Confidence, Empty, Note, StatusChip } from "../ui";

const ORDER = ["vendor_name", "vendor_tax_id", "invoice_number", "invoice_date", "due_date", "po_number", "currency",
  "subtotal", "taxable_amount", "discount", "tax_amount", "shipping", "grand_total", "vendor_address"];

export default function ExtractionPanel({ run }: { run: any }) {
  const ex = run.extraction;
  const meta = ex.meta || {};
  const cur = ex.fields?.currency?.value;
  const show = (name: string, v: any) => (v == null ? "—" : MONEY_FIELDS.has(name) ? money(v, cur) : name.endsWith("_date") ? dateOnly(v) : v);
  return (
    <div className="space-y-4">
      <div className="flex flex-wrap gap-2 text-xs">
        <span className="rounded-full bg-white border border-slate-200 px-2.5 py-1">Extractor: <b>{meta.extractor}</b></span>
        <span className="rounded-full bg-white border border-slate-200 px-2.5 py-1">{meta.is_scanned ? `Scanned · OCR mean confidence ${Math.round((meta.ocr_mean_conf ?? 0) * 100)}%` : "Digital text layer"}</span>
        <span className="rounded-full bg-white border border-slate-200 px-2.5 py-1">AI extraction: <b>{meta.ai_used ? "used (quotes verified against document text)" : "not used"}</b></span>
      </div>
      {meta.ai_error && <Note tone="warn">AI extraction unavailable: {meta.ai_error}. Rule-based extraction was used.</Note>}
      {meta.ai_notes && <Note>AI note: {meta.ai_notes}</Note>}
      {meta.line_item_note && <Note>{meta.line_item_note}</Note>}
      {(meta.warnings || []).map((w: string) => <Note key={w} tone="warn">{w}</Note>)}

      <Card title="Invoice fields" subtitle="Original reading kept in 'As printed'. Missing values are never invented.">
        <div className="overflow-x-auto -mx-5">
          <table className="w-full">
            <thead><tr><th className="th">Field</th><th className="th">Value</th><th className="th">As printed</th><th className="th">Confidence</th><th className="th">Status</th><th className="th">Source</th></tr></thead>
            <tbody>
              {ORDER.map((name) => {
                const f = ex.fields[name] || { status: "MISSING" };
                return (
                  <tr key={name} className={f.status === "UNCERTAIN" ? "bg-amber-50/50" : f.status === "MISSING" ? "bg-slate-50/50" : ""}>
                    <td className="td font-medium text-slate-700 whitespace-nowrap">{FIELD_LABELS[name]}</td>
                    <td className="td">{show(name, f.value)}{f.note && <div className="text-[11px] text-slate-500 mt-0.5">{f.note}</div>}</td>
                    <td className="td text-xs text-slate-500 max-w-[180px] truncate" title={f.raw || ""}>{f.raw || "—"}</td>
                    <td className="td"><Confidence value={f.confidence} status={f.status} /></td>
                    <td className="td"><StatusChip status={f.status} /></td>
                    <td className="td text-xs text-slate-500">{f.source || "—"}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      </Card>

      <Card title={`Line items (${ex.line_items.length})`}>
        {ex.line_items.length === 0 ? <Empty>No line items could be extracted</Empty> : (
          <div className="overflow-x-auto -mx-5">
            <table className="w-full">
              <thead><tr><th className="th">#</th><th className="th">Description</th><th className="th">Code</th><th className="th num">Qty</th><th className="th">UOM</th><th className="th num">Unit price</th><th className="th num">Tax %</th><th className="th num">Line total</th><th className="th">Status</th></tr></thead>
              <tbody>
                {ex.line_items.map((li: any) => (
                  <tr key={li.line_no} className={li.status === "UNCERTAIN" ? "bg-amber-50/50" : ""}>
                    <td className="td text-slate-400">{li.line_no}</td>
                    <td className="td">{li.description || "—"}{li.note && <div className="text-[11px] text-slate-500">{li.note}</div>}</td>
                    <td className="td text-xs">{li.item_code || "—"}</td>
                    <td className="td num">{num(li.quantity)}</td>
                    <td className="td text-xs">{li.uom || "—"}</td>
                    <td className="td num">{money(li.unit_price, cur)}</td>
                    <td className="td num">{num(li.tax_rate)}</td>
                    <td className="td num">{money(li.line_total, cur)}</td>
                    <td className="td"><div className="flex flex-col gap-1"><StatusChip status={li.status} /><Confidence value={li.confidence} /></div></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>

      {ex.tax_components?.length > 0 && (
        <Card title="Tax components">
          <div className="flex flex-wrap gap-3">
            {ex.tax_components.map((t: any, i: number) => (
              <div key={i} className="rounded-lg border border-slate-200 px-3 py-2 text-sm"><b>{t.kind.replace("_", " ")}</b>{t.rate != null && ` @ ${t.rate}%`}: {money(t.amount, cur)}</div>
            ))}
          </div>
        </Card>
      )}
    </div>
  );
}
