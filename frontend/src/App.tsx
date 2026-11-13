import { ChevronsLeft, ChevronsRight, FileText, LayoutDashboard, LogOut, Package, Pencil, Settings as SettingsIcon, Upload as UploadIcon, Users } from "lucide-react";
import { ReactNode, useEffect, useState } from "react";
import { HiOutlineDocumentSearch } from "react-icons/hi";
import { NavLink, Navigate, Route, Routes, useLocation } from "react-router-dom";
import { api, auth } from "./api";
import Modal from "./components/Modal";
import { Avatar, ProfileDialog, User } from "./components/Profile";
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
  { to: "/upload", label: "Process invoices", icon: UploadIcon },
  { to: "/invoices", label: "Invoices", icon: FileText },
  { to: "/purchase-orders", label: "Purchase orders", icon: Package },
  { to: "/vendors", label: "Vendors", icon: Users },
  { to: "/settings", label: "Rules & data", icon: SettingsIcon },
];

const WIDTH_KEY = "invoice-ai-sidebar-width";
const COLLAPSED_KEY = "invoice-ai-sidebar-collapsed";
const MIN_W = 200, MAX_W = 360, DEFAULT_W = 248, COLLAPSED_W = 76;

const stored = (key: string, fallback: string) => { try { return localStorage.getItem(key) ?? fallback; } catch { return fallback; } };
const store = (key: string, value: string) => { try { localStorage.setItem(key, value); } catch { /* private mode */ } };

function Shell({ children, user, onLogout, onUser }: { children: ReactNode; user: User; onLogout: () => void; onUser: (u: User) => void }) {
  const [width, setWidth] = useState(() => Math.min(MAX_W, Math.max(MIN_W, Number(stored(WIDTH_KEY, String(DEFAULT_W))) || DEFAULT_W)));
  const [collapsed, setCollapsed] = useState(() => stored(COLLAPSED_KEY, "0") === "1");
  const [dragging, setDragging] = useState(false);
  const [editing, setEditing] = useState(false);
  const [confirmLogout, setConfirmLogout] = useState(false);
  const [switching, setSwitching] = useState(false);

  // Guests can look at their sandbox as a reviewer or as a manager; the page reloads so every screen
  // picks up the new role.
  async function switchRole(role: "REVIEWER" | "MANAGER") {
    if (role === user.role) return;
    setSwitching(true);
    try {
      const r = await api.post("/auth/guest/role", { role });
      auth.set(r.token);
      onUser(r.user);
      window.location.reload();
    } catch { setSwitching(false); }
  }
  useEffect(() => store(WIDTH_KEY, String(width)), [width]);
  useEffect(() => store(COLLAPSED_KEY, collapsed ? "1" : "0"), [collapsed]);

  // Drag the sidebar's right edge to resize it; dragging it narrow enough collapses it to icons.
  useEffect(() => {
    if (!dragging) return;
    const move = (e: MouseEvent) => {
      if (e.clientX < 140) { setCollapsed(true); return; }
      setCollapsed(false);
      setWidth(Math.min(MAX_W, Math.max(MIN_W, e.clientX)));
    };
    const up = () => setDragging(false);
    window.addEventListener("mousemove", move);
    window.addEventListener("mouseup", up);
    document.body.style.cursor = "col-resize";
    document.body.style.userSelect = "none";
    return () => {
      window.removeEventListener("mousemove", move);
      window.removeEventListener("mouseup", up);
      document.body.style.cursor = "";
      document.body.style.userSelect = "";
    };
  }, [dragging]);

  return (
    <div className="h-[100dvh] flex flex-col md:flex-row overflow-hidden">
      <aside style={{ width: collapsed ? COLLAPSED_W : width }}
        className={`relative hidden md:flex shrink-0 flex-col bg-slate-900 text-slate-300 ${dragging ? "" : "transition-[width] duration-200"}`}>
        {/* brand */}
        <div className={`shrink-0 flex items-center gap-2.5 py-5 ${collapsed ? "justify-center" : "px-5"}`}>
          <div className="h-8 w-8 shrink-0 rounded-lg bg-indigo-500 flex items-center justify-center text-white"><HiOutlineDocumentSearch className="h-5 w-5" /></div>
          {!collapsed && (
            <div className="min-w-0">
              <div className="text-sm font-semibold text-white truncate">Invoice AI</div>
              <div className="text-[11px] text-slate-400 truncate">AP · PO matching</div>
            </div>
          )}
        </div>
        <button onClick={() => setCollapsed((c) => !c)} title={collapsed ? "Expand sidebar" : "Collapse sidebar"}
          className="absolute -right-3 top-6 z-20 h-6 w-6 rounded-full bg-slate-800 border border-slate-700 text-slate-300 hover:text-white hover:bg-indigo-600 flex items-center justify-center shadow">
          {collapsed ? <ChevronsRight className="h-3.5 w-3.5" /> : <ChevronsLeft className="h-3.5 w-3.5" />}
        </button>

        {/* navigation scrolls on its own */}
        <nav className="sidebar-scroll flex-1 min-h-0 overflow-y-auto overscroll-contain px-3 space-y-0.5">
          {NAV.map(({ to, label, icon: Icon, end }) => (
            <NavLink key={to} to={to} end={end} title={collapsed ? label : undefined}
              className={({ isActive }) => `flex items-center gap-2.5 rounded-lg py-2 text-sm transition ${collapsed ? "justify-center" : "px-3"} ${isActive ? "bg-slate-800 text-white" : "hover:bg-slate-800/60 hover:text-white"}`}>
              <Icon className="h-4 w-4 shrink-0" />{!collapsed && <span className="truncate">{label}</span>}
            </NavLink>
          ))}
        </nav>

        {/* profile: always on screen at the bottom of the sidebar */}
        <div className="shrink-0 border-t border-slate-800 p-3">
          {user.guest && !collapsed && (
            <div className="mb-2 px-1">
              <div className="mb-1.5 flex items-center justify-between text-[11px]">
                <span className="font-semibold uppercase tracking-wide text-indigo-300">Guest sandbox</span>
                <span className="text-slate-500">private demo data</span>
              </div>
              <div className="grid grid-cols-2 rounded-lg bg-slate-800 p-0.5 text-xs" role="group" aria-label="View as">
                {(["REVIEWER", "MANAGER"] as const).map((r) => (
                  <button key={r} disabled={switching} onClick={() => switchRole(r)} aria-pressed={user.role === r}
                    className={`rounded-md py-1.5 font-medium transition ${user.role === r ? "bg-indigo-600 text-white shadow" : "text-slate-300 hover:text-white"}`}>
                    {r === "REVIEWER" ? "Reviewer" : "Manager"}
                  </button>
                ))}
              </div>
            </div>
          )}
          <div className={`flex items-center gap-1 ${collapsed ? "flex-col" : ""}`}>
            <button onClick={() => setEditing(true)} title="Edit profile"
              className={`group flex min-w-0 flex-1 items-center gap-3 rounded-xl p-2 text-left transition hover:bg-slate-800 ${collapsed ? "justify-center" : ""}`}>
              <Avatar user={user} />
              {!collapsed && (
                <span className="min-w-0 flex-1">
                  <span className="block text-sm font-semibold text-white truncate">{user.name}</span>
                  <span className="block text-[11px] text-indigo-300 truncate">{user.designation}</span>
                </span>
              )}
              {!collapsed && <Pencil className="h-3.5 w-3.5 shrink-0 text-slate-500 opacity-0 transition group-hover:opacity-100" />}
            </button>
            <button onClick={() => setConfirmLogout(true)} title="Sign out"
              className="shrink-0 rounded-lg p-2 text-slate-400 hover:bg-slate-800 hover:text-white"><LogOut className="h-4 w-4" /></button>
          </div>
        </div>

        {/* resize handle */}
        <div onMouseDown={(e) => { e.preventDefault(); setDragging(true); }} onDoubleClick={() => { setCollapsed(false); setWidth(DEFAULT_W); }}
          title="Drag to resize · double-click to reset"
          className={`absolute top-0 right-0 h-full w-1.5 cursor-col-resize transition ${dragging ? "bg-indigo-500/60" : "hover:bg-indigo-500/40"}`} />
      </aside>

      <div className="md:hidden shrink-0 flex items-center justify-between bg-slate-900 text-white px-4 py-3">
        <span className="font-semibold text-sm flex items-center gap-2"><HiOutlineDocumentSearch className="h-5 w-5 text-indigo-300" />Invoice AI</span>
        <div className="flex items-center gap-3 text-xs">
          {NAV.slice(0, 3).map((n) => <NavLink key={n.to} to={n.to} end={n.end}>{n.label.split(" ")[0]}</NavLink>)}
          {user.guest && (
            <button disabled={switching} onClick={() => switchRole(user.role === "MANAGER" ? "REVIEWER" : "MANAGER")}
              className="rounded-md bg-indigo-600 px-2 py-1 font-medium" title="Switch guest role">
              {user.role === "MANAGER" ? "Manager" : "Reviewer"} ⇄
            </button>
          )}
          <button onClick={() => setEditing(true)} title="Edit profile"><Avatar user={user} size="h-7 w-7" text="text-[10px]" /></button>
          <button onClick={() => setConfirmLogout(true)} title="Sign out"><LogOut className="h-4 w-4" /></button>
        </div>
      </div>

      {/* the page scrolls on its own, independent of the sidebar */}
      <div className="flex-1 min-w-0 min-h-0 overflow-y-auto overscroll-contain">
        <main className="mx-auto max-w-[1400px] px-4 md:px-8 py-6">{children}</main>
      </div>

      {editing && <ProfileDialog user={user} onClose={() => setEditing(false)} onSaved={onUser} />}
      {confirmLogout && (
        <Modal title="Sign out?" onClose={() => setConfirmLogout(false)} width="max-w-sm">
          <p className="text-sm text-slate-600">{user.guest
            ? "This ends your guest session. Your sandbox cannot be reopened after signing out; \"Continue as guest\" starts a fresh one."
            : "You will need to sign in again to review invoices."}</p>
          <div className="mt-5 flex justify-end gap-2">
            <button className="btn-secondary" onClick={() => setConfirmLogout(false)} autoFocus>Cancel</button>
            <button className="btn bg-rose-600 text-white hover:bg-rose-700" onClick={() => { setConfirmLogout(false); onLogout(); }}>
              <LogOut className="h-4 w-4" />Sign out
            </button>
          </div>
        </Modal>
      )}
    </div>
  );
}

export default function App() {
  const [user, setUser] = useState<User | null>(null);
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
    <Shell user={user} onLogout={logout} onUser={setUser}>
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
