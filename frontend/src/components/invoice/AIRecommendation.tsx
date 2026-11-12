import { AlertTriangle, Check, Copy, Lightbulb } from "lucide-react";
import { useState } from "react";
import { Pill } from "../ui";

const CONFIDENCE: Record<string, string> = {
  high: "bg-emerald-50 text-emerald-700 ring-emerald-600/20",
  medium: "bg-amber-50 text-amber-800 ring-amber-600/20",
  low: "bg-slate-100 text-slate-600 ring-slate-500/20",
};

/** Where each recommended action is carried out in this app. */
const TARGET_TAB: Record<string, [string, string]> = {
  CORRECT_FIELDS: ["extraction", "Open extracted data"],
  RESOLVE_DUPLICATE: ["duplicates", "Open duplicate check"],
  APPROVE: ["review", "Go to human review"],
  REJECT: ["review", "Go to human review"],
  ESCALATE: ["review", "Go to human review"],
  REQUEST_INFO_FROM_VENDOR: ["review", "Go to human review"],
  CONFIRM_PO: ["matching", "Open PO matching"],
};

export default function AIRecommendation({ rec, disclaimer, model, busy, onTab, onConfirmPo }: {
  rec: any; disclaimer: string; model?: string; busy?: boolean;
  onTab: (t: string) => void; onConfirmPo?: (po: string, reason: string) => void;
}) {
  const [copied, setCopied] = useState(false);
  if (!rec) return null;
  const [tab, tabLabel] = TARGET_TAB[rec.action] ?? ["review", "Go to human review"];

  async function copy() {
    try { await navigator.clipboard.writeText(rec.vendor_message); setCopied(true); setTimeout(() => setCopied(false), 1500); } catch { /* clipboard blocked */ }
  }

  return (
    <div className="rounded-xl border border-indigo-200 bg-gradient-to-br from-indigo-50 to-white p-4">
      <div className="flex items-center justify-between gap-3 flex-wrap">
        <div className="flex items-center gap-2 text-xs font-semibold text-indigo-700"><Lightbulb className="h-3.5 w-3.5" />AI recommendation</div>
        <Pill className={CONFIDENCE[rec.confidence] ?? CONFIDENCE.low}>{rec.confidence} confidence</Pill>
      </div>
      <p className="mt-2 text-sm font-semibold text-slate-900">{rec.action_label}{rec.suggested_po ? `: ${rec.suggested_po}` : ""}</p>
      <p className="mt-1 text-sm text-slate-700 leading-relaxed">{rec.rationale}</p>
      {rec.steps?.length > 0 && (
        <ol className="mt-2 list-decimal pl-5 space-y-0.5 text-sm text-slate-700">{rec.steps.map((s: string, i: number) => <li key={i}>{s}</li>)}</ol>
      )}
      {rec.guard_warnings?.length > 0 && (
        <div className="mt-2 flex gap-1.5 text-xs text-amber-800"><AlertTriangle className="h-3.5 w-3.5 mt-0.5 shrink-0" />{rec.guard_warnings.join(" ")}</div>
      )}
      {rec.vendor_message && (
        <div className="mt-3">
          <div className="flex items-center justify-between"><span className="text-xs font-medium text-slate-500">Draft message to the vendor</span>
            <button className="text-xs text-indigo-600 hover:underline flex items-center gap-1" onClick={copy}>{copied ? <Check className="h-3 w-3" /> : <Copy className="h-3 w-3" />}{copied ? "Copied" : "Copy"}</button></div>
          <pre className="mt-1 whitespace-pre-wrap rounded-lg bg-white border border-slate-200 p-3 text-xs text-slate-700 font-sans">{rec.vendor_message}</pre>
        </div>
      )}
      <div className="mt-3 flex flex-wrap gap-2">
        {rec.action === "CONFIRM_PO" && rec.suggested_po && onConfirmPo && (
          <button className="btn-primary" disabled={busy}
            onClick={() => onConfirmPo(rec.suggested_po, `Accepted AI recommendation: ${rec.rationale}`.slice(0, 1900))}>
            Confirm {rec.suggested_po}
          </button>
        )}
        <button className="btn-secondary" onClick={() => onTab(tab)}>{tabLabel}</button>
      </div>
      <p className="mt-2 text-[11px] text-indigo-600/80">Advisory only{model ? ` · ${model}` : ""} - the rule engine made the decision and nothing is applied without you. {disclaimer}</p>
    </div>
  );
}
