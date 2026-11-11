import { Link } from "react-router-dom";
import { dateOnly, money } from "../../format";
import { Card, DecisionBadge, Empty, StatusChip } from "../ui";

const SIG: Record<string, string> = { invoice_number: "Invoice number", date: "Invoice date", amount: "Grand total", po_reference: "PO reference", line_items: "Line items (qty & amount)", fingerprint: "Content fingerprint" };

export default function DuplicatePanel({ data }: { data: any }) {
  const dup = data.run.duplicates;
  const w = data.run.rules.duplicates.weights;
  if (!dup) return <Empty>No duplicate check result</Empty>;
  const target = dup.best || dup.closest?.[0];
  const other = data.duplicate_of;
  const me = data.invoice;
  return (
    <div className="space-y-4">
      <Card title="Duplicate check">
        <div className="flex items-center gap-3"><StatusChip status={dup.status} /><span className="text-sm text-slate-600">{dup.explanation}</span></div>
        <p className="mt-2 text-xs text-slate-500">Compared against {dup.compared} previously processed invoice(s). Confirmed = identical file, or same vendor + invoice number + date + amount. Probable = weighted score ≥ {data.run.rules.duplicates.probable_threshold}. Vendor and amount alone can never reach "probable".</p>
        {data.duplicate_resolution && <p className="mt-2 text-sm">Reviewer resolution: <b>{data.duplicate_resolution.replace("_", " ").toLowerCase()}</b></p>}
      </Card>
      {target && target.signals && Object.keys(target.signals).length > 0 && (
        <Card title={`Signals vs invoice #${target.invoice_id}`} subtitle={target.filename}>
          <table className="w-full">
            <thead><tr><th className="th">Signal</th><th className="th num">Weight</th><th className="th num">Similarity</th><th className="th num">Contribution</th><th className="th">Detail</th></tr></thead>
            <tbody>
              {Object.entries(target.signals).map(([k, s]: [string, any]) => (
                <tr key={k}><td className="td">{SIG[k] || k}</td><td className="td num">{w[k]}</td><td className="td num">{Math.round(s.value * 100)}%</td><td className="td num">{(s.value * w[k]).toFixed(2)}</td><td className="td text-xs">{s.note}</td></tr>
              ))}
              <tr><td className="td font-semibold">Total</td><td className="td" /><td className="td" /><td className="td num font-semibold">{target.score.toFixed(2)}</td><td className="td text-xs">{target.rule}</td></tr>
            </tbody>
          </table>
        </Card>
      )}
      {other && (
        <Card title="Side-by-side with the possible original">
          <div className="grid grid-cols-2 gap-4 text-sm">
            {[["This invoice", me], [`Invoice #${other.id}`, other]].map(([label, x]: any) => (
              <div key={label} className="rounded-lg border border-slate-200 p-4 space-y-1">
                <div className="text-xs font-semibold text-slate-500 uppercase">{label}</div>
                <div className="truncate"><span className="text-slate-500">File:</span> {x.filename}</div>
                <div><span className="text-slate-500">Vendor:</span> {x.vendor_name}</div>
                <div><span className="text-slate-500">Number:</span> <b>{x.invoice_number || "—"}</b></div>
                <div><span className="text-slate-500">Date:</span> {dateOnly(x.invoice_date)}</div>
                <div><span className="text-slate-500">Total:</span> {money(x.grand_total, x.currency)}</div>
                <div><span className="text-slate-500">PO:</span> {x.po_number || "—"}</div>
                <div className="pt-1"><DecisionBadge decision={x.decision} by={x.decided_by} /></div>
                {x.id !== me.id && <Link to={`/invoices/${x.id}`} className="text-xs text-indigo-600 hover:underline">Open invoice #{x.id}</Link>}
              </div>
            ))}
          </div>
        </Card>
      )}
    </div>
  );
}
