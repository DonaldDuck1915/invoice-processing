import { FlaskConical } from "lucide-react";
import { FormEvent, useEffect, useState } from "react";
import { HiOutlineDocumentSearch } from "react-icons/hi";
import { useNavigate } from "react-router-dom";
import { api, auth } from "../api";
import { ErrorBox } from "../components/ui";

export default function Login({ onLogin }: { onLogin: (u: any) => void }) {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState<"login" | "guest" | null>(null);
  const [guestMode, setGuestMode] = useState<{ on: boolean; hours: number } | null>(null);
  const nav = useNavigate();

  useEffect(() => {
    api.get("/health").then((h) => setGuestMode({ on: Boolean(h.guest_mode), hours: h.guest_ttl_hours || 24 })).catch(() => setGuestMode(null));
  }, []);

  async function enter(path: string, body?: unknown, kind: "login" | "guest" = "login") {
    setBusy(kind);
    setError(null);
    try {
      const r = await api.post(path, body);
      auth.set(r.token);
      onLogin(r.user);
      nav("/");
    } catch (err) {
      setError(err);
    } finally {
      setBusy(null);
    }
  }

  function submit(e: FormEvent) {
    e.preventDefault();
    enter("/auth/login", { username, password });
  }

  return (
    <div className="min-h-screen flex items-center justify-center bg-gradient-to-br from-slate-900 via-slate-800 to-indigo-900 px-4 py-8">
      <div className="w-full max-w-sm card p-7 space-y-5">
        <div className="flex items-center gap-3">
          <div className="h-10 w-10 rounded-xl bg-indigo-600 flex items-center justify-center text-white"><HiOutlineDocumentSearch className="h-6 w-6" /></div>
          <div>
            <h1 className="text-lg font-semibold text-slate-900">Invoice AI</h1>
            <p className="text-xs text-slate-500">Accounts payable · invoice to decision</p>
          </div>
        </div>

        {guestMode?.on && (
          <div className="rounded-xl border border-indigo-200 bg-indigo-50/60 p-4 space-y-3">
            <div className="flex items-start gap-2.5">
              <FlaskConical className="h-5 w-5 text-indigo-600 shrink-0 mt-0.5" />
              <div className="text-sm">
                <div className="font-semibold text-slate-900">Try it as a guest</div>
                <p className="text-xs text-slate-600 mt-0.5">
                  You get a private sandbox with demo vendors, purchase orders and sample invoices. Upload, review and change
                  settings freely; nobody else sees your data. Switch between reviewer and manager at any time. The sandbox is
                  deleted after {guestMode.hours} hours without use.
                </p>
              </div>
            </div>
            <button type="button" className="btn-primary w-full justify-center" disabled={busy !== null}
              onClick={() => enter("/auth/guest", undefined, "guest")}>
              {busy === "guest" ? "Preparing your sandbox..." : "Continue as guest"}
            </button>
          </div>
        )}

        <form onSubmit={submit} className="space-y-4">
          {guestMode?.on && <div className="flex items-center gap-3 text-[11px] uppercase tracking-wide text-slate-400"><span className="h-px flex-1 bg-slate-200" />or sign in<span className="h-px flex-1 bg-slate-200" /></div>}
          <div>
            <label className="label">Username</label>
            <input className="input" value={username} onChange={(e) => setUsername(e.target.value)} autoFocus={!guestMode?.on} autoComplete="username" />
          </div>
          <div>
            <label className="label">Password</label>
            <input className="input" type="password" value={password} onChange={(e) => setPassword(e.target.value)} autoComplete="current-password" />
          </div>
          <ErrorBox error={error} />
          <button className={`${guestMode?.on ? "btn-secondary" : "btn-primary"} w-full justify-center`} disabled={busy !== null || !username || !password}>
            {busy === "login" ? "Signing in..." : "Sign in"}
          </button>
        </form>
        <p className="text-[11px] text-slate-400 text-center">{guestMode?.on ? "Sign-in is for the AP team; guests use their own sandbox." : "Authorised AP reviewers only."}</p>
      </div>
    </div>
  );
}
