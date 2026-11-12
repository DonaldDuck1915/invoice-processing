import { Plus } from "lucide-react";
import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../api";
import NewPurchaseOrder from "../components/NewPurchaseOrder";
import { Card, ErrorBox, Note, Spinner, StatusChip } from "../components/ui";
import { dateTime, money, num } from "../format";

export default function PurchaseOrders() {
  const [pos, setPos] = useState<any[] | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [open, setOpen] = useState<string | null>(null);
  const [detail, setDetail] = useState<any>(null);
  const [adding, setAdding] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const load = () => api.get("/purchase-orders").then(setPos).catch(setError);
  useEffect(() => { load(); }, []);
  useEffect(() => { setDetail(null); if (open) api.get(`/purchase-orders/${open}`).then(setDetail).catch(setError); }, [open]);
  if (error) return <ErrorBox error={error} />;
  if (!pos) return <Spinner label="Loading purchase orders..." />;
  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div>
          <h1 className="text-xl font-semibold text-slate-900">Purchase orders</h1>
          <p className="text-sm text-slate-500">Consumption is derived from the ledger of approved invoices (each invoice counted once per PO line).</p>
        </div>
        {!adding && <button className="btn-primary" onClick={() => { setAdding(true); setNotice(null); }}><Plus className="h-4 w-4" />New purchase order</button>}
      </div>
      {notice && <Note>{notice}</Note>}
      {adding && (
        <NewPurchaseOrder onCancel={() => setAdding(false)} onCreated={(po) => {
          setAdding(false);
          setNotice(`${po.po_number} created (${money(po.subtotal, po.currency)} net). Invoices waiting for a PO can be re-run to match it.`);
          load();
          setOpen(po.po_number);
        }} />
      )}
      <div className="card overflow-x-auto">
        <table className="w-full">
          <thead><tr><th className="th">PO</th><th className="th">Vendor</th><th className="th">Description</th><th className="th">Status</th><th className="th num">Net value</th><th className="th" style={{ width: 220 }}>Consumed</th><th className="th num">Remaining</th></tr></thead>
          <tbody>
            {pos.map((p) => {
              const used = Number(p.consumed_amount) / Number(p.subtotal || 1);
              return (
                <tr key={p.po_number} className="hover:bg-slate-50 cursor-pointer" onClick={() => setOpen(open === p.po_number ? null : p.po_number)}>
                  <td className="td font-medium">{p.po_number}<div className="text-[11px] text-slate-400">{p.po_date}</div></td>
                  <td className="td">{p.vendor_name}<div className="text-[11px] text-slate-400">{p.vendor_code}</div></td>
                  <td className="td text-sm">{p.description}</td>
                  <td className="td"><StatusChip status={p.status} /></td>
                  <td className="td num">{money(p.subtotal, p.currency)}</td>
                  <td className="td"><div className="h-2 rounded-full bg-slate-200 overflow-hidden"><div className={`h-full ${used > 1 ? "bg-rose-500" : "bg-indigo-500"}`} style={{ width: `${Math.min(100, used * 100)}%` }} /></div><div className="text-[11px] text-slate-500 mt-1">{money(p.consumed_amount, p.currency)} ({Math.round(used * 100)}%)</div></td>
                  <td className="td num">{money(p.remaining_amount, p.currency)}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      {open && (
        <Card title={`${open} detail`} actions={<button className="text-xs text-slate-500" onClick={() => setOpen(null)}>Close</button>}>
          {!detail ? <Spinner /> : (
            <div className="space-y-4">
              <table className="w-full">
                <thead><tr><th className="th">#</th><th className="th">Code</th><th className="th">Description</th><th className="th num">Ordered</th><th className="th num">Invoiced</th><th className="th num">Remaining</th><th className="th num">Unit price</th><th className="th num">Tax %</th></tr></thead>
                <tbody>{detail.lines.map((l: any) => (
                  <tr key={l.line_no}><td className="td">{l.line_no}</td><td className="td text-xs">{l.item_code}</td><td className="td">{l.description}</td><td className="td num">{num(l.quantity)} {l.uom}</td><td className="td num">{num(l.consumed_qty)}</td><td className="td num">{num(l.remaining_qty)}</td><td className="td num">{money(l.unit_price, detail.currency)}</td><td className="td num">{num(l.tax_rate)}</td></tr>
                ))}</tbody>
              </table>
              <div>
                <h4 className="text-xs font-semibold text-slate-500 uppercase mb-2">Consumption history</h4>
                {detail.consumption.length === 0 ? <p className="text-sm text-slate-400">No invoices against this PO yet.</p> : (
                  <ul className="space-y-1 text-sm">{detail.consumption.map((c: any, i: number) => (
                    <li key={i} className={c.status === "REVERSED" ? "text-slate-400 line-through" : ""}>
                      <Link to={`/invoices/${c.invoice_id}`} className="text-indigo-600 hover:underline">{c.invoice_number || c.filename}</Link> · line {c.po_line_no} · qty {num(c.quantity)} · {money(c.amount, detail.currency)} · {c.status.toLowerCase()} · {dateTime(c.created_at)}
                    </li>))}</ul>
                )}
              </div>
            </div>
          )}
        </Card>
      )}
    </div>
  );
}
