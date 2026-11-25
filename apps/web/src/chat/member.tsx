// Member sign-in for the resident chat: passwordless email link (Firebase), checked against the
// LHCA member list by the API. Residents chat anonymously; signing in unlocks reporting issues and
// member-only documents. Nothing here reveals whether an address is on the list until its owner
// opens the emailed link.

import { useEffect, useState } from "react";
import {
  isSignInWithEmailLink, onIdTokenChanged, sendSignInLinkToEmail, signInWithEmailLink, signOut,
} from "firebase/auth";
import { setToken } from "../admin/api";
import { currentAuth, firebaseAuth } from "../lib/firebase";
import { reportError } from "../lib/report";

const EMAIL_KEY = "cypress.signin.email";
const PENDING_KEY = "cypress.signin.pending";
const STAFF = ["admin", "board", "staff", "security"];

export type Member =
  | { kind: "loading" }
  | { kind: "anon" }
  | { kind: "signed-in"; email: string; role: string | null };

export type Notice = { tone: "ok" | "warn" | "err"; text: string } | null;

const store = {
  get: (k: string) => { try { return localStorage.getItem(k); } catch { return null; } },
  set: (k: string, v: string) => { try { localStorage.setItem(k, v); } catch { /* private mode */ } },
  del: (k: string) => { try { localStorage.removeItem(k); } catch { /* private mode */ } },
};

async function me(token: string): Promise<{ email: string; role: string | null } | null> {
  const r = await fetch("/api/me", { headers: { authorization: `Bearer ${token}` } });
  return r.ok ? r.json() : null;
}

async function completePending(id: string, token: string) {
  const r = await fetch(`/api/chat/pending/${id}/complete`, { method: "POST", headers: { authorization: `Bearer ${token}` } });
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw Object.assign(new Error(data.detail || "That request couldn't be completed."), { status: r.status });
  return data as { ticket_number: number; title: string; photos_attached?: number };
}

/** Sign-in state for the chat, and finishing a sign-in when the page was opened from the emailed link. */
export function useMember() {
  const [member, setMember] = useState<Member>(import.meta.env.DEV ? { kind: "anon" } : { kind: "loading" });
  const [notice, setNotice] = useState<Notice>(null);
  const [needEmail, setNeedEmail] = useState(false);   // opened the link on another device/browser

  useEffect(() => {
    if (import.meta.env.DEV) return;
    let unsub = () => {};
    firebaseAuth().then((auth) => {
      unsub = onIdTokenChanged(auth, async (user) => {
        if (!user) { setToken(null); setMember({ kind: "anon" }); return; }
        const token = await user.getIdToken();
        setToken(token);
        const who = await me(token);
        setMember({ kind: "signed-in", email: user.email ?? "", role: who?.role ?? null });
      });
      if (isSignInWithEmailLink(auth, location.href)) {
        const email = store.get(EMAIL_KEY);
        if (email) finish(email); else setNeedEmail(true);
      }
    }, () => setMember({ kind: "anon" }));
    const t = setInterval(() => currentAuth()?.currentUser?.getIdToken(true), 45 * 60 * 1000);
    return () => { unsub(); clearInterval(t); };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  async function finish(email: string) {
    setNeedEmail(false);
    const auth = await firebaseAuth();
    const url = new URL(location.href);
    const pending = url.searchParams.get("pending") || store.get(PENDING_KEY);
    try {
      const cred = await signInWithEmailLink(auth, email.trim(), location.href);
      history.replaceState(null, "", "/");
      store.del(EMAIL_KEY);
      const token = await cred.user.getIdToken();
      const who = await me(token);
      if (!who?.role) {
        setNotice({ tone: "warn", text: `We couldn't find ${email} on the LHCA member list. If you're a member, ask CAM (admin@camanagers.com) to add this address. You can keep chatting without signing in.` });
        await signOut(auth);
        return;
      }
      const kind = STAFF.includes(who.role) ? "LHCA team member" : "LHCA member";
      if (pending) {
        store.del(PENDING_KEY);
        try {
          const r = await completePending(pending, token);
          setNotice({ tone: "ok", text: `You're signed in as an ${kind}. Report #${r.ticket_number} (“${r.title}”) has been filed and the board and staff have been notified.` });
        } catch (e) {
          setNotice({ tone: "warn", text: `You're signed in as an ${kind}, but your report couldn't be filed: ${(e as Error).message}` });
        }
      } else {
        setNotice({ tone: "ok", text: `You're signed in as an ${kind}. You can now report issues and see member-only information.` });
      }
    } catch (e) {
      const code = (e as { code?: string }).code ?? "";
      history.replaceState(null, "", "/");
      if (code === "auth/invalid-action-code" || code === "auth/expired-action-code") {
        setNotice({ tone: "err", text: "That sign-in link has expired or was already used. Ask for a new one." });
      } else if (code === "auth/invalid-email") {
        setNotice({ tone: "err", text: "That email doesn't match the one the link was sent to." });
      } else {
        reportError("member-sign-in", e, code || "finish");
        setNotice({ tone: "err", text: "Sign-in didn't complete. Please try again." });
      }
    }
  }

  const signOutNow = async () => { const a = currentAuth(); if (a) await signOut(a); setNotice(null); };
  return { member, notice, setNotice, needEmail, finish, signOut: signOutNow };
}

/** "Confirm you're a member" card: sends a sign-in link to the address given. */
export function SignInCard({ pendingId, title, compact }: { pendingId?: string; title?: string | null; compact?: boolean }) {
  const [email, setEmail] = useState(store.get(EMAIL_KEY) ?? "");
  const [state, setState] = useState<"idle" | "sending" | "sent" | "error">("idle");
  const [err, setErr] = useState("");
  const send = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(email.trim())) { setErr("Enter your email address."); setState("error"); return; }
    setState("sending");
    try {
      const auth = await firebaseAuth();
      const url = `${location.origin}/?signin=1${pendingId ? `&pending=${pendingId}` : ""}`;
      await sendSignInLinkToEmail(auth, email.trim(), { url, handleCodeInApp: true });
      store.set(EMAIL_KEY, email.trim());
      if (pendingId) store.set(PENDING_KEY, pendingId);
      setState("sent");
    } catch (e2) {
      const code = (e2 as { code?: string }).code ?? "";
      if (code !== "auth/invalid-email") reportError("member-sign-in", e2, code || "send");
      setErr(code === "auth/invalid-email" ? "That doesn't look like an email address."
        : code === "auth/quota-exceeded" ? "Too many sign-in emails today. Please try again tomorrow."
        : "The email couldn't be sent. Please try again.");
      setState("error");
    }
  };
  if (state === "sent") return (
    <div className={`signin-card sent${compact ? " compact" : ""}`} role="status">
      <b>Check your email</b>
      <p>We sent a sign-in link to <b>{email.trim()}</b>. Open it on this phone or computer{pendingId ? " and your report will be filed" : ""}. It works if that address is on the LHCA member list.</p>
      <button className="linkish small" onClick={() => setState("idle")}>Use a different email</button>
    </div>
  );
  return (
    <form className={`signin-card${compact ? " compact" : ""}`} onSubmit={send}>
      <b>{pendingId ? "Confirm you're an LHCA member to file this report" : "Member sign-in"}</b>
      {title && <p className="small muted">Report: “{title}”</p>}
      <p className="small">Use the email address the association has on file for your household. No password: we'll email you a link.</p>
      <div className="row">
        <input type="email" inputMode="email" autoComplete="email" autoCapitalize="none" spellCheck={false}
          placeholder="you@example.com" value={email} onChange={(e) => setEmail(e.target.value)} aria-label="Email address" />
        <button className="btn primary" disabled={state === "sending"}>{state === "sending" ? "Sending…" : "Email me a link"}</button>
      </div>
      {state === "error" && <p className="small err-text" role="alert">{err}</p>}
    </form>
  );
}

/** Shown when the link was opened in a different browser than the one that asked for it. */
export function ConfirmEmail({ onDone }: { onDone: (email: string) => void }) {
  const [email, setEmail] = useState("");
  return (
    <form className="signin-card" onSubmit={(e) => { e.preventDefault(); if (email.trim()) onDone(email); }}>
      <b>Finish signing in</b>
      <p className="small">For security, confirm the email address the link was sent to.</p>
      <div className="row">
        <input type="email" inputMode="email" autoComplete="email" autoCapitalize="none" autoFocus value={email}
          onChange={(e) => setEmail(e.target.value)} placeholder="you@example.com" aria-label="Email address" />
        <button className="btn primary">Sign in</button>
      </div>
    </form>
  );
}
