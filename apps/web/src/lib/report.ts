// Sends browser-side errors to the API (/api/client-error) so they land in Cloud Logging and
// Error Reporting next to server errors. Anonymous: no user id, email or chat text.

const RELEASE = import.meta.env.VITE_RELEASE || "dev";
const seen = new Set<string>();
let sent = 0;

export type ErrorKind = "window-error" | "unhandled-rejection" | "sign-in" | "member-sign-in" | "chat-stream" | "api" | "render";

export function reportError(kind: ErrorKind, err: unknown, code?: string) {
  const e = err instanceof Error ? err : new Error(typeof err === "string" ? err : JSON.stringify(err));
  const message = (e.message || String(err)).slice(0, 2000);
  const key = `${kind}|${code ?? ""}|${message}`;
  if (seen.has(key) || sent >= 20) return;   // don't flood: once per distinct error, 20 per page load
  seen.add(key);
  sent++;
  if (import.meta.env.DEV) console.warn("[cypress] would report", kind, code, e);
  const body = JSON.stringify({
    kind, message, code: code ?? (e as { code?: string }).code ?? null,
    stack: e.stack?.slice(0, 8000) ?? null, path: location.pathname, release: RELEASE,
  });
  try {
    // sendBeacon survives page unloads; fall back to fetch.
    const blob = new Blob([body], { type: "application/json" });
    if (!navigator.sendBeacon?.("/api/client-error", blob)) {
      fetch("/api/client-error", { method: "POST", headers: { "content-type": "application/json" }, body, keepalive: true })
        .catch(() => undefined);
    }
  } catch { /* reporting must never break the page */ }
}

export function installGlobalHandlers() {
  window.addEventListener("error", (ev) => {
    // Ignore errors from extensions and cross-origin scripts ("Script error." with no details).
    if (!ev.error && (!ev.message || ev.message === "Script error.")) return;
    reportError("window-error", ev.error ?? ev.message);
  });
  window.addEventListener("unhandledrejection", (ev) => {
    if ((ev.reason as Error)?.name === "AbortError") return;   // the user pressed stop
    reportError("unhandled-rejection", ev.reason);
  });
}
