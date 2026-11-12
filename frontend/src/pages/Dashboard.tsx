import { AlertTriangle, CheckCircle2, FileStack, Upload, XCircle } from "lucide-react";
import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../api";
import { Card, DecisionBadge, Empty, ErrorBox, Spinner } from "../components/ui";
import { dateTime, money } from "../format";

function Stat({ label, value, icon: Icon, tone, to }: { label: string; value: any; icon: any; tone: string; to?: string }) {
  const body = (
    <div className="card p-5 flex items-center gap-4 hover:shadow transition">
      <div className={`h-11 w-11 rounded-xl flex items-center justify-center ${tone}`}><Icon className="h-5 w-5" /></div>
      <div>
        <div className="text-2xl font-semibold tabular-nums text-slate-900">{value}</div>
        <div className="text-xs text-slate-500">{label}</div>
      </div>
    </div>
  );
  return to ? <Link to={to}>{body}</Link> : body;
}

export default function Dashboard() {
  const [stats, setStats] = useState<any>(null);
  const [review, setReview] = useState<any[]>([]);
  const [error, setError] = useState<unknown>(null);

  useEffect(() => {
    api.get("/dashboard/stats").then(setStats).catch(setError);
    api.get("/invoices?decision=MANUAL_REVIEW&limit=8").then(setReview).catch(() => {});
  }, []);

  if (error) return <ErrorBox error={error} />;
  if (!stats) return <Spinner label="Loading dashboard..." />;
  const values = Object.entries(stats.total_value || {}) as [string, string][];

  return (
    <div className="space-y-6">
      <div className="flex items-end justify-between">
        <div>
          <h1 className="text-xl font-semibold text-slate-900">Dashboard</h1>
          <p className="text-sm text-slate-500">Invoice processing overview</p>
        </div>
        <Link to="/upload" className="btn-primary"><Upload className="h-4 w-4" />Upload invoice</Link>
      </div>

      <div className="grid grid-cols-2 lg:grid-cols-4 gap-4">
        <Stat label="Invoices processed" value={stats.total} icon={FileStack} tone="bg-indigo-50 text-indigo-600" to="/invoices" />
        <Stat label="Approved" value={stats.approved} icon={CheckCircle2} tone="bg-emerald-50 text-emerald-600" to="/invoices?decision=APPROVED" />
        <Stat label="Rejected" value={stats.rejected} icon={XCircle} tone="bg-rose-50 text-rose-600" to="/invoices?decision=REJECTED" />
        <Stat label="Awaiting manual review" value={stats.manual_review} icon={AlertTriangle} tone="bg-amber-50 text-amber-600" to="/invoices?decision=MANUAL_REVIEW" />
      </div>
      {(stats.awaiting_manager > 0 || stats.risk_flagged > 0 || stats.notes > 0 || Number(stats.tds_withheld?.INR) > 0) && (
        <div className="flex flex-wrap gap-x-6 gap-y-1 text-sm text-slate-600 -mt-2">
          {stats.awaiting_manager > 0 && <span><b className="text-violet-700">{stats.awaiting_manager}</b> above the approval limit, waiting for a manager</span>}
          {stats.risk_flagged > 0 && <span><b className="text-rose-700">{stats.risk_flagged}</b> with fraud / risk signals</span>}
          {stats.notes > 0 && <span><b>{stats.notes}</b> credit / debit note(s)</span>}
          {Number(stats.tds_withheld?.INR) > 0 && <span>TDS withheld on approved invoices: <b>{money(stats.tds_withheld.INR, "INR")}</b></span>}
        </div>
      )}

      <div className="grid lg:grid-cols-3 gap-6">
        <Card title="Total invoice value" subtitle="Grand totals by currency (no conversion)">
          {values.length === 0 ? <Empty>No invoices yet</Empty> : (
            <div className="space-y-4">
              {values.map(([cur, v]) => (
                <div key={cur}>
                  <div className="text-xs text-slate-500">{cur}</div>
                  <div className="text-xl font-semibold tabular-nums">{money(v, cur)}</div>
                  <div className="mt-1 flex gap-3 text-[11px] text-slate-500">
                    {["APPROVED", "MANUAL_REVIEW", "REJECTED"].map((d) => stats.value_by_decision?.[d]?.[cur] && (
                      <span key={d}>{d === "MANUAL_REVIEW" ? "Review" : d[0] + d.slice(1).toLowerCase()}: {money(stats.value_by_decision[d][cur], cur)}</span>
                    ))}
                  </div>
                </div>
              ))}
              {stats.human_decided > 0 && <p className="text-xs text-slate-500">{stats.human_decided} decision(s) made by a reviewer.</p>}
            </div>
          )}
          {stats.ai_usage?.calls > 0 && (
            <div className="mt-4 border-t border-slate-100 pt-3 text-xs text-slate-500 space-y-0.5">
              <div className="font-medium text-slate-700">AI usage</div>
              <div>{stats.ai_usage.calls} call(s) over {stats.ai_usage.runs} run(s) · {(stats.ai_usage.input_tokens + stats.ai_usage.output_tokens).toLocaleString()} tokens</div>
              {stats.ai_usage.cost_usd != null && <div>≈ ${stats.ai_usage.cost_usd.toFixed(4)} total · ${stats.ai_usage.avg_cost_per_run_usd.toFixed(5)} per run</div>}
            </div>
          )}
        </Card>

        <Card title="Needs your review" className="lg:col-span-2" actions={<Link to="/invoices?decision=MANUAL_REVIEW" className="text-xs text-indigo-600 hover:underline">View all</Link>}>
          {review.length === 0 ? <Empty>Nothing waiting for review</Empty> : (
            <ul className="divide-y divide-slate-100 -my-2">
              {review.map((i) => (
                <li key={i.id}>
                  <Link to={`/invoices/${i.id}`} className="flex items-center justify-between py-2.5 hover:bg-slate-50 -mx-2 px-2 rounded">
                    <div className="min-w-0">
                      <div className="text-sm font-medium truncate">{i.vendor_name || i.filename}</div>
                      <div className="text-xs text-slate-500">{i.invoice_number || "no number"} · {i.po_number || "no PO"} · {i.filename}</div>
                    </div>
                    <div className="text-sm tabular-nums">{money(i.grand_total, i.currency)}</div>
                  </Link>
                </li>
              ))}
            </ul>
          )}
        </Card>
      </div>

      <Card title="Recent processing activity">
        {stats.recent_activity.length === 0 ? <Empty>No activity yet - upload an invoice to begin</Empty> : (
          <ul className="space-y-3">
            {stats.recent_activity.map((e: any, n: number) => (
              <li key={n} className="flex gap-3 text-sm">
                <div className="w-36 shrink-0 text-xs text-slate-400 pt-0.5">{dateTime(e.created_at)}</div>
                <div className="min-w-0">
                  <Link to={`/invoices/${e.invoice_id}`} className="font-medium text-slate-800 hover:text-indigo-700">{e.filename}</Link>
                  <span className="text-slate-400"> · {e.event_type.replace(/_/g, " ").toLowerCase()}{e.actor !== "SYSTEM" ? ` · ${e.actor}` : ""}</span>
                  <div className="text-slate-600 text-xs mt-0.5 line-clamp-2">
                    {e.event_type === "DECISION_GENERATED" ? <><DecisionBadge decision={e.message.split(":")[0]} /> <span className="ml-1">{e.message.split(": ").slice(1).join(": ")}</span></> : e.message}
                  </div>
                </div>
              </li>
            ))}
          </ul>
        )}
      </Card>
    </div>
  );
}
