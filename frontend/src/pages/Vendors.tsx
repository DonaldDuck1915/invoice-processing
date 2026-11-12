import { Pencil } from "lucide-react";
import { FormEvent, useEffect, useState } from "react";
import { api } from "../api";
import { Card, ErrorBox, Pill, Spinner, StatusChip } from "../components/ui";

const mask = (a?: string | null) => (!a ? "—" : a.length > 4 ? "••••" + a.slice(-4) : a);
const TDS = ["", "194C", "194J", "194I", "194Q"];

function EditVendor({ v, role, onDone, onCancel }: { v: any; role?: string; onDone: (rows: any[]) => void; onCancel: () => void }) {
  const [bank, setBank] = useState(v.bank_account || "");
  const [ifsc, setIfsc] = useState(v.bank_ifsc || "");
  const [tds, setTds] = useState(v.tds_section || "");
  const [einv, setEinv] = useState(Boolean(v.einvoice_required));
  const [reason, setReason] = useState("");
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);
  const manager = role === "MANAGER";

  async function save(e: FormEvent) {
    e.preventDefault();
    setError(null);
    const body: any = { reason, tds_section: tds || null, einvoice_required: einv };
    if (manager && (bank !== (v.bank_account || "") || ifsc !== (v.bank_ifsc || ""))) Object.assign(body, { bank_account: bank || null, bank_ifsc: ifsc || null });
    setBusy(true);
    try { onDone(await api.patch(`/vendors/${v.code}`, body)); } catch (err) { setError(err); } finally { setBusy(false); }
  }

  return (
    <Card title={`Edit ${v.code} · ${v.name}`} subtitle="Payment and compliance data used by the AP controls. Every change is written to the audit log.">
      <form onSubmit={save} className="space-y-4">
        <div className="grid sm:grid-cols-2 lg:grid-cols-4 gap-4">
          <div><label className="label">Bank account / IBAN</label>
            <input className="input" value={bank} disabled={!manager} onChange={(e) => setBank(e.target.value)} placeholder={manager ? "" : mask(v.bank_account)} /></div>
          <div><label className="label">IFSC / SWIFT</label>
            <input className="input" value={ifsc} disabled={!manager} onChange={(e) => setIfsc(e.target.value.toUpperCase())} /></div>
          <div><label className="label">TDS section</label>
            <select className="input" value={tds} onChange={(e) => setTds(e.target.value)}>
              {TDS.map((t) => <option key={t} value={t}>{t || "none"}</option>)}
            </select></div>
          <div><label className="label">GST e-invoicing</label>
            <label className="flex items-center gap-2 text-sm mt-2"><input type="checkbox" checked={einv} onChange={(e) => setEinv(e.target.checked)} />Vendor must send IRN + QR</label></div>
        </div>
        {!manager && <p className="text-xs text-slate-500">Bank details can only be changed by a manager - changed bank details are the most common invoice-fraud pattern.</p>}
        <input className="input" required minLength={3} placeholder="Reason (required, e.g. 'vendor letter 1-Nov, verified by phone')" value={reason} onChange={(e) => setReason(e.target.value)} />
        <ErrorBox error={error} />
        <div className="flex gap-3">
          <button className="btn-primary" disabled={busy}>{busy ? "Saving…" : "Save"}</button>
          <button type="button" className="btn-secondary" onClick={onCancel}>Cancel</button>
        </div>
      </form>
    </Card>
  );
}

export default function Vendors() {
  const [rows, setRows] = useState<any[] | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [editing, setEditing] = useState<string | null>(null);
  const [me, setMe] = useState<any>(null);
  useEffect(() => {
    api.get("/vendors").then(setRows).catch(setError);
    api.get("/auth/me").then(setMe).catch(() => {});
  }, []);
  if (error) return <ErrorBox error={error} />;
  if (!rows) return <Spinner label="Loading vendors..." />;
  const current = rows.find((v) => v.code === editing);
  return (
    <div className="space-y-5">
      <div>
        <h1 className="text-xl font-semibold text-slate-900">Vendors</h1>
        <p className="text-sm text-slate-500">Vendor master used for identification (by tax ID), approval status, bank-detail checks, GST e-invoicing and TDS.</p>
      </div>
      {current && <EditVendor v={current} role={me?.role} onCancel={() => setEditing(null)} onDone={(r) => { setRows(r); setEditing(null); }} />}
      <div className="card overflow-x-auto">
        <table className="w-full">
          <thead><tr><th className="th">Code</th><th className="th">Name</th><th className="th">Tax ID</th><th className="th">Currency</th><th className="th">Status</th>
            <th className="th">Bank on file</th><th className="th">Compliance</th><th className="th num">POs</th><th className="th" /></tr></thead>
          <tbody>{rows.map((v) => (
            <tr key={v.code}>
              <td className="td font-medium">{v.code}</td>
              <td className="td">{v.name}<div className="text-[11px] text-slate-400">{v.address}</div></td>
              <td className="td font-mono text-xs">{v.tax_id}<div className="text-[11px] text-slate-400 font-sans">{v.tax_id_type} · {v.country}</div></td>
              <td className="td">{v.default_currency}</td>
              <td className="td"><StatusChip status={v.status} /></td>
              <td className="td font-mono text-xs">{mask(v.bank_account)}<div className="text-[11px] text-slate-400">{v.bank_ifsc || ""}</div></td>
              <td className="td"><div className="flex flex-wrap gap-1">
                {v.tds_section && <Pill className="bg-indigo-50 text-indigo-700 ring-indigo-600/20">TDS {v.tds_section}</Pill>}
                {v.einvoice_required && <Pill className="bg-emerald-50 text-emerald-700 ring-emerald-600/20">e-invoice</Pill>}
              </div></td>
              <td className="td num">{v.po_count}</td>
              <td className="td"><button className="text-slate-400 hover:text-indigo-600" title="Edit payment & compliance data" onClick={() => setEditing(v.code)}><Pencil className="h-4 w-4" /></button></td>
            </tr>
          ))}</tbody>
        </table>
      </div>
    </div>
  );
}
