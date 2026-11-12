import { AlertTriangle, BadgeCheck, Banknote, CheckCircle2, FileMinus2, FilePlus2, QrCode, ShieldAlert, UserCheck, XCircle } from "lucide-react";
import { Link } from "react-router-dom";
import { dateOnly, money } from "../../format";
import { Card, DecisionBadge, Note, Pill, StatusChip } from "../ui";

const SEVERITY: Record<string, string> = {
  HIGH: "bg-rose-100 text-rose-700 ring-rose-600/20",
  MEDIUM: "bg-amber-100 text-amber-800 ring-amber-600/20",
  LOW: "bg-slate-100 text-slate-600 ring-slate-400/30",
};
const EINV_TONE: Record<string, string> = {
  OK: "bg-emerald-100 text-emerald-700 ring-emerald-600/20", MISSING: "bg-amber-100 text-amber-800 ring-amber-600/20",
  MISMATCH: "bg-rose-100 text-rose-700 ring-rose-600/20",
};

function Row({ label, children }: { label: string; children: React.ReactNode }) {
  return <div className="flex justify-between gap-4 py-1"><dt className="text-slate-500">{label}</dt><dd className="text-right font-medium text-slate-800">{children}</dd></div>;
}

/** Approval level, fraud signals, credit/debit-note links, GST e-invoice and TDS for one invoice. */
export default function ControlsPanel({ data }: { data: any }) {
  const c = data.run?.controls;
  const inv = data.invoice;
  if (!c) return <Note>This invoice was processed before AP controls existed. Re-run it to evaluate approval limits, fraud signals, e-invoice and TDS.</Note>;
  const cur = inv.currency;
  const a = c.approval || {};
  const bank = c.signals?.bank || {};
  const e = c.einvoice || {};
  const tds = c.tds || {};
  const note = c.note;
  const viewer = data.viewer;
  const isNote = c.document_type !== "INVOICE";
  const held = a.level && a.level !== "AUTO";   // the limit kept this invoice from automatic approval

  return (
    <div className="space-y-4">
      {/* ---------------------------------------------------------------- approval level */}
      <div className={`rounded-xl border p-4 flex gap-3 ${data.manager_required ? "border-violet-200 bg-violet-50/70" : held ? "border-amber-200 bg-amber-50/70" : "border-slate-200 bg-white"}`}>
        <UserCheck className={`h-5 w-5 shrink-0 mt-0.5 ${data.manager_required ? "text-violet-600" : held ? "text-amber-600" : "text-slate-400"}`} />
        <div className="text-sm min-w-0">
          <div className="font-semibold text-slate-800 flex items-center gap-2 flex-wrap">
            {data.manager_required ? "Manager approval required" : held ? "Manual approval required (reviewer or manager)" : "Approval: within the auto-approval limit"}
            {a.limit && <Pill className="bg-white text-slate-600 ring-slate-300">limit {money(a.limit, a.currency || cur)}</Pill>}
            {a.near_limit && <Pill className={SEVERITY.MEDIUM}>just under limit</Pill>}
          </div>
          <p className="text-slate-600 mt-0.5">{a.message}</p>
          {data.manager_required && viewer && viewer.role !== "MANAGER" && inv.decision === "MANUAL_REVIEW" && (
            <p className="text-xs text-violet-700 mt-1">You are signed in as a reviewer: you can correct fields or reject, but only a manager can approve.</p>
          )}
        </div>
      </div>

      {/* ---------------------------------------------------------------- risk signals */}
      <Card title="Fraud & risk signals" subtitle="Bank-detail changes, unusual amounts, invoices predating the PO, month-end arrival, approval-limit gaming">
        {c.risk?.length ? (
          <ul className="space-y-2">{c.risk.map((r: any, i: number) => (
            <li key={i} className="flex gap-2 text-sm">
              <ShieldAlert className={`h-4 w-4 shrink-0 mt-0.5 ${r.severity === "HIGH" ? "text-rose-600" : r.severity === "MEDIUM" ? "text-amber-600" : "text-slate-400"}`} />
              <div><Pill className={SEVERITY[r.severity]}>{r.severity.toLowerCase()}</Pill> <span className="text-slate-700">{r.message}</span></div>
            </li>))}</ul>
        ) : <p className="text-sm text-slate-500 flex items-center gap-2"><CheckCircle2 className="h-4 w-4 text-emerald-500" />No risk signals.</p>}
        <dl className="mt-3 border-t border-slate-100 pt-2 text-sm">
          <Row label="Bank account on document">{bank.printed_account}{bank.printed_ifsc ? ` · ${bank.printed_ifsc}` : ""}</Row>
          <Row label="Bank account on file">{bank.master_account || "none"}{bank.master_ifsc ? ` · ${bank.master_ifsc}` : ""}</Row>
          <Row label="Bank check"><StatusChip status={bank.status} /></Row>
          {c.signals?.amount_vs_history && (
            <Row label="Amount vs vendor median">{c.signals.amount_vs_history.ratio}x of {money(c.signals.amount_vs_history.median, cur)} ({c.signals.amount_vs_history.invoices} invoices)</Row>
          )}
          <Row label="Received on">{dateOnly(c.received_on)}</Row>
        </dl>
        {bank.message && <p className="mt-1 text-xs text-slate-500 flex gap-1.5"><Banknote className="h-3.5 w-3.5 shrink-0 mt-0.5" />{bank.message}</p>}
      </Card>

      {/* ---------------------------------------------------------------- credit / debit note */}
      {isNote && (
        <Card title={<span className="flex items-center gap-2">{c.document_type === "CREDIT_NOTE" ? <FileMinus2 className="h-4 w-4 text-emerald-600" /> : <FilePlus2 className="h-4 w-4 text-amber-600" />}{c.document_type === "CREDIT_NOTE" ? "Credit note" : "Debit note"}</span>}
          subtitle={data.run.matching?.explanation}>
          {!note?.original ? <Note tone="warn">Original invoice {note?.original_reference || "(not stated)"} was not found for this vendor. Process it first or correct the "original invoice number" field, then re-run.</Note> : (
            <dl className="text-sm">
              <Row label="Original invoice"><Link className="text-indigo-600 hover:underline" to={`/invoices/${note.original.id}`}>{note.original.invoice_number}</Link> <DecisionBadge decision={note.original.decision} /></Row>
              <Row label="Original total">{money(note.original.grand_total, note.original.currency)}</Row>
              {Number(note.original.credited_before) > 0 && <Row label="Credited earlier">−{money(note.original.credited_before, cur)}</Row>}
              {Number(note.original.debited_before) > 0 && <Row label="Debited earlier">+{money(note.original.debited_before, cur)}</Row>}
              <Row label={c.document_type === "CREDIT_NOTE" ? "This credit note" : "This debit note"}>{c.document_type === "CREDIT_NOTE" ? "−" : "+"}{money(note.amount, cur)}</Row>
              <Row label="Original's net after this note">{money(note.original_net_after, cur)}</Row>
            </dl>
          )}
          {note?.lines?.length > 0 && (
            <table className="w-full mt-3">
              <thead><tr><th className="th">Line</th><th className="th">Effect</th><th className="th num">Qty</th><th className="th num">Amount</th></tr></thead>
              <tbody>{note.lines.map((l: any) => (
                <tr key={l.line_no}><td className="td text-sm">{l.line_no}. {l.description}<div className="text-[11px] text-slate-400">→ PO line {l.po_line_no}{l.note ? ` · ${l.note}` : ""}</div></td>
                  <td className="td text-xs">{l.kind === "RETURN" ? "goods returned (qty back to PO)" : l.kind === "EXTRA_QUANTITY" ? "extra quantity billed" : "value only"}</td>
                  <td className="td num">{Number(l.quantity) ? l.quantity : "—"}</td><td className="td num">{money(l.amount, cur)}</td></tr>))}</tbody>
            </table>
          )}
        </Card>
      )}
      {data.adjustments && (
        <Card title="Credit / debit notes on this invoice" subtitle="Approved notes change what is owed on this invoice">
          <ul className="text-sm divide-y divide-slate-100">{data.adjustments.notes.map((n: any) => (
            <li key={n.id} className="py-1.5 flex justify-between gap-3">
              <span><Link className="text-indigo-600 hover:underline" to={`/invoices/${n.id}`}>{n.number || `#${n.id}`}</Link> <span className="text-slate-500">{n.type === "CREDIT_NOTE" ? "credit note" : "debit note"} · {dateOnly(n.invoice_date)}</span> <DecisionBadge decision={n.decision} /></span>
              <span className={`tabular-nums ${n.decision === "APPROVED" ? "" : "text-slate-400 line-through"}`}>{Number(n.amount) < 0 ? "−" : "+"}{money(Math.abs(Number(n.amount)), cur)}</span>
            </li>))}</ul>
          <div className="mt-2 pt-2 border-t border-slate-200 flex justify-between text-sm font-semibold"><span>Net owed on this invoice</span><span>{money(data.adjustments.net_total, cur)}</span></div>
        </Card>
      )}

      <div className="grid md:grid-cols-2 gap-4">
        {/* ---------------------------------------------------------------- e-invoice */}
        <Card title={<span className="flex items-center gap-2"><QrCode className="h-4 w-4" />GST e-invoice</span>}
          actions={e.status && <Pill className={EINV_TONE[e.status] || "bg-slate-100 text-slate-600 ring-slate-300"}>{String(e.status).replace(/_/g, " ").toLowerCase()}</Pill>}>
          <p className="text-sm text-slate-600">{e.message}</p>
          {(e.irn || e.required) && (
            <dl className="mt-2 text-sm">
              <Row label="Vendor must e-invoice">{e.required ? "yes" : "no"}</Row>
              <Row label="IRN">{e.irn ? <span className="font-mono text-[11px]" title={e.irn}>{e.irn.slice(0, 20)}…</span> : "not printed"}</Row>
              <Row label="Signed QR code">{e.qr_is_einvoice ? "read" : e.qr_found ? "found, not an e-invoice QR" : e.qr_scanned === false ? "not scanned (re-extract)" : "not found"}</Row>
            </dl>
          )}
          {e.checks?.length > 0 && (
            <ul className="mt-2 space-y-1 text-xs">{e.checks.map((k: any) => (
              <li key={k.name} className="flex gap-1.5">{k.ok ? <BadgeCheck className="h-3.5 w-3.5 text-emerald-500 shrink-0" /> : <XCircle className="h-3.5 w-3.5 text-rose-500 shrink-0" />}
                <span><b>{k.name}</b>: printed {k.printed ?? "—"} · QR {k.qr ?? "—"}</span></li>))}</ul>
          )}
        </Card>

        {/* ---------------------------------------------------------------- TDS */}
        <Card title={<span className="flex items-center gap-2"><Banknote className="h-4 w-4" />TDS & payment</span>}
          actions={tds.section && <Pill className="bg-indigo-50 text-indigo-700 ring-indigo-600/20">section {tds.section}</Pill>}>
          <p className="text-sm text-slate-600">{tds.message}</p>
          {tds.applicable && (
            <dl className="mt-2 text-sm">
              <Row label="Invoice total">{money(inv.grand_total, cur)}</Row>
              <Row label="TDS base (ex-GST)">{money(Math.abs(Number(tds.base)), "INR")}</Row>
              <Row label={`TDS @ ${tds.rate}%`}>{Number(tds.amount) ? `−${money(Math.abs(Number(tds.amount)), "INR")}` : money(0, "INR")}</Row>
              {tds.net_payable && <Row label="Pay the vendor">{money(tds.net_payable, "INR")}</Row>}
              <Row label={`FY ${tds.financial_year} billed before`}>{money(tds.aggregate_before, "INR")}</Row>
            </dl>
          )}
          {tds.note && <p className="mt-2 text-xs text-amber-700 flex gap-1.5"><AlertTriangle className="h-3.5 w-3.5 shrink-0 mt-0.5" />{tds.note}</p>}
        </Card>
      </div>
    </div>
  );
}
