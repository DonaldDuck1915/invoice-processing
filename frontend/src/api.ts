export const API_URL = (import.meta.env.VITE_API_URL || "http://localhost:8000").replace(/\/$/, "");
const TOKEN_KEY = "invoice-ai-token";

export class ApiError extends Error {
  code: string;
  status: number;
  details: unknown;
  constructor(status: number, code: string, message: string, details?: unknown) {
    super(message);
    this.status = status;
    this.code = code;
    this.details = details;
  }
}

export const auth = {
  get token(): string | null {
    try { return localStorage.getItem(TOKEN_KEY); } catch { return null; }
  },
  set(token: string | null) {
    try { token ? localStorage.setItem(TOKEN_KEY, token) : localStorage.removeItem(TOKEN_KEY); } catch { /* ignore */ }
  },
};

async function request<T = any>(path: string, init: RequestInit = {}): Promise<T> {
  const headers: Record<string, string> = { ...(init.headers as Record<string, string>) };
  if (auth.token) headers["Authorization"] = `Bearer ${auth.token}`;
  if (init.body && !(init.body instanceof FormData)) headers["Content-Type"] = "application/json";
  let res: Response;
  try {
    res = await fetch(`${API_URL}/api/v1${path}`, { ...init, headers });
  } catch {
    throw new ApiError(0, "NETWORK", "Cannot reach the server. It may be starting up - try again in a moment.");
  }
  if (res.status === 401 && !path.startsWith("/auth/login")) {
    auth.set(null);
    window.dispatchEvent(new Event("auth:logout"));
  }
  if (!res.ok) {
    let body: any = {};
    try { body = await res.json(); } catch { /* not json */ }
    throw new ApiError(res.status, body.error_code || "ERROR", body.message || `Request failed (${res.status})`, body.details);
  }
  const type = res.headers.get("content-type") || "";
  return (type.includes("application/json") ? res.json() : res.blob()) as Promise<T>;
}

export const api = {
  get: <T = any>(p: string) => request<T>(p),
  post: <T = any>(p: string, body?: unknown) => request<T>(p, { method: "POST", body: body === undefined ? undefined : JSON.stringify(body) }),
  put: <T = any>(p: string, body: unknown) => request<T>(p, { method: "PUT", body: JSON.stringify(body) }),
  patch: <T = any>(p: string, body: unknown) => request<T>(p, { method: "PATCH", body: JSON.stringify(body) }),
  upload: <T = any>(p: string, file: File | Blob, name = "invoice.pdf") => {
    const fd = new FormData();
    fd.append("file", file, (file as File).name || name);
    return request<T>(p, { method: "POST", body: fd });
  },
  blob: (p: string) => request<Blob>(p),
};
