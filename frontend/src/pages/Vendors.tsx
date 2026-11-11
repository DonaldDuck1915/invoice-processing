import { useEffect, useState } from "react";
import { api } from "../api";
import { ErrorBox, Spinner, StatusChip } from "../components/ui";

export default function Vendors() {
  const [rows, setRows] = useState<any[] | null>(null);
  const [error, setError] = useState<unknown>(null);
  useEffect(() => { api.get("/vendors").then(setRows).catch(setError); }, []);
  if (error) return <ErrorBox error={error} />;
  if (!rows) return <Spinner label="Loading vendors..." />;
  return (
    <div className="space-y-5">
      <div>
        <h1 className="text-xl font-semibold text-slate-900">Vendors</h1>
        <p className="text-sm text-slate-500">Vendor master used for identification (by tax ID) and approval status.</p>
      </div>
      <div className="card overflow-x-auto">
        <table className="w-full">
          <thead><tr><th className="th">Code</th><th className="th">Name</th><th className="th">Tax ID</th><th className="th">Country</th><th className="th">Currency</th><th className="th">Status</th><th className="th num">POs</th></tr></thead>
          <tbody>{rows.map((v) => (
            <tr key={v.code}>
              <td className="td font-medium">{v.code}</td>
              <td className="td">{v.name}<div className="text-[11px] text-slate-400">{v.address}</div></td>
              <td className="td font-mono text-xs">{v.tax_id}<div className="text-[11px] text-slate-400 font-sans">{v.tax_id_type}</div></td>
              <td className="td">{v.country}</td>
              <td className="td">{v.default_currency}</td>
              <td className="td"><StatusChip status={v.status} /></td>
              <td className="td num">{v.po_count}</td>
            </tr>
          ))}</tbody>
        </table>
      </div>
    </div>
  );
}
