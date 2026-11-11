import { useEffect, useState } from "react";
import { api } from "../../api";
import { dateTime } from "../../format";
import { Card, ErrorBox, Spinner } from "../ui";

const COLOR: Record<string, string> = {
  DOCUMENT_UPLOADED: "bg-slate-500", DATA_EXTRACTED: "bg-sky-500", EXTRACTION_REUSED: "bg-sky-300", VALIDATION_COMPLETED: "bg-indigo-500",
  PO_CANDIDATES_RANKED: "bg-violet-400", PO_IDENTIFIED: "bg-violet-500", PO_NOT_IDENTIFIED: "bg-amber-500", MATCHING_COMPLETED: "bg-violet-600",
  DUPLICATE_CHECK_COMPLETED: "bg-fuchsia-500", DECISION_GENERATED: "bg-emerald-600", HUMAN_ACTION: "bg-amber-600",
  FINAL_DECISION: "bg-emerald-700", PROCESSING_FAILED: "bg-rose-600", PO_CONSUMPTION_RECORDED: "bg-teal-500",
  PO_CONSUMPTION_REVERSED: "bg-orange-500", AI_SUMMARY_GENERATED: "bg-violet-300", CORRECTIONS_APPLIED: "bg-amber-400",
};

export default function AuditPanel({ invoiceId }: { invoiceId: string }) {
  const [events, setEvents] = useState<any[] | null>(null);
  const [error, setError] = useState<unknown>(null);
  useEffect(() => { api.get(`/invoices/${invoiceId}/audit`).then(setEvents).catch(setError); }, [invoiceId]);
  if (error) return <ErrorBox error={error} />;
  if (!events) return <Spinner label="Loading audit trail..." />;
  return (
    <Card title="Audit trail" subtitle="Append-only, chronological record of every processing stage and human action">
      <ol className="relative border-l border-slate-200 ml-2 space-y-4">
        {events.map((e) => (
          <li key={e.seq} className="ml-5">
            <span className={`absolute -left-[7px] mt-1 h-3.5 w-3.5 rounded-full ring-4 ring-white ${COLOR[e.event_type] || "bg-slate-400"}`} />
            <div className="flex flex-wrap items-baseline gap-x-2">
              <span className="text-sm font-semibold text-slate-800">{e.event_type.replace(/_/g, " ").toLowerCase()}</span>
              {e.run_no ? <span className="text-[11px] text-slate-400">run {e.run_no}</span> : null}
              <span className="text-[11px] text-slate-400">{dateTime(e.created_at)} · {e.actor}</span>
            </div>
            <p className="text-sm text-slate-600">{e.message}</p>
          </li>
        ))}
      </ol>
    </Card>
  );
}
