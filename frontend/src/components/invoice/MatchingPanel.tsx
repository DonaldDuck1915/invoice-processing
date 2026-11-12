import { CheckCircle2, XCircle } from "lucide-react";
import { useState } from "react";
import { money, num, pct } from "../../format";
import { Card, Empty, Note, Pill, StatusChip } from "../ui";

const SIGNAL_LABELS: Record<string, string> = {
  semantic: "AI: description meaning", lexical: "Wording similarity", item_code: "Item codes",
  qty_price: "Qty & unit price fit", amount: "Amount vs remaining", date: "Date plausibility",
};

function sub(a?: string, b?: string) {
  // exact decimal subtraction for display (values have <= 4 dp)
  if (a == null || b == null) return null;
  const scale = 10000n;
  const toInt = (v: string) => { const [i, f = ""] = v.split("."); const neg = i.startsWith("-"); const n = BigInt(i.replace("-", "") + (f + "0000").slice(0, 4)); return neg ? -n : n; };
  const r = toInt(a) - toInt(b);
  const neg = r < 0n; const abs = neg ? -r : r;
  return `${neg ? "-" : ""}${abs / scale}.${(abs % scale).toString().padStart(4, "0").slice(0, 2)}`;
}

function ConsumptionBar({ po, run }: { po: any; run: any }) {
  const total = Number(po.subtotal);
  const before = Number(po.consumed_amount);
  const thisInv = (run.matching?.pairs || []).reduce((s: number, p: any) => s + Number(p.invoice.line_total || 0), 0);
  const pctOf = (v: number) => `${Math.max(0, Math.min(100, (v / total) * 100))}%`;
  const over = before + thisInv > total;
  return (
    <div>
      <div className="flex h-3 w-full overflow-hidden rounded-full bg-slate-200">
        <div className="bg-slate-500" style={{ width: pctOf(before) }} title="Previously invoiced" />
        <div className={over ? "bg-rose-500" : po.counts_this_invoice ? "bg-indigo-500" : "bg-indigo-300"} style={{ width: pctOf(thisInv) }} title="This invoice" />
      </div>
      <div className="mt-2 flex flex-wrap gap-x-5 gap-y-1 text-xs text-slate-600">
        <span>PO value (net) <b>{money(po.subtotal, po.currency)}</b></span>
        <span><span className="inline-block h-2 w-2 rounded-full bg-slate-500 mr-1" />Previously invoiced <b>{money(po.consumed_amount, po.currency)}</b></span>
        <span><span className={`inline-block h-2 w-2 rounded-full mr-1 ${over ? "bg-rose-500" : "bg-indigo-500"}`} />This invoice <b>{money(String(thisInv.toFixed(2)), po.currency)}</b>{!po.counts_this_invoice && " (not yet consumed)"}</span>
        <span>Remaining after <b className={over ? "text-rose-600" : ""}>{money(sub(po.remaining_amount, thisInv.toFixed(2)) ?? undefined, po.currency)}</b></span>
      </div>
    </div>
  );
}

export default function MatchingPanel({ data, onConfirm, busy }: { data: any; onConfirm: (po: string, reason: string) => void; busy: boolean }) {
  const run = data.run;
  const m = run.matching;
  const cur = data.invoice.currency;
  const [open, setOpen] = useState<string | null>(m?.candidates?.[0]?.po_number ?? null);
  const [reason, setReason] = useState("");
  if (!m) return <Empty>No matching result</Empty>;
  const lineVars = (m.variances || []).filter((v: any) => v.scope === "LINE");
  const headVars = (m.variances || []).filter((v: any) => v.scope === "HEADER");
  const canConfirm = data.invoice.decision === "MANUAL_REVIEW" && !m.selected_po;

  return (
    <div className="space-y-4">
      <Card title="PO identification">
        <div className="flex flex-wrap items-center gap-3 text-sm">
          <span className="text-slate-500">Method</span><Pill className="bg-indigo-50 text-indigo-700 ring-indigo-600/20">{m.method.replace(/_/g, " ").toLowerCase()}</Pill>
          <span className="text-slate-500">Selected</span><b>{m.selected_po || "none"}</b>
          {m.explicit_reference && <><span className="text-slate-500">Printed reference</span><span>{m.explicit_reference}</span></>}
          {m.ambiguous && <StatusChip status="MANUAL_REVIEW" />}
        </div>
        <p className="mt-2 text-sm text-slate-600">{m.explanation}</p>
        {m.net_basis && (
          <p className="mt-2 text-xs text-sky-700 bg-sky-50 rounded px-2 py-1">
            Prices on this invoice include tax. They were converted to net before comparing with the PO
            {m.net_basis.net_subtotal ? ` (net subtotal ${m.net_basis.net_subtotal})` : " (some lines had no tax rate and stayed gross)"}.
          </p>
        )}
        {m.issues?.length > 0 && <ul className="mt-3 space-y-1">{m.issues.filter((i: any) => i.message !== m.explanation).map((i: any, n: number) => <li key={n} className="text-xs text-slate-600"><b className="text-slate-700">{i.code.replace(/_/g, " ").toLowerCase()}</b>: {i.message}</li>)}</ul>}
        {m.candidates?.length > 0 && !m.semantic?.available && <div className="mt-3"><Note tone="warn">AI meaning signal unavailable ({m.semantic?.error}). Its weight was excluded and inferred matches require confirmation.</Note></div>}
      </Card>

      {m.candidates?.length > 0 && (
        <Card title="Candidate POs" subtitle={`Transparent weighted score out of 100; auto-select needs ≥ ${run.rules.matching.select_min_score} and a ${run.rules.matching.select_margin}-point lead. Amount alone can never select a PO.`}>
          <div className="space-y-2">
            {m.candidates.map((c: any) => (
              <div key={c.po_number} className={`rounded-lg border ${c.po_number === m.selected_po ? "border-emerald-300 bg-emerald-50/40" : c.po_number === m.proposed_po ? "border-indigo-300" : "border-slate-200"}`}>
                <button className="w-full flex items-center justify-between gap-3 px-4 py-3 text-left" onClick={() => setOpen(open === c.po_number ? null : c.po_number)}>
                  <div className="min-w-0">
                    <div className="text-sm font-semibold">{c.po_number} <span className="font-normal text-slate-500">· {c.description}</span></div>
                    <div className="text-xs text-slate-500">PO date {c.po_date} · net {money(c.subtotal, cur)} · remaining {money(c.remaining_amount, cur)} · <StatusChip status={c.status} /></div>
                  </div>
                  {c.eligible ? (
                    <div className="flex items-center gap-3 shrink-0">
                      <div className="h-2 w-28 rounded-full bg-slate-200 overflow-hidden"><div className="h-full bg-indigo-500" style={{ width: `${c.score}%` }} /></div>
                      <span className="text-lg font-semibold tabular-nums w-12 text-right">{c.score}</span>
                    </div>
                  ) : <span className="text-xs text-rose-600 shrink-0">Ineligible: {c.ineligible_reason}</span>}
                </button>
                {open === c.po_number && c.eligible && (
                  <div className="border-t border-slate-100 px-4 py-3">
                    <table className="w-full">
                      <thead><tr><th className="th">Signal</th><th className="th num">Weight</th><th className="th num">Score</th><th className="th num">Points</th><th className="th">Evidence</th></tr></thead>
                      <tbody>
                        {Object.entries(c.signals).map(([k, s]: [string, any]) => (
                          <tr key={k} className={s.value == null ? "text-slate-400" : ""}>
                            <td className="td whitespace-nowrap">{SIGNAL_LABELS[k] || k}{k === "semantic" && <span className="ml-1 text-[10px] text-violet-600">AI</span>}</td>
                            <td className="td num">{s.weight}</td>
                            <td className="td num">{s.value == null ? "n/a" : `${Math.round(s.value * 100)}%`}</td>
                            <td className="td num">{s.points ?? "excluded"}</td>
                            <td className="td text-xs">{s.evidence}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                    <p className="mt-2 text-xs text-slate-500">Description/code evidence {Math.round(c.description_evidence * 100)}% · {c.lines_aligned} line(s) aligned{c.lines_unmatched?.length ? `, ${c.lines_unmatched.length} unmatched` : ""}</p>
                    {canConfirm && (
                      <div className="mt-3 flex gap-2">
                        <input className="input" placeholder="Reason for confirming this PO (required)" value={reason} onChange={(e) => setReason(e.target.value)} />
                        <button className="btn-primary whitespace-nowrap" disabled={busy || reason.trim().length < 3} onClick={() => onConfirm(c.po_number, reason)}>Confirm {c.po_number}</button>
                      </div>
                    )}
                  </div>
                )}
              </div>
            ))}
          </div>
        </Card>
      )}

      {data.po && (
        <Card title={`PO ${data.po.po_number} consumption`} subtitle={`${data.po.description} · status ${data.po.status.toLowerCase()} · PO date ${data.po.po_date}`}>
          <ConsumptionBar po={data.po} run={run} />
        </Card>
      )}

      {m.pairs?.length > 0 && (
        <Card title="Side-by-side line comparison" subtitle="Invoice line vs matched PO line (remaining = PO qty minus other invoices)">
          <div className="overflow-x-auto -mx-5">
            <table className="w-full">
              <thead><tr><th className="th">Invoice line</th><th className="th">PO line</th><th className="th num">Qty inv / remaining</th><th className="th num">Unit price inv / PO</th><th className="th num">Tax % inv / PO</th><th className="th num">Amount</th><th className="th">Match</th></tr></thead>
              <tbody>
                {m.pairs.map((p: any) => {
                  const vs = lineVars.filter((v: any) => v.invoice_line_no === p.invoice_line_no);
                  const bad = (f: string) => vs.find((v: any) => v.field === f)?.exceeded;
                  return (
                    <tr key={p.invoice_line_no}>
                      <td className="td"><div className="text-sm">{p.invoice_line_no}. {p.invoice_description}</div><div className="text-[11px] text-slate-400">{p.invoice.item_code || "no code"}</div></td>
                      <td className="td"><div className="text-sm">{p.po_line_no}. {p.po_description}</div><div className="text-[11px] text-slate-400">{p.po_item_code || "no code"}</div></td>
                      <td className={`td num ${bad("quantity") ? "text-rose-600 font-semibold" : ""}`}>{num(p.invoice.quantity)} / {num(p.po.remaining_qty)}<div className="text-[11px] text-slate-400">ordered {num(p.po.quantity)}, billed {num(p.po.consumed_qty)}</div></td>
                      <td className={`td num ${bad("unit_price") ? "text-rose-600 font-semibold" : ""}`}>{money(p.invoice.unit_price, cur)} / {money(p.po.unit_price, cur)}</td>
                      <td className={`td num ${bad("tax_rate") ? "text-rose-600 font-semibold" : ""}`}>{num(p.invoice.tax_rate)} / {num(p.po.tax_rate)}</td>
                      <td className="td num">{money(p.invoice.line_total, cur)}</td>
                      <td className="td text-xs whitespace-nowrap">{p.similarity.code_match ? "item code" : `${Math.round(p.similarity.combined * 100)}% similar`}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
          {m.unmatched_lines?.length > 0 && <div className="mt-3"><Note tone="warn">Invoice line(s) {m.unmatched_lines.join(", ")} have no counterpart on the PO.</Note></div>}
        </Card>
      )}

      {m.variances?.length > 0 && (
        <Card title="Variance calculations" subtitle="Expected vs actual, absolute and percentage difference, and the tolerance applied">
          <div className="overflow-x-auto -mx-5">
            <table className="w-full">
              <thead><tr><th className="th">Item</th><th className="th">Field</th><th className="th num">Expected</th><th className="th num">Actual</th><th className="th num">Abs. diff</th><th className="th num">% diff</th><th className="th num">Tolerance</th><th className="th">Result</th></tr></thead>
              <tbody>
                {[...lineVars, ...headVars].map((v: any, i: number) => {
                  const isMoney = ["unit_price", "line_amount", "net_amount", "cumulative_value", "tax", "grand_total", "shipping", "discount"].includes(v.field);
                  const fmt = (x: any) => (v.field === "currency" ? x : isMoney ? money(x, cur) : num(x));
                  return (
                    <tr key={i} className={v.exceeded ? "bg-rose-50/60" : ""}>
                      <td className="td text-xs min-w-[190px]">{v.scope === "LINE" ? v.label : <b>{v.label}</b>}{v.note && <div className="text-[11px] text-slate-400">{v.note}</div>}</td>
                      <td className="td text-xs">{v.field.replace(/_/g, " ")}</td>
                      <td className="td num">{fmt(v.expected)}</td>
                      <td className="td num">{fmt(v.actual)}</td>
                      <td className="td num">{v.abs_diff != null ? fmt(v.abs_diff) : "—"}</td>
                      <td className="td num">{v.pct_diff != null ? pct(v.pct_diff) : "—"}</td>
                      <td className="td num text-xs">{v.tolerance_kind === "pct" ? `${v.tolerance}%` : v.tolerance_kind === "abs" ? `±${v.tolerance}` : "exact"}</td>
                      <td className="td">{v.exceeded ? <span className="inline-flex items-center gap-1 text-xs font-medium text-rose-600"><XCircle className="h-3.5 w-3.5" />exceeded</span> : <span className="inline-flex items-center gap-1 text-xs text-emerald-600"><CheckCircle2 className="h-3.5 w-3.5" />within</span>}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        </Card>
      )}
    </div>
  );
}
