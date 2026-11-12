import { FileUp, Files, Loader2, Mail } from "lucide-react";
import { DragEvent, useEffect, useRef, useState } from "react";
import { api } from "../api";
import LiveRun, { LiveRow } from "../components/LiveRun";
import { Card, ErrorBox, Note } from "../components/ui";

type Queued = { id: number; filename: string };

const isPdf = (f: File) => f.name.toLowerCase().endsWith(".pdf") || f.type === "application/pdf";

export default function Upload() {
  const [files, setFiles] = useState<File[]>([]);
  const [preview, setPreview] = useState<string | null>(null);
  const [queue, setQueue] = useState<Queued[]>([]);
  const [selected, setSelected] = useState<number | null>(null);
  const [rejected, setRejected] = useState<{ filename: string; message: string }[]>([]);
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);
  const [drag, setDrag] = useState(false);
  const [samples, setSamples] = useState<string[]>([]);
  const [email, setEmail] = useState<any>(null);
  const input = useRef<HTMLInputElement>(null);

  useEffect(() => {
    api.get("/admin/sample-invoices").then(setSamples).catch(() => {});
    api.get("/admin/email").then(setEmail).catch(() => {});
  }, []);
  useEffect(() => () => { if (preview) URL.revokeObjectURL(preview); }, [preview]);

  function choose(list: File[]) {
    setError(null);
    const pdfs = list.filter(isPdf);
    if (list.length && !pdfs.length) { setError(new Error("Only PDF files are supported.")); return; }
    if (pdfs.length < list.length) setError(new Error(`${list.length - pdfs.length} non-PDF file(s) were skipped.`));
    setFiles(pdfs);
    if (preview) URL.revokeObjectURL(preview);
    setPreview(pdfs[0] ? URL.createObjectURL(pdfs[0]) : null);
  }

  async function process() {
    if (!files.length) return;
    setBusy(true); setError(null); setRejected([]);
    try {
      let added: Queued[];
      if (files.length === 1) {
        const inv = await api.upload("/invoices", files[0]);
        added = [{ id: inv.id, filename: inv.filename }];
      } else {
        const res = await api.uploadMany("/invoices/batch", files);
        added = res.accepted.map((a: any) => ({ id: a.id, filename: a.filename }));
        setRejected(res.rejected);
      }
      setQueue((q) => [...added, ...q]);
      if (added[0]) setSelected(added[0].id);
      setFiles([]);
    } catch (e) { setError(e); } finally { setBusy(false); }
  }

  async function useSample(name: string) {
    const blob = await api.blob(`/admin/sample-invoices/${encodeURIComponent(name)}`);
    choose([new File([blob], name, { type: "application/pdf" })]);
  }

  async function checkInbox() {
    setError(null);
    try {
      const r = await api.post("/admin/email/poll");
      const ids: number[] = r.invoice_ids;
      setQueue((q) => [...ids.map((id) => ({ id, filename: `E-mail invoice #${id}` })), ...q]);
      if (ids[0]) setSelected(ids[0]);
      if (!ids.length) setError(new Error(`Inbox checked: ${r.messages} new message(s), no new PDF invoices.`));
      setEmail(await api.get("/admin/email"));
    } catch (e) { setError(e); }
  }

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-xl font-semibold text-slate-900">Process invoices</h1>
        <p className="text-sm text-slate-500">Drop one or more PDFs (digital or scanned, max 10 MB / 10 pages each). Each runs through the pipeline live.</p>
      </div>
      <div className="grid lg:grid-cols-5 gap-6">
        <div className="lg:col-span-2 space-y-6">
          <div onDragOver={(e) => { e.preventDefault(); setDrag(true); }} onDragLeave={() => setDrag(false)}
            onDrop={(e: DragEvent) => { e.preventDefault(); setDrag(false); choose(Array.from(e.dataTransfer.files ?? [])); }}
            onClick={() => input.current?.click()}
            className={`card cursor-pointer border-2 border-dashed p-8 text-center transition ${drag ? "border-indigo-500 bg-indigo-50" : "border-slate-300 hover:border-indigo-400"}`}>
            {files.length > 1 ? <Files className="mx-auto h-10 w-10 text-indigo-500" /> : <FileUp className="mx-auto h-10 w-10 text-indigo-500" />}
            <p className="mt-3 text-sm font-medium">Drop PDF invoices here or click to browse</p>
            <p className="text-xs text-slate-500 mt-1">
              {files.length === 0 ? "No file selected" : files.length === 1 ? files[0].name : `${files.length} files selected - processed in this order`}
            </p>
            <input ref={input} type="file" multiple accept="application/pdf,.pdf" className="hidden" onChange={(e) => choose(Array.from(e.target.files ?? []))} />
          </div>
          <div className="flex gap-3 flex-wrap">
            <button className="btn-primary" disabled={!files.length || busy} onClick={process}>
              {busy ? <Loader2 className="h-4 w-4 animate-spin" /> : <FileUp className="h-4 w-4" />}
              {files.length > 1 ? `Process ${files.length} invoices` : "Process invoice"}
            </button>
            {files.length > 0 && !busy && <button className="btn-secondary" onClick={() => choose([])}>Clear</button>}
            {email?.enabled && <button className="btn-secondary" onClick={checkInbox}><Mail className="h-4 w-4" />Check inbox now</button>}
          </div>
          <ErrorBox error={error} />
          {rejected.length > 0 && (
            <Note tone="warn">{rejected.map((r) => <div key={r.filename}><b>{r.filename}</b>: {r.message}</div>)}</Note>
          )}

          {queue.length > 0 && (
            <Card title="Live run" subtitle={queue.length > 1 ? "Select an invoice to follow its stages" : "Each stage reports its result as soon as it finishes"}>
              {queue.length > 1 && (
                <div className="mb-4 max-h-56 overflow-auto -mx-2 space-y-0.5">
                  {queue.map((q) => <LiveRow key={q.id} invoiceId={q.id} filename={q.filename} selected={selected === q.id} onSelect={() => setSelected(q.id)} />)}
                </div>
              )}
              {selected != null && <LiveRun key={selected} invoiceId={selected} />}
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
