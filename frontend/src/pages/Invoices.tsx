import { Search } from "lucide-react";
import { useEffect, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { api } from "../api";
import { DecisionBadge, Empty, ErrorBox, Pill, Spinner } from "../components/ui";
import { dateOnly, dateTime, money } from "../format";

const FILTERS = [["", "All"], ["APPROVED", "Approved"], ["MANUAL_REVIEW", "Manual review"], ["REJECTED", "Rejected"]];

export default function Invoices() {
  const [params, setParams] = useSearchParams();
  const decision = params.get("decision") || "";
  const [q, setQ] = useState("");
  const [rows, setRows] = useState<any[] | null>(null);
  const [error, setError] = useState<unknown>(null);

  useEffect(() => {
    const qs = new URLSearchParams();
    if (decision) qs.set("decision", decision);
    if (q) qs.set("q", q);
    const t = setTimeout(() => api.get(`/invoices?${qs}`).then(setRows).catch(setError), 200);
    return () => clearTimeout(t);
  }, [decision, q]);

  return (
    <div className="space-y-5">
      <h1 className="text-xl font-semibold text-slate-900">Invoices</h1>
      <div className="flex flex-wrap items-center gap-3">
        <div className="flex rounded-lg border border-slate-300 bg-white p-0.5">
          {FILTERS.map(([v, l]) => (
            <button key={v} onClick={() => setParams(v ? { decision: v } : {})}
              className={`px-3 py-1.5 text-sm rounded-md ${decision === v ? "bg-indigo-600 text-white" : "text-slate-600 hover:bg-slate-50"}`}>{l}</button>
          ))}
        </div>
        <div className="relative">
          <Search className="absolute left-2.5 top-2.5 h-4 w-4 text-slate-400" />
          <input className="input pl-8 w-72" placeholder="Search vendor, invoice no., PO, file" value={q} onChange={(e) => setQ(e.target.value)} />
        </div>
      </div>
      <ErrorBox error={error} />
      {!rows ? <Spinner label="Loading..." /> : rows.length === 0 ? <div className="card"><Empty>No invoices match</Empty></div> : (
        <div className="card overflow-x-auto">
          <table className="w-full">
            <thead><tr>
              <th className="th">#</th><th className="th">Vendor</th><th className="th">Invoice</th><th className="th">Date</th>
              <th className="th">PO</th><th className="th num">Grand total</th><th className="th">Decision</th><th className="th">Uploaded</th>
            </tr></thead>
            <tbody>
              {rows.map((i) => (
                <tr key={i.id} className="hover:bg-slate-50">
                  <td className="td text-slate-400">{i.id}</td>
                  <td className="td"><Link to={`/invoices/${i.id}`} className="font-medium text-slate-800 hover:text-indigo-700">{i.vendor_name || "—"}</Link>
                    <div className="text-xs text-slate-400 truncate max-w-[260px]">{i.filename}</div></td>
                  <td className="td">{i.invoice_number || <span className="text-slate-400">missing</span>}{i.is_scanned && <Pill className="ml-2 bg-slate-100 text-slate-500 ring-slate-400/20">scan</Pill>}</td>
                  <td className="td whitespace-nowrap">{dateOnly(i.invoice_date)}</td>
                  <td className="td">{i.po_number || <span className="text-slate-400">—</span>}</td>
                  <td className="td num">{money(i.grand_total, i.currency)}</td>
                  <td className="td">{i.status === "PROCESSED" ? <DecisionBadge decision={i.decision} by={i.decided_by} /> : <Pill className="bg-slate-100 text-slate-600 ring-slate-400/20">{i.status.toLowerCase()}</Pill>}</td>
                  <td className="td text-xs text-slate-500 whitespace-nowrap">{dateTime(i.created_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
