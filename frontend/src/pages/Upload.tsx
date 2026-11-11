import { CheckCircle2, Circle, FileUp, Loader2, XCircle } from "lucide-react";
import { DragEvent, useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../api";
import { Card, DecisionBadge, ErrorBox } from "../components/ui";
import { money } from "../format";

const STAGES = [
  { id: "QUEUED", label: "Document uploaded" },
  { id: "EXTRACTING", label: "Extracting data (text layer / OCR / AI)" },
  { id: "VALIDATING", label: "Validating fields & arithmetic" },
  { id: "MATCHING", label: "Identifying & matching PO" },
  { id: "DUPLICATE_CHECK", label: "Checking for duplicates" },
  { id: "DECIDING", label: "Applying decision rules" },
  { id: "DONE", label: "Decision generated" },
];

export default function Upload() {
  const [file, setFile] = useState<File | null>(null);
  const [preview, setPreview] = useState<string | null>(null);
  const [invoice, setInvoice] = useState<any>(null);
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);
  const [drag, setDrag] = useState(false);
  const [samples, setSamples] = useState<string[]>([]);
  const timer = useRef<number>();
  const input = useRef<HTMLInputElement>(null);

  useEffect(() => { api.get("/admin/sample-invoices").then(setSamples).catch(() => {}); return () => window.clearInterval(timer.current); }, []);
  useEffect(() => () => { if (preview) URL.revokeObjectURL(preview); }, [preview]);

  function choose(f: File | null) {
    setError(null);
    setInvoice(null);
    if (!f) return;
    if (!f.name.toLowerCase().endsWith(".pdf") && f.type !== "application/pdf") {
      setError(new Error("Only PDF files are supported."));
      return;
    }
    setFile(f);
    setPreview(URL.createObjectURL(f));
  }

  async function process(f = file) {
    if (!f) return;
    setBusy(true);
    setError(null);
    try {
      const inv = await api.upload("/invoices", f);
      setInvoice(inv);
      window.clearInterval(timer.current);
      timer.current = window.setInterval(async () => {
        try {
          const d = await api.get(`/invoices/${inv.id}`);
          setInvoice(d.invoice);
          if (["PROCESSED", "FAILED"].includes(d.invoice.status)) { window.clearInterval(timer.current); setBusy(false); }
        } catch (e) { setError(e); window.clearInterval(timer.current); setBusy(false); }
      }, 1200);
    } catch (e) {
      setError(e);
      setBusy(false);
    }
  }

  async function useSample(name: string) {
    const blob = await api.blob(`/admin/sample-invoices/${encodeURIComponent(name)}`);
    const f = new File([blob], name, { type: "application/pdf" });
    choose(f);
  }

  const stageIdx = invoice ? STAGES.findIndex((s) => s.id === invoice.stage) : -1;
  const failed = invoice?.status === "FAILED";

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-xl font-semibold text-slate-900">Upload invoice</h1>
        <p className="text-sm text-slate-500">One PDF at a time · digital or scanned · max 10 MB, 10 pages</p>
      </div>
      <div className="grid lg:grid-cols-5 gap-6">
        <div className="lg:col-span-2 space-y-6">
          <div onDragOver={(e) => { e.preventDefault(); setDrag(true); }} onDragLeave={() => setDrag(false)}
            onDrop={(e: DragEvent) => { e.preventDefault(); setDrag(false); choose(e.dataTransfer.files?.[0] ?? null); }}
            onClick={() => input.current?.click()}
            className={`card cursor-pointer border-2 border-dashed p-10 text-center transition ${drag ? "border-indigo-500 bg-indigo-50" : "border-slate-300 hover:border-indigo-400"}`}>
            <FileUp className="mx-auto h-10 w-10 text-indigo-500" />
            <p className="mt-3 text-sm font-medium">Drop a PDF invoice here or click to browse</p>
            <p className="text-xs text-slate-500 mt-1">{file ? file.name : "No file selected"}</p>
            <input ref={input} type="file" accept="application/pdf,.pdf" className="hidden" onChange={(e) => choose(e.target.files?.[0] ?? null)} />
          </div>
          <div className="flex gap-3">
            <button className="btn-primary" disabled={!file || busy} onClick={() => process()}>{busy ? <Loader2 className="h-4 w-4 animate-spin" /> : <FileUp className="h-4 w-4" />}Process invoice</button>
            {file && !busy && <button className="btn-secondary" onClick={() => { setFile(null); setPreview(null); setInvoice(null); }}>Clear</button>}
          </div>
          <ErrorBox error={error} />

          {invoice && (
            <Card title="Processing status">
              <ol className="space-y-2.5">
                {STAGES.map((s, i) => {
                  const done = !failed && (stageIdx > i || invoice.stage === "DONE");
                  const active = !failed && stageIdx === i && invoice.stage !== "DONE";
                  return (
                    <li key={s.id} className="flex items-center gap-2.5 text-sm">
                      {done ? <CheckCircle2 className="h-4 w-4 text-emerald-500" /> : active ? <Loader2 className="h-4 w-4 animate-spin text-indigo-500" /> : <Circle className="h-4 w-4 text-slate-300" />}
                      <span className={done ? "text-slate-800" : active ? "text-indigo-700 font-medium" : "text-slate-400"}>{s.label}</span>
                    </li>
                  );
                })}
              </ol>
              {failed && <div className="mt-4 flex gap-2 text-sm text-rose-700"><XCircle className="h-4 w-4 mt-0.5" />{invoice.error}</div>}
              {invoice.status === "PROCESSED" && (
                <div className="mt-5 rounded-lg bg-slate-50 p-4 space-y-2">
                  <DecisionBadge decision={invoice.decision} size="lg" />
                  <div className="text-sm text-slate-600">{invoice.vendor_name} · {invoice.invoice_number || "no number"} · {money(invoice.grand_total, invoice.currency)}</div>
                  <Link to={`/invoices/${invoice.id}`} className="btn-primary mt-2">Open decision report</Link>
                </div>
              )}
            </Card>
          )}

          {samples.length > 0 && (
            <Card title="Demo invoices" subtitle="Generated samples covering every scenario - load one to try the flow">
              <div className="max-h-72 overflow-auto -mx-2">
                {samples.map((s) => (
                  <button key={s} onClick={() => useSample(s)} className="block w-full text-left text-xs px-2 py-1.5 rounded hover:bg-slate-50 text-slate-700">{s}</button>
                ))}
              </div>
            </Card>
          )}
        </div>
        <div className="lg:col-span-3">
          <div className="card overflow-hidden h-[78vh]">
            {preview ? <iframe title="Invoice preview" src={preview} className="w-full h-full" /> :
              <div className="h-full flex items-center justify-center text-sm text-slate-400">Invoice preview appears here</div>}
          </div>
        </div>
      </div>
    </div>
  );
}
