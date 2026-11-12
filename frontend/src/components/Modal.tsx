import { X } from "lucide-react";
import { ReactNode, useEffect } from "react";

/** Centered dialog with a dimmed backdrop. Escape or a click on the backdrop closes it. */
export default function Modal({ title, onClose, children, width = "max-w-md" }: {
  title: ReactNode; onClose: () => void; children: ReactNode; width?: string;
}) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") onClose(); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center p-4 bg-slate-900/50 backdrop-blur-[1px] animate-[fadein_.15s_ease-out]"
      onMouseDown={(e) => { if (e.target === e.currentTarget) onClose(); }}>
      <div role="dialog" aria-modal="true" className={`w-full ${width} rounded-xl bg-white shadow-xl`}>
        <div className="flex items-center justify-between px-5 pt-4">
          <h2 className="text-base font-semibold text-slate-900">{title}</h2>
          <button className="text-slate-400 hover:text-slate-700" onClick={onClose} aria-label="Close"><X className="h-4 w-4" /></button>
        </div>
        <div className="px-5 pb-5 pt-3">{children}</div>
      </div>
    </div>
  );
}
