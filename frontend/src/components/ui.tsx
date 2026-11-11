import { AlertTriangle, CheckCircle2, CircleHelp, Info, Loader2, Sparkles, XCircle } from "lucide-react";
import { ReactNode } from "react";

export type DecisionT = "APPROVED" | "REJECTED" | "MANUAL_REVIEW" | null | undefined;

const DECISION_STYLE: Record<string, string> = {
  APPROVED: "bg-emerald-50 text-emerald-700 ring-emerald-600/20",
  REJECTED: "bg-rose-50 text-rose-700 ring-rose-600/20",
  MANUAL_REVIEW: "bg-amber-50 text-amber-800 ring-amber-600/20",
};
const DECISION_LABEL: Record<string, string> = { APPROVED: "Approved", REJECTED: "Rejected", MANUAL_REVIEW: "Manual review" };

export function DecisionBadge({ decision, by, size = "sm" }: { decision: DecisionT | string; by?: string | null; size?: "sm" | "lg" }) {
  if (!decision) return <Pill className="bg-slate-100 text-slate-600 ring-slate-500/20">Pending</Pill>;
  const Icon = decision === "APPROVED" ? CheckCircle2 : decision === "REJECTED" ? XCircle : AlertTriangle;
  return (
    <span className={`inline-flex items-center gap-1.5 rounded-full ring-1 ring-inset font-semibold ${DECISION_STYLE[decision]} ${size === "lg" ? "px-3 py-1 text-sm" : "px-2 py-0.5 text-xs"}`}>
      <Icon className={size === "lg" ? "h-4 w-4" : "h-3.5 w-3.5"} />
      {DECISION_LABEL[decision] ?? decision}
      {by === "HUMAN" && <span className="font-normal opacity-75">· by reviewer</span>}
    </span>
  );
}

export function Pill({ children, className = "" }: { children: ReactNode; className?: string }) {
  return <span className={`inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-xs font-medium ring-1 ring-inset ${className}`}>{children}</span>;
}

const STATUS_STYLE: Record<string, string> = {
  EXTRACTED: "bg-emerald-50 text-emerald-700 ring-emerald-600/20",
  CORRECTED: "bg-indigo-50 text-indigo-700 ring-indigo-600/20",
  DERIVED: "bg-sky-50 text-sky-700 ring-sky-600/20",
  UNCERTAIN: "bg-amber-50 text-amber-800 ring-amber-600/20",
  MISSING: "bg-slate-100 text-slate-500 ring-slate-500/20",
  PASS: "bg-emerald-50 text-emerald-700 ring-emerald-600/20",
  FAIL: "bg-rose-50 text-rose-700 ring-rose-600/20",
  WARN: "bg-amber-50 text-amber-800 ring-amber-600/20",
  SKIPPED: "bg-slate-100 text-slate-500 ring-slate-500/20",
  NEW: "bg-emerald-50 text-emerald-700 ring-emerald-600/20",
  PROBABLE: "bg-amber-50 text-amber-800 ring-amber-600/20",
  CONFIRMED: "bg-rose-50 text-rose-700 ring-rose-600/20",
  APPROVED: "bg-emerald-50 text-emerald-700 ring-emerald-600/20",
  CANCELLED: "bg-rose-50 text-rose-700 ring-rose-600/20",
  CLOSED: "bg-slate-100 text-slate-600 ring-slate-500/20",
  BLOCKED: "bg-rose-50 text-rose-700 ring-rose-600/20",
  PENDING: "bg-amber-50 text-amber-800 ring-amber-600/20",
  REJECT: "bg-rose-50 text-rose-700 ring-rose-600/20",
  MANUAL_REVIEW: "bg-amber-50 text-amber-800 ring-amber-600/20",
  INFO: "bg-slate-100 text-slate-600 ring-slate-500/20",
};

export function StatusChip({ status }: { status?: string | null }) {
  if (!status) return null;
  return <Pill className={STATUS_STYLE[status] ?? "bg-slate-100 text-slate-600 ring-slate-500/20"}>{status.replace(/_/g, " ").toLowerCase()}</Pill>;
}

export function Confidence({ value, status }: { value?: number; status?: string }) {
  if (status === "MISSING" || value === undefined || value === null) return <span className="text-xs text-slate-400">—</span>;
  const p = Math.round(value * 100);
  const color = p >= 90 ? "bg-emerald-500" : p >= 80 ? "bg-lime-500" : p >= 60 ? "bg-amber-500" : "bg-rose-500";
  return (
    <div className="flex items-center gap-2" title={`Confidence ${p}%`}>
      <div className="h-1.5 w-16 rounded-full bg-slate-200 overflow-hidden"><div className={`h-full ${color}`} style={{ width: `${p}%` }} /></div>
      <span className="text-xs tabular-nums text-slate-500">{p}%</span>
    </div>
  );
}

export function Card({ title, actions, children, className = "", subtitle }: { title?: ReactNode; subtitle?: ReactNode; actions?: ReactNode; children: ReactNode; className?: string }) {
  return (
    <section className={`card ${className}`}>
      {(title || actions) && (
        <header className="flex items-start justify-between gap-4 border-b border-slate-100 px-5 py-3.5">
          <div>
            {title && <h3 className="text-sm font-semibold text-slate-900">{title}</h3>}
            {subtitle && <p className="text-xs text-slate-500 mt-0.5">{subtitle}</p>}
          </div>
          {actions}
        </header>
      )}
      <div className="p-5">{children}</div>
    </section>
  );
}

export function Spinner({ label }: { label?: string }) {
  return <div className="flex items-center gap-2 text-sm text-slate-500"><Loader2 className="h-4 w-4 animate-spin" />{label}</div>;
}

export function ErrorBox({ error }: { error: unknown }) {
  if (!error) return null;
  const msg = error instanceof Error ? error.message : String(error);
  return <div className="rounded-lg border border-rose-200 bg-rose-50 px-4 py-3 text-sm text-rose-700 flex gap-2"><XCircle className="h-4 w-4 mt-0.5 shrink-0" />{msg}</div>;
}

export function Note({ children, tone = "info" }: { children: ReactNode; tone?: "info" | "warn" }) {
  const Icon = tone === "warn" ? AlertTriangle : Info;
  return (
    <div className={`rounded-lg px-3 py-2 text-xs flex gap-2 ${tone === "warn" ? "bg-amber-50 text-amber-800" : "bg-slate-50 text-slate-600"}`}>
      <Icon className="h-3.5 w-3.5 mt-0.5 shrink-0" />
      <div>{children}</div>
    </div>
  );
}

export function AIBox({ text, disclaimer, error }: { text?: string | null; disclaimer: string; error?: string | null }) {
  if (!text && !error) return null;
  return (
    <div className="rounded-xl border border-violet-200 bg-gradient-to-br from-violet-50 to-white p-4">
      <div className="flex items-center gap-2 text-xs font-semibold text-violet-700">
        <Sparkles className="h-3.5 w-3.5" /> AI summary
      </div>
      {text ? <p className="mt-2 text-sm leading-relaxed text-slate-700">{text}</p> : <p className="mt-2 text-sm text-slate-500">AI summary unavailable: {error}</p>}
      <p className="mt-2 text-[11px] text-violet-600/80">{disclaimer}</p>
    </div>
  );
}

export function Empty({ children }: { children: ReactNode }) {
  return <div className="flex items-center gap-2 py-6 justify-center text-sm text-slate-400"><CircleHelp className="h-4 w-4" />{children}</div>;
}

export function Tabs({ tabs, active, onChange }: { tabs: { id: string; label: ReactNode }[]; active: string; onChange: (id: string) => void }) {
  return (
    <div className="flex gap-1 border-b border-slate-200 overflow-x-auto">
      {tabs.map((t) => (
        <button key={t.id} onClick={() => onChange(t.id)}
          className={`whitespace-nowrap px-3.5 py-2.5 text-sm font-medium border-b-2 -mb-px transition ${active === t.id ? "border-indigo-600 text-indigo-700" : "border-transparent text-slate-500 hover:text-slate-800"}`}>
          {t.label}
        </button>
      ))}
    </div>
  );
}
