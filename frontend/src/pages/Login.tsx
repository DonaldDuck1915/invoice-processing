import { FileText } from "lucide-react";
import { FormEvent, useState } from "react";
import { useNavigate } from "react-router-dom";
import { api, auth } from "../api";
import { ErrorBox } from "../components/ui";

export default function Login({ onLogin }: { onLogin: (u: any) => void }) {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);
  const nav = useNavigate();

  async function submit(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const r = await api.post("/auth/login", { username, password });
      auth.set(r.token);
      onLogin(r.user);
      nav("/");
    } catch (err) {
      setError(err);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="min-h-screen flex items-center justify-center bg-gradient-to-br from-slate-900 via-slate-800 to-indigo-900 px-4">
      <form onSubmit={submit} className="w-full max-w-sm card p-7 space-y-5">
        <div className="flex items-center gap-3">
          <div className="h-10 w-10 rounded-xl bg-indigo-600 flex items-center justify-center text-white"><FileText className="h-5 w-5" /></div>
          <div>
            <h1 className="text-lg font-semibold text-slate-900">Invoice AI</h1>
            <p className="text-xs text-slate-500">Accounts payable · invoice to decision</p>
          </div>
        </div>
        <div>
          <label className="label">Username</label>
          <input className="input" value={username} onChange={(e) => setUsername(e.target.value)} autoFocus autoComplete="username" />
        </div>
        <div>
          <label className="label">Password</label>
          <input className="input" type="password" value={password} onChange={(e) => setPassword(e.target.value)} autoComplete="current-password" />
        </div>
        <ErrorBox error={error} />
        <button className="btn-primary w-full justify-center" disabled={busy || !username || !password}>{busy ? "Signing in..." : "Sign in"}</button>
        <p className="text-[11px] text-slate-400 text-center">Authorised AP reviewers only.</p>
      </form>
    </div>
  );
}
