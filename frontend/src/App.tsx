import { FileText, LayoutDashboard, LogOut, Package, Settings as SettingsIcon, Upload as UploadIcon, Users } from "lucide-react";
import { ReactNode, useEffect, useState } from "react";
import { NavLink, Navigate, Route, Routes, useLocation } from "react-router-dom";
import { api, auth } from "./api";
import Dashboard from "./pages/Dashboard";
import InvoiceDetail from "./pages/InvoiceDetail";
import Invoices from "./pages/Invoices";
import Login from "./pages/Login";
import PurchaseOrders from "./pages/PurchaseOrders";
import Settings from "./pages/Settings";
import Upload from "./pages/Upload";
import Vendors from "./pages/Vendors";

const NAV = [
  { to: "/", label: "Dashboard", icon: LayoutDashboard, end: true },
  { to: "/upload", label: "Upload invoice", icon: UploadIcon },
  { to: "/invoices", label: "Invoices", icon: FileText },
  { to: "/purchase-orders", label: "Purchase orders", icon: Package },
  { to: "/vendors", label: "Vendors", icon: Users },
  { to: "/settings", label: "Rules & data", icon: SettingsIcon },
];

function Shell({ children, user, onLogout }: { children: ReactNode; user: any; onLogout: () => void }) {
  const [health, setHealth] = useState<any>(null);
  useEffect(() => { api.get("/health").then(setHealth).catch(() => setHealth(null)); }, []);
  return (
    <div className="min-h-screen flex">
      <aside className="hidden md:flex w-60 shrink-0 flex-col bg-slate-900 text-slate-300">
        <div className="px-5 py-5 flex items-center gap-2.5">
          <div className="h-8 w-8 rounded-lg bg-indigo-500 flex items-center justify-center text-white"><FileText className="h-4 w-4" /></div>
          <div>
            <div className="text-sm font-semibold text-white">Invoice AI</div>
            <div className="text-[11px] text-slate-400">AP · PO matching</div>
          </div>
        </div>
        <nav className="flex-1 px-3 space-y-0.5">
          {NAV.map(({ to, label, icon: Icon, end }) => (
            <NavLink key={to} to={to} end={end}
              className={({ isActive }) => `flex items-center gap-2.5 rounded-lg px-3 py-2 text-sm transition ${isActive ? "bg-slate-800 text-white" : "hover:bg-slate-800/60 hover:text-white"}`}>
              <Icon className="h-4 w-4" />{label}
            </NavLink>
          ))}
        </nav>
        <div className="px-5 py-4 text-[11px] space-y-1 border-t border-slate-800">
          <div className="flex items-center gap-1.5"><span className={`h-1.5 w-1.5 rounded-full ${health?.ocr_available ? "bg-emerald-400" : "bg-slate-500"}`} />OCR {health?.ocr_available ? "ready" : "unavailable"}</div>
          <div className="flex items-center gap-1.5"><span className={`h-1.5 w-1.5 rounded-full ${health?.ai_configured ? "bg-emerald-400" : "bg-amber-400"}`} />AI {health?.ai_configured ? "enabled" : "not configured (rules only)"}</div>
        </div>
        <div className="px-5 py-4 border-t border-slate-800 flex items-center justify-between">
          <div className="text-xs"><div className="text-white font-medium">{user?.name}</div><div className="text-slate-400">{user?.username}</div></div>
          <button onClick={onLogout} className="text-slate-400 hover:text-white" title="Sign out"><LogOut className="h-4 w-4" /></button>
        </div>
      </aside>
      <div className="flex-1 min-w-0">
        <div className="md:hidden flex items-center justify-between bg-slate-900 text-white px-4 py-3">
          <span className="font-semibold text-sm">Invoice AI</span>
          <div className="flex gap-3 text-xs">{NAV.slice(0, 3).map((n) => <NavLink key={n.to} to={n.to} end={n.end}>{n.label.split(" ")[0]}</NavLink>)}</div>
        </div>
        <main className="mx-auto max-w-[1400px] px-4 md:px-8 py-6">{children}</main>
      </div>
    </div>
  );
}

export default function App() {
  const [user, setUser] = useState<any>(null);
  const [checking, setChecking] = useState(true);
  const location = useLocation();

  useEffect(() => {
    const onLogout = () => setUser(null);
    window.addEventListener("auth:logout", onLogout);
    if (auth.token) api.get("/auth/me").then(setUser).catch(() => auth.set(null)).finally(() => setChecking(false));
    else setChecking(false);
    return () => window.removeEventListener("auth:logout", onLogout);
  }, []);

  if (checking) return null;
  if (!user) {
    if (location.pathname !== "/login") return <Navigate to="/login" replace />;
    return <Login onLogin={setUser} />;
  }
  const logout = () => { auth.set(null); setUser(null); };
  return (
    <Shell user={user} onLogout={logout}>
      <Routes>
        <Route path="/" element={<Dashboard />} />
        <Route path="/upload" element={<Upload />} />
        <Route path="/invoices" element={<Invoices />} />
        <Route path="/invoices/:id" element={<InvoiceDetail />} />
        <Route path="/purchase-orders" element={<PurchaseOrders />} />
        <Route path="/vendors" element={<Vendors />} />
        <Route path="/settings" element={<Settings />} />
        <Route path="*" element={<Navigate to="/" replace />} />
      </Routes>
    </Shell>
  );
}
