import { Plus, Trash2 } from "lucide-react";
import { FormEvent, useEffect, useMemo, useState } from "react";
import { api } from "../api";
import { money } from "../format";
import { Card, ErrorBox } from "./ui";

type Line = { item_code: string; description: string; uom: string; quantity: string; unit_price: string; tax_rate: string };

const emptyLine = (): Line => ({ item_code: "", description: "", uom: "NOS", quantity: "", unit_price: "", tax_rate: "18" });
const today = () => new Date().toISOString().slice(0, 10);
const n = (v: string) => (v.trim() === "" || isNaN(Number(v)) ? 0 : Number(v));
// a line counts once anything is typed in it; untouched extra lines are simply ignored
const touched = (l: Line) => Boolean(l.item_code.trim() || l.description.trim() || l.quantity || l.unit_price);

/** Form to enter one purchase order by hand. Totals shown here are a preview; the server recomputes them. */
export default function NewPurchaseOrder({ onCreated, onCancel }: { onCreated: (po: any) => void; onCancel: () => void }) {
  const [vendors, setVendors] = useState<any[]>([]);
  const [poNumber, setPoNumber] = useState("");
  const [vendorCode, setVendorCode] = useState("");
  const [poDate, setPoDate] = useState(today());
  const [currency, setCurrency] = useState("");
  const [status, setStatus] = useState("APPROVED");
  const [description, setDescription] = useState("");
  const [lines, setLines] = useState<Line[]>([emptyLine()]);
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);

  const [currencies, setCurrencies] = useState<string[]>(["INR", "EUR", "USD"]);
  useEffect(() => {
    api.get("/vendors").then(setVendors).catch(setError);
    api.get("/config/rules").then((r) => r.supported_currencies && setCurrencies(r.supported_currencies)).catch(() => {});
  }, []);
  const vendor = vendors.find((v) => v.code === vendorCode);
  const cur = currency || vendor?.default_currency || "INR";

  const totals = useMemo(() => {
    const net = lines.reduce((s, l) => s + n(l.quantity) * n(l.unit_price), 0);
    const tax = lines.reduce((s, l) => s + (n(l.quantity) * n(l.unit_price) * n(l.tax_rate)) / 100, 0);
    return { net, tax, total: net + tax };
  }, [lines]);

  const setLine = (i: number, patch: Partial<Line>) => setLines((ls) => ls.map((l, j) => (j === i ? { ...l, ...patch } : l)));

  async function submit(e: FormEvent) {
    e.preventDefault();
    setError(null);
    const filled = lines.filter(touched);
    if (!filled.length) { setError(new Error("Add at least one line.")); return; }
    setBusy(true);
    try {
      const po = await api.post("/purchase-orders", {
        po_number: poNumber, vendor_code: vendorCode, po_date: poDate, currency: currency || null, status,
        description: description || null,
        lines: filled.map((l) => ({ item_code: l.item_code || null, description: l.description, uom: l.uom || null,
          quantity: l.quantity, unit_price: l.unit_price, tax_rate: l.tax_rate || "0" })),
      });
      onCreated(po);
    } catch (err: any) {
      // field-level validation errors come back as a list; show them in plain words
      const details = Array.isArray(err?.details) ? err.details.map((d: any) => `${(d.loc || []).slice(1).join(" › ")}: ${d.msg}`).join("; ") : "";
      setError(details ? new Error(`${err.message} ${details}`) : err);
    } finally { setBusy(false); }
  }

  return (
    <Card title="New purchase order" subtitle="Amounts are recomputed by the server; the PO is used for matching from the next invoice on.">
      <form onSubmit={submit} className="space-y-5">
        <div className="grid sm:grid-cols-2 lg:grid-cols-3 gap-4">
          <div><label className="label">PO number *</label>
            <input className="input" required value={poNumber} onChange={(e) => setPoNumber(e.target.value.toUpperCase())} placeholder="PO-5001" /></div>
          <div><label className="label">Vendor *</label>
            <select className="input" required value={vendorCode} onChange={(e) => { setVendorCode(e.target.value); setCurrency(""); }}>
              <option value="">Select a vendor…</option>
              {vendors.map((v) => <option key={v.code} value={v.code} disabled={v.status === "BLOCKED"}>{v.code} · {v.name}{v.status !== "APPROVED" ? ` (${v.status.toLowerCase()})` : ""}</option>)}
            </select></div>
          <div><label className="label">PO date *</label>
            <input className="input" type="date" required max={today()} value={poDate} onChange={(e) => setPoDate(e.target.value)} /></div>
          <div><label className="label">Currency</label>
            <select className="input" value={cur} onChange={(e) => setCurrency(e.target.value)}>
              {[...new Set([...currencies, cur])].map((c) => <option key={c}>{c}</option>)}
            </select></div>
          <div><label className="label">Status</label>
            <select className="input" value={status} onChange={(e) => setStatus(e.target.value)}>
              <option value="APPROVED">approved (open)</option><option value="CLOSED">closed</option><option value="CANCELLED">cancelled</option>
            </select></div>
          <div><label className="label">Description</label>
            <input className="input" value={description} onChange={(e) => setDescription(e.target.value)} placeholder="e.g. Office chairs for Pune" /></div>
        </div>
        {vendor?.status === "PENDING" && <p className="text-xs text-amber-700">This vendor's approval is pending - invoices against this PO will still go to review.</p>}

        <div className="overflow-x-auto">
          <table className="w-full">
            <thead><tr>
              <th className="th">Item code</th><th className="th">Description *</th><th className="th">UOM</th>
              <th className="th num whitespace-nowrap">Qty *</th><th className="th num whitespace-nowrap">Unit price *</th><th className="th num">Tax %</th><th className="th num">Amount</th><th className="th" />
            </tr></thead>
            <tbody>
              {lines.map((l, i) => (
                <tr key={i}>
                  <td className="td"><input className="input min-w-[7rem]" value={l.item_code} onChange={(e) => setLine(i, { item_code: e.target.value })} /></td>
                  <td className="td"><input className="input min-w-[13rem]" required={i === 0 || touched(l)} value={l.description} onChange={(e) => setLine(i, { description: e.target.value })} /></td>
                  <td className="td"><input className="input min-w-[4.5rem]" value={l.uom} onChange={(e) => setLine(i, { uom: e.target.value.toUpperCase() })} /></td>
                  <td className="td"><input className="input text-right min-w-[5.5rem]" required={i === 0 || touched(l)} type="number" min="0.0001" step="any" value={l.quantity} onChange={(e) => setLine(i, { quantity: e.target.value })} /></td>
                  <td className="td"><input className="input text-right min-w-[7.5rem]" required={i === 0 || touched(l)} type="number" min="0" step="any" value={l.unit_price} onChange={(e) => setLine(i, { unit_price: e.target.value })} /></td>
                  <td className="td"><input className="input text-right min-w-[4.5rem]" type="number" min="0" max="100" step="any" value={l.tax_rate} onChange={(e) => setLine(i, { tax_rate: e.target.value })} /></td>
                  <td className="td num whitespace-nowrap">{money(n(l.quantity) * n(l.unit_price), cur)}</td>
                  <td className="td"><button type="button" className="text-slate-400 hover:text-rose-600 disabled:opacity-30" title="Remove line"
                    disabled={lines.length === 1} onClick={() => setLines((ls) => ls.filter((_, j) => j !== i))}><Trash2 className="h-4 w-4" /></button></td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <div className="flex flex-wrap items-start justify-between gap-4">
          <button type="button" className="btn-secondary" onClick={() => setLines((ls) => [...ls, emptyLine()])}><Plus className="h-4 w-4" />Add line</button>
          <dl className="text-sm grid grid-cols-2 gap-x-6 gap-y-1">
            <dt className="text-slate-500">Net</dt><dd className="num font-medium">{money(totals.net, cur)}</dd>
            <dt className="text-slate-500">Tax</dt><dd className="num">{money(totals.tax, cur)}</dd>
            <dt className="text-slate-500">Total</dt><dd className="num font-semibold">{money(totals.total, cur)}</dd>
          </dl>
        </div>
        <ErrorBox error={error} />
        <div className="flex gap-3">
          <button type="submit" className="btn-primary" disabled={busy}>{busy ? "Saving…" : "Create purchase order"}</button>
          <button type="button" className="btn-secondary" onClick={onCancel}>Cancel</button>
        </div>
      </form>
    </Card>
  );
}
