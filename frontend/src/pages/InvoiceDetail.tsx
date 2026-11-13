import { ArrowLeft, Download, RefreshCw } from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { api } from "../api";
import AuditPanel from "../components/invoice/AuditPanel";
import ControlsPanel from "../components/invoice/ControlsPanel";
import DecisionPanel from "../components/invoice/DecisionPanel";
import DuplicatePanel from "../components/invoice/DuplicatePanel";
import ExtractionPanel from "../components/invoice/ExtractionPanel";
import MatchingPanel from "../components/invoice/MatchingPanel";
import ReviewPanel from "../components/invoice/ReviewPanel";
import LiveRun from "../components/LiveRun";
import { Card, DecisionBadge, ErrorBox, Pill, Spinner, Tabs } from "../components/ui";
import { dateOnly, money } from "../format";

export default function InvoiceDetail() {
  const { id } = useParams();
  const [data, setData] = useState<any>(null);
  const [error, setError] = useState<unknown>(null);
  const [tab, setTab] = useState("decision");
  const [pdfUrl, setPdfUrl] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const timer = useRef<number>();

  const load = useCallback(async () => {
    try {
      const d = await api.get(`/invoices/${id}`);
      setData(d);
      return d;
    } catch (e) { setError(e); }
  }, [id]);

  const watch = useCallback(() => {
    window.clearInterval(timer.current);
    timer.current = window.setInterval(async () => {
      const n = await load();
      if (n && !["RECEIVED", "PROCESSING"].includes(n.invoice.status) && n.invoice.stage !== "SUMMARISING") window.clearInterval(timer.current);
    }, 1500);
  }, [load]);

  useEffect(() => {
    load().then((d) => { if (d && (["RECEIVED", "PROCESSING"].includes(d.invoice.status) || d.invoice.stage === "SUMMARISING")) watch(); });
    let url: string | null = null;
    api.blob(`/invoices/${id}/file`).then((b) => { url = URL.createObjectURL(b); setPdfUrl(url); }).catch(() => {});
    return () => { window.clearInterval(timer.current); if (url) URL.revokeObjectURL(url); };
  }, [id, load, watch]);

  async function rerun() {
    setError(null);
    try { setData(await api.post(`/invoices/${id}/reprocess`, { reason: "Manual re-run" })); watch(); } catch (e) { setError(e); }
  }

  async function act(fn: () => Promise<any>) {
    setBusy(true);
    setError(null);
    try { const d = await fn(); if (d?.invoice) setData(d); else await load(); } catch (e) { setError(e); } finally { setBusy(false); }
  }

  async function downloadReport() {
    const blob = await api.blob(`/invoices/${id}/report`);
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = `decision-report-${id}.json`;
    a.click();
  }

  if (!data) return error ? <ErrorBox error={error} /> : <Spinner label="Loading invoice..." />;
  const inv = data.invoice;
  const run = data.run;
  const processing = ["RECEIVED", "PROCESSING"].includes(inv.status);
  const needsReview = inv.decision === "MANUAL_REVIEW";

  const tabs = [
    { id: "decision", label: "Decision report" },
    { id: "extraction", label: "Extracted data" },
    { id: "matching", label: "PO matching" },
    { id: "controls", label: <span className="flex items-center gap-1.5">Controls & payment{inv.risk_flags?.length > 0 && <span className="h-2 w-2 rounded-full bg-rose-500" />}</span> },
    { id: "duplicates", label: "Duplicates" },
    { id: "audit", label: "Audit trail" },
    { id: "review", label: <span className="flex items-center gap-1.5">Human review{needsReview && <span className="h-2 w-2 rounded-full bg-amber-500" />}</span> },
  ];

  return (
    <div className="space-y-5">
      <Link to="/invoices" className="inline-flex items-center gap-1 text-sm text-slate-500 hover:text-slate-800"><ArrowLeft className="h-4 w-4" />Invoices</Link>
      <div className="card p-5 flex flex-wrap items-start justify-between gap-4">
        <div className="min-w-0">
          <div className="flex items-center gap-3 flex-wrap">
            <h1 className="text-xl font-semibold text-slate-900">{inv.vendor_name || "Unknown vendor"}</h1>
            {inv.status === "PROCESSED" && <DecisionBadge decision={inv.decision} by={inv.decided_by} size="lg" />}
            {inv.document_type && inv.document_type !== "INVOICE" && <Pill className={inv.document_type === "CREDIT_NOTE" ? "bg-emerald-50 text-emerald-700 ring-emerald-600/20" : "bg-amber-50 text-amber-800 ring-amber-600/20"}>{inv.document_type === "CREDIT_NOTE" ? "credit note" : "debit note"}</Pill>}
            {inv.approval_level === "MANAGER" && inv.decision === "MANUAL_REVIEW" && <Pill className="bg-violet-100 text-violet-700 ring-violet-600/20">needs manager</Pill>}
            {processing && <Spinner label={`Processing - ${String(inv.stage || "").toLowerCase().replace(/_/g, " ")}`} />}
          </div>
          <div className="mt-1 text-sm text-slate-500 flex flex-wrap gap-x-4 gap-y-1">
            <span>{inv.document_type === "CREDIT_NOTE" ? "Credit note" : inv.document_type === "DEBIT_NOTE" ? "Debit note" : "Invoice"} <b className="text-slate-700">{inv.invoice_number || "—"}</b></span>
            {data.original_invoice && <span>Against <Link className="font-semibold text-indigo-600 hover:underline" to={`/invoices/${data.original_invoice.id}`}>{data.original_invoice.invoice_number}</Link></span>}
            {data.adjustments && <span>Net after notes <b className="text-slate-700">{money(data.adjustments.net_total, inv.currency)}</b></span>}
            <span>Dated <b className="text-slate-700">{dateOnly(inv.invoice_date)}</b></span>
            <span>PO <b className="text-slate-700">{inv.po_number || "—"}</b></span>
            <span>Total <b className="text-slate-700">{money(inv.grand_total, inv.currency)}</b></span>
            <span className="truncate">File {inv.filename}{inv.is_scanned ? " (scanned)" : ""}</span>
            {run && <span>Run {run.run_no} · rules {run.rules_hash}</span>}
          </div>
          {inv.decided_by === "HUMAN" && inv.system_decision !== inv.decision && (
            <p className="mt-1 text-xs text-slate-500">System decision was <b>{inv.system_decision?.replace("_", " ").toLowerCase()}</b>; final decision recorded by a reviewer.</p>
          )}
        </div>
        <div className="flex gap-2">
          <button className="btn-secondary" disabled={busy || processing} onClick={rerun}><RefreshCw className="h-4 w-4" />Re-run</button>
          <button className="btn-secondary" onClick={downloadReport} disabled={!run}><Download className="h-4 w-4" />Report</button>
        </div>
      </div>
      <ErrorBox error={error} />
      {inv.status === "FAILED" && <ErrorBox error={new Error(`Processing failed: ${inv.error}`)} />}

      <div className="grid xl:grid-cols-5 gap-6">
        <div className="xl:col-span-2">
          <div className="card overflow-hidden h-[82vh] xl:sticky xl:top-6">
            {pdfUrl ? <iframe title="Invoice PDF" src={pdfUrl} className="w-full h-full" /> : <div className="p-6"><Spinner label="Loading PDF..." /></div>}
          </div>
        </div>
        <div className="xl:col-span-3 min-w-0 space-y-4">
          {processing && <Card title="Live run" subtitle="Each stage reports its result as soon as it finishes"><LiveRun invoiceId={id!} /></Card>}
          {!processing && <Tabs tabs={tabs} active={tab} onChange={setTab} />}
          {processing ? null : !run ? <p className="text-sm text-slate-500">No processing results.</p> : (
            <>
              {tab === "decision" && <DecisionPanel data={data} onTab={setTab} busy={busy}
                onConfirmPo={(po: string, reason: string) => act(() => api.post(`/invoices/${id}/confirm-po`, { po_number: po, reason }))} />}
              {tab === "extraction" && <ExtractionPanel run={run} />}
              {tab === "matching" && <MatchingPanel data={data} busy={busy} onConfirm={(po: string, reason: string) => act(() => api.post(`/invoices/${id}/confirm-po`, { po_number: po, reason }))} />}
              {tab === "controls" && <ControlsPanel data={data} />}
              {tab === "duplicates" && <DuplicatePanel data={data} />}
              {tab === "audit" && <AuditPanel invoiceId={id!} key={`${run.run_no}-${inv.decision}-${inv.decided_by}`} />}
              {tab === "review" && <ReviewPanel data={data} busy={busy} act={act} id={id!} />}
            </>
          )}
        </div>
      </div>
    </div>
  );
}
