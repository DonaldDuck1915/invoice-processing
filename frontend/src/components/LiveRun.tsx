import { CheckCircle2, Circle, Loader2, XCircle } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../api";
import { money } from "../format";
import { DecisionBadge } from "./ui";

/** Pipeline stages in order, with the audit events each stage writes. The backend commits after
 *  every stage, so polling /progress shows each stage's result as soon as it finishes. */
export const STAGES = [
  { id: "QUEUED", label: "Document received", events: ["DOCUMENT_UPLOADED", "IMAGE_CONVERTED", "RECEIVED_BY_EMAIL", "PROCESSING_RESUMED", "PROCESSING_STARTED"] },
  { id: "EXTRACTING", label: "Extract data (text layer / OCR / AI)", events: ["DATA_EXTRACTED", "EXTRACTION_REUSED", "CORRECTIONS_APPLIED", "TAX_INCLUSIVE_DETECTED"] },
  { id: "VALIDATING", label: "Validate fields & arithmetic", events: ["VALIDATION_COMPLETED"] },
  { id: "MATCHING", label: "Identify & match purchase order", events: ["PO_CANDIDATES_RANKED", "PO_IDENTIFIED", "PO_NOT_IDENTIFIED", "MATCHING_COMPLETED"] },
  { id: "DUPLICATE_CHECK", label: "Check for duplicates", events: ["DUPLICATE_CHECK_COMPLETED"] },
  { id: "CONTROLS", label: "AP controls: approval limit, fraud signals, e-invoice, TDS", events: ["NOTE_LINKED", "NOTE_ORIGINAL_MISSING", "CONTROLS_COMPLETED"] },
  { id: "DECIDING", label: "Apply decision rules & update PO ledger", events: ["DECISION_GENERATED", "HUMAN_DECISION_KEPT", "PO_CONSUMPTION_RECORDED", "PO_CONSUMPTION_REVERSED"] },
  { id: "SUMMARISING", label: "Write summary", events: ["AI_SUMMARY_GENERATED"] },
] as const;

const ACTIVE = ["RECEIVED", "PROCESSING"];

type Progress = { invoice: any; events: any[]; run_no: number | null; started_at?: string; finished_at?: string };

export function useProgress(invoiceId: number | string | null, intervalMs = 700) {
  const [data, setData] = useState<Progress | null>(null);
  const [error, setError] = useState<unknown>(null);
  const timer = useRef<number>();
  useEffect(() => {
    if (invoiceId == null) return;
    let stopped = false;
    const tick = async () => {
      try {
        const p = await api.get<Progress>(`/invoices/${invoiceId}/progress`);
        if (stopped) return;
        setData(p);
        if (ACTIVE.includes(p.invoice.status)) timer.current = window.setTimeout(tick, intervalMs);
      } catch (e) { if (!stopped) setError(e); }
    };
    tick();
    return () => { stopped = true; window.clearTimeout(timer.current); };
  }, [invoiceId, intervalMs]);
  return { data, error };
}

function stageIndex(stage: string | null | undefined, status: string) {
  if (status === "PROCESSED" || stage === "DONE") return STAGES.length;
  const i = STAGES.findIndex((s) => s.id === stage);
  return i < 0 ? 0 : i;
}

function seconds(a?: string, b?: string) {
  if (!a) return null;
  const end = b ? new Date(b).getTime() : Date.now();
  return Math.max(0, (end - new Date(a).getTime()) / 1000);
}

/** Full live run view: every stage with its status and what it found. */
export default function LiveRun({ invoiceId, compact = false }: { invoiceId: number | string; compact?: boolean }) {
  const { data, error } = useProgress(invoiceId);
  if (error) return <p className="text-sm text-rose-600">{(error as Error).message}</p>;
  if (!data) return <p className="text-sm text-slate-400 flex items-center gap-2"><Loader2 className="h-4 w-4 animate-spin" />Connecting…</p>;
  const inv = data.invoice;
  const failed = inv.status === "FAILED";
  const current = stageIndex(inv.stage, inv.status);
  const elapsed = seconds(data.started_at, data.finished_at);

  return (
    <div>
      <ol className="relative space-y-0">
        {STAGES.map((s, i) => {
          const done = i < current;
          const active = !failed && i === current && ACTIVE.includes(inv.status);
          const broke = failed && i === current;
          const evs = data.events.filter((e) => (s.events as readonly string[]).includes(e.event_type));
          return (
            <li key={s.id} className="relative pl-7 pb-4 last:pb-0">
              {i < STAGES.length - 1 && <span className={`absolute left-[7px] top-5 bottom-0 w-px ${done ? "bg-emerald-300" : "bg-slate-200"}`} />}
              <span className="absolute left-0 top-0.5">
                {broke ? <XCircle className="h-4 w-4 text-rose-500" /> : done ? <CheckCircle2 className="h-4 w-4 text-emerald-500" />
                  : active ? <Loader2 className="h-4 w-4 animate-spin text-indigo-500" /> : <Circle className="h-4 w-4 text-slate-300" />}
              </span>
              <div className={`text-sm ${done ? "text-slate-800" : active ? "text-indigo-700 font-medium" : broke ? "text-rose-700 font-medium" : "text-slate-400"}`}>
                {s.label}{active && <span className="ml-2 text-xs font-normal text-indigo-500">running…</span>}
              </div>
              {!compact && evs.length > 0 && (
                <ul className="mt-1 space-y-1">
                  {evs.map((e) => (
                    <li key={e.seq} className="text-xs text-slate-600 leading-relaxed animate-[fadein_.3s_ease-out]">{e.message}</li>
                  ))}
                </ul>
              )}
              {broke && <p className="mt-1 text-xs text-rose-700">{inv.error}</p>}
            </li>
          );
        })}
      </ol>
      <div className="mt-4 flex items-center justify-between text-xs text-slate-500">
        <span>{data.run_no ? `Run ${data.run_no}` : "Queued"}{elapsed != null && ` · ${elapsed.toFixed(1)} s`}</span>
        {inv.status === "PROCESSED" && <Link to={`/invoices/${inv.id}`} className="text-indigo-600 hover:underline">Open full report →</Link>}
      </div>
      {inv.status === "PROCESSED" && (
        <div className="mt-4 rounded-lg bg-slate-50 p-4 space-y-2">
          <DecisionBadge decision={inv.decision} by={inv.decided_by} size="lg" />
          <div className="text-sm text-slate-600">{inv.vendor_name || "Unknown vendor"} · {inv.invoice_number || "no number"} · {money(inv.grand_total, inv.currency)}</div>
          <p className="text-sm text-slate-700">{data.events.find((e) => e.event_type === "DECISION_GENERATED")?.message.replace(/^[A-Z_]+: /, "")}</p>
        </div>
      )}
    </div>
  );
}

/** One line per invoice for batch uploads: name, current stage, decision. */
export function LiveRow({ invoiceId, filename, selected, onSelect }: { invoiceId: number; filename: string; selected: boolean; onSelect: () => void }) {
  const { data } = useProgress(invoiceId, 1000);
  const inv = data?.invoice;
  const label = !inv ? "queued" : inv.status === "PROCESSED" ? null : inv.status === "FAILED" ? "failed"
    : (STAGES.find((s) => s.id === inv.stage)?.label ?? "queued");
  return (
    <button onClick={onSelect} className={`w-full flex items-center justify-between gap-3 rounded-lg px-3 py-2 text-left text-sm transition ${selected ? "bg-indigo-50 ring-1 ring-indigo-200" : "hover:bg-slate-50"}`}>
      <span className="truncate text-slate-700">{filename}</span>
      {inv?.status === "PROCESSED" ? <DecisionBadge decision={inv.decision} />
        : inv?.status === "FAILED" ? <span className="text-xs text-rose-600">failed</span>
        : <span className="flex items-center gap-1.5 text-xs text-indigo-600 whitespace-nowrap"><Loader2 className="h-3 w-3 animate-spin" />{label}</span>}
    </button>
  );
}
