import { Camera, Trash2 } from "lucide-react";
import { FormEvent, useRef, useState } from "react";
import { api } from "../api";
import Modal from "./Modal";
import { ErrorBox } from "./ui";

export type User = { username: string; role: string; name: string; designation?: string; avatar?: string | null; guest?: boolean };

const initials = (name: string) => name.split(/\s+/).filter(Boolean).slice(0, 2).map((w) => w[0]).join("").toUpperCase() || "?";

export function Avatar({ user, size = "h-10 w-10", text = "text-sm", offset = "ring-offset-slate-900" }: { user: User; size?: string; text?: string; offset?: string }) {
  return (
    <span className={`${size} shrink-0 rounded-full ring-2 ring-indigo-400/70 ring-offset-2 ${offset} overflow-hidden bg-indigo-500 flex items-center justify-center`}>
      {user.avatar ? <img src={user.avatar} alt="" className="h-full w-full object-cover" />
        : <span className={`${text} font-semibold text-white`}>{initials(user.name)}</span>}
    </span>
  );
}

/** Shrinks a chosen photo to a 160 px square (centre crop) so it can be stored with the profile. */
function toSquareDataUrl(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const img = new Image();
    img.onload = () => {
      const side = Math.min(img.width, img.height);
      const canvas = document.createElement("canvas");
      canvas.width = canvas.height = 160;
      canvas.getContext("2d")!.drawImage(img, (img.width - side) / 2, (img.height - side) / 2, side, side, 0, 0, 160, 160);
      URL.revokeObjectURL(img.src);
      resolve(canvas.toDataURL("image/jpeg", 0.85));
    };
    img.onerror = () => reject(new Error("That file could not be read as an image."));
    img.src = URL.createObjectURL(file);
  });
}

export function ProfileDialog({ user, onClose, onSaved }: { user: User; onClose: () => void; onSaved: (u: User) => void }) {
  const [name, setName] = useState(user.name);
  const [designation, setDesignation] = useState(user.designation || "");
  const [avatar, setAvatar] = useState<string | null>(user.avatar || null);
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);
  const file = useRef<HTMLInputElement>(null);

  async function pick(f?: File) {
    if (!f) return;
    setError(null);
    try { setAvatar(await toSquareDataUrl(f)); } catch (e) { setError(e); }
  }

  async function save(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      onSaved(await api.patch("/auth/me", { name, designation, avatar: avatar || "" }));
      onClose();
    } catch (err) { setError(err); } finally { setBusy(false); }
  }

  return (
    <Modal title="Edit profile" onClose={onClose}>
      <form onSubmit={save} className="space-y-4">
        <div className="flex items-center gap-4">
          <div className="relative">
            <Avatar user={{ ...user, name: name || user.name, avatar }} size="h-16 w-16" text="text-lg" offset="ring-offset-white" />
            <button type="button" onClick={() => file.current?.click()} title="Change photo"
              className="absolute -bottom-1 -right-1 h-7 w-7 rounded-full bg-indigo-600 text-white flex items-center justify-center shadow hover:bg-indigo-700">
              <Camera className="h-3.5 w-3.5" />
            </button>
          </div>
          <div className="text-xs text-slate-500 space-y-1">
            <div>Signed in as <b className="text-slate-700">{user.username}</b>{user.role === "MANAGER" ? " (manager)" : ""}</div>
            <div className="flex gap-3">
              <button type="button" className="text-indigo-600 hover:underline" onClick={() => file.current?.click()}>Upload photo</button>
              {avatar && <button type="button" className="text-slate-500 hover:text-rose-600 inline-flex items-center gap-1" onClick={() => setAvatar(null)}><Trash2 className="h-3 w-3" />Remove</button>}
            </div>
          </div>
          <input ref={file} type="file" accept="image/png,image/jpeg,image/webp" className="hidden" onChange={(e) => pick(e.target.files?.[0])} />
        </div>
        <div><label className="label">Name</label>
          <input className="input" required maxLength={80} value={name} onChange={(e) => setName(e.target.value)} autoFocus /></div>
        <div><label className="label">Designation</label>
          <input className="input" maxLength={80} value={designation} onChange={(e) => setDesignation(e.target.value)} placeholder="e.g. Senior AP Analyst" /></div>
        <ErrorBox error={error} />
        <div className="flex justify-end gap-2">
          <button type="button" className="btn-secondary" onClick={onClose}>Cancel</button>
          <button className="btn-primary" disabled={busy || !name.trim()}>{busy ? "Saving…" : "Save"}</button>
        </div>
      </form>
    </Modal>
  );
}
