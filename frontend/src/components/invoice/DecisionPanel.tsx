import { ArrowRight, CheckCircle2, Flag, UserCheck, XCircle } from "lucide-react";
import { AIBox, Card, DecisionBadge, Pill, StatusChip } from "../ui";
import AIRecommendation from "./AIRecommendation";

const RULE_NAMES: Record<string, string> = {
  R1: "Confirmed duplicate", R2: "Cancelled PO", R3: "Closed PO, no balance", R4: "Blocked vendor", R5: "PO belongs to another vendor",
  R6: "Quantity exceeds PO", R7: "Currency mismatch", M1: "Missing critical information", M2: "Low extraction confidence",
  M3: "PO not identified unambiguously", M4: "Probable duplicate", M5: "Price outside tolerance", M6: "Amount outside tolerance",
  M7: "Calculation inconsistency", M8: "Currency mismatch", M9: "Vendor needs verification", M10: "Unmatched invoice line",
  M11: "Date anomaly", M12: "Closed PO with balance", M13: "PO reference corrected", M14: "Tax inconsistency",
  M15: "Unplanned charges", M16: "Processing error", M17: "Unsupported currency",
  M18: "Above auto-approval limit", M19: "Approval-limit pattern", M20: "Bank details changed", M21: "Unusual amount",
  M22: "Invoice predates PO", M23: "GST e-invoice", M24: "Credit/debit note link", M25: "Vendor debit note",
  R8: "AP control (reject policy)",
};

export default function DecisionPanel({ data, onTab, busy, onConfirmPo }: {
  data: any; onTab: (t: string) => void; busy?: boolean; onConfirmPo?: (po: string, reason: string) => void;
}) {
  const run = data.run;
  const d = run.decision;
  const checks: any[] = run.validation || [];
  const rules = run.rules;
  const tone = d.decision === "APPROVED" ? "border-emerald-200 bg-emerald-50/60" : d.decision === "REJECTED" ? "border-rose-200 bg-rose-50/60" : "border-amber-200 bg-amber-50/60";
  return (
    <div className="space-y-4">
      <div className={`rounded-xl border p-5 ${tone}`}>
        <div className="flex items-center justify-between gap-3 flex-wrap">
          <div className="flex items-center gap-3">
            <DecisionBadge decision={d.decision} size="lg" />
            <span className="text-xs text-slate-500">System decision · run {run.run_no}</span>
          </div>
          {d.flags?.length > 0 && <div className="flex gap-1.5 flex-wrap">{d.flags.map((f: string) => (
            <Pill key={f} className={f.startsWith("VENDOR_") || f.includes("DUPLICATE") || ["BANK_DETAILS_CHANGED", "EINVOICE_MISMATCH", "SPLIT_BELOW_LIMIT"].includes(f) ? "bg-rose-100 text-rose-700 ring-rose-600/20" : "bg-white text-slate-600 ring-slate-300"}><Flag className="h-3 w-3" />{f.replace(/_/g, " ").toLowerCase()}</Pill>))}</div>}
        </div>
        <p className="mt-3 text-sm text-slate-800 leading-relaxed">{d.summary}</p>
        {d.approval_level && d.approval_level !== "AUTO" && d.decision !== "REJECTED" && (
          <div className={`mt-2 flex items-center gap-2 text-sm ${data.manager_required ? "text-violet-800" : "text-amber-800"}`}><UserCheck className="h-4 w-4" />
            <b>{data.manager_required ? "Manager approval required." : "Above the auto-approval limit - a reviewer or manager can approve it."}</b>
            <button className="text-xs text-indigo-600 hover:underline" onClick={() => onTab("controls")}>Why?</button></div>
        )}
        <div className="mt-3 flex items-start gap-2 text-sm"><ArrowRight className="h-4 w-4 mt-0.5 text-indigo-600 shrink-0" /><span><b>Recommended next action:</b> {d.next_action}</span></div>
      </div>

      <AIBox text={run.ai_summary?.text} error={run.ai_summary?.error} disclaimer={data.ai_disclaimer} />
      {run.ai_usage?.calls > 0 && (
        <p className="-mt-2 text-[11px] text-slate-400">
          AI this run: {run.ai_usage.calls} call(s) · {(run.ai_usage.input_tokens + run.ai_usage.output_tokens).toLocaleString()} tokens
          {run.ai_usage.cost_usd != null && ` · ≈ $${run.ai_usage.cost_usd.toFixed(5)}`}
        </p>
      )}
      {data.invoice.decided_by !== "HUMAN" && (
        <AIRecommendation rec={run.ai_summary?.recommendation} disclaimer={data.ai_disclaimer} model={run.ai_summary?.model}
          busy={busy} onTab={onTab} onConfirmPo={onConfirmPo} />
      )}

      {d.reasons.length > 0 && (
        <Card title="Reasons behind the decision" subtitle="Every rule that fired, in order of precedence (reject > manual review)">
          <ul className="space-y-3">
            {d.reasons.map((r: any, i: number) => (
              <li key={i} className="flex gap-3">
                <span className={`mt-0.5 shrink-0 rounded px-1.5 py-0.5 text-[11px] font-bold ${r.outcome === "REJECT" ? "bg-rose-600 text-white" : "bg-amber-500 text-white"}`}>{r.rule}</span>
                <div className="min-w-0 text-sm">
                  <div className="font-medium text-slate-800">{RULE_NAMES[r.rule] || r.rule} <StatusChip status={r.outcome} /></div>
                  <div className="text-slate-600">{r.message}</div>
                  {r.evidence && Object.keys(r.evidence).length > 0 && (
                    <div className="mt-1 flex flex-wrap gap-1.5">{Object.entries(r.evidence).filter(([, v]) => typeof v !== "object").map(([k, v]) => (
                      <span key={k} className="rounded bg-slate-100 px-1.5 py-0.5 text-[11px] text-slate-600">{k.replace(/_/g, " ")}: <b>{String(v)}</b></span>))}</div>
                  )}
                </div>
              </li>
            ))}
          </ul>
        </Card>
      )}

      <div className="grid md:grid-cols-2 gap-4">
        <Card title={`Validation checks (${checks.filter((c) => c.status === "PASS").length}/${checks.length} passed)`}>
          <ul className="space-y-1.5 max-h-96 overflow-auto">
            {checks.map((c) => (
              <li key={c.code} className="flex gap-2 text-sm" title={c.message}>
                {c.status === "PASS" ? <CheckCircle2 className="h-4 w-4 text-emerald-500 shrink-0 mt-0.5" /> : c.status === "FAIL" ? <XCircle className="h-4 w-4 text-rose-500 shrink-0 mt-0.5" /> : <span className="h-4 w-4 shrink-0 mt-0.5 rounded-full border-2 border-slate-300" />}
                <div className="min-w-0">
                  <div className="text-slate-800">{c.name} {c.status !== "PASS" && <StatusChip status={c.status} />} {c.status === "FAIL" && c.category !== "OK" && <Pill className="bg-slate-100 text-slate-600 ring-slate-300">{c.category.toLowerCase()}</Pill>}</div>
                  {c.status !== "PASS" && <div className="text-xs text-slate-500">{c.message}</div>}
                </div>
              </li>
            ))}
          </ul>
        </Card>
        <div className="space-y-4">
          <Card title="Matching evidence" actions={<button className="text-xs text-indigo-600 hover:underline" onClick={() => onTab("matching")}>Details</button>}>
            <dl className="text-sm space-y-1.5">
              <div className="flex justify-between gap-3"><dt className="text-slate-500">PO</dt><dd className="font-medium">{run.matching?.selected_po || "not selected"}</dd></div>
              <div className="flex justify-between gap-3"><dt className="text-slate-500">Method</dt><dd>{run.matching?.method?.replace(/_/g, " ").toLowerCase()}</dd></div>
              <div className="flex justify-between gap-3"><dt className="text-slate-500">Variances outside tolerance</dt><dd>{run.matching?.variances?.filter((v: any) => v.exceeded).length ?? 0} of {run.matching?.variances?.length ?? 0}</dd></div>
              <div className="flex justify-between gap-3"><dt className="text-slate-500">Duplicate check</dt><dd><StatusChip status={run.duplicates?.status} /></dd></div>
              <p className="text-xs text-slate-500 pt-1">{run.matching?.explanation}</p>
            </dl>
          </Card>
          <Card title="Applicable tolerance rules" subtitle={`Rules snapshot ${run.rules_hash} used for this run`}>
            <dl className="grid grid-cols-2 gap-x-4 gap-y-1.5 text-xs">
              <dt className="text-slate-500">Quantity</dt><dd>{rules.tolerances.quantity_pct}% over remaining</dd>
              <dt className="text-slate-500">Unit price</dt><dd>{rules.tolerances.unit_price_pct}%</dd>
              <dt className="text-slate-500">Total amount</dt><dd>{rules.tolerances.total_amount_pct}%</dd>
              <dt className="text-slate-500">Tax rounding</dt><dd>±{rules.tolerances.tax_abs_header} + {rules.tolerances.tax_abs_per_line}/line</dd>
              <dt className="text-slate-500">Line arithmetic</dt><dd>±{rules.tolerances.rounding_abs}</dd>
              <dt className="text-slate-500">Min. confidence</dt><dd>{Math.round(rules.extraction.min_confidence * 100)}%</dd>
              <dt className="text-slate-500">Unapproved vendor</dt><dd>{rules.policy.unapproved_vendor.replace("_", " ").toLowerCase()}</dd>
              <dt className="text-slate-500">Currency mismatch</dt><dd>{rules.policy.currency_mismatch.replace("_", " ").toLowerCase()}</dd>
              <dt className="text-slate-500">Over PO quantity</dt><dd>{rules.policy.qty_exceeds_remaining.replace("_", " ").toLowerCase()}</dd>
              <dt className="text-slate-500">PO auto-select</dt><dd>score ≥ {rules.matching.select_min_score}, margin ≥ {rules.matching.select_margin}</dd>
            </dl>
          </Card>
        </div>
      </div>
    </div>
  );
}
