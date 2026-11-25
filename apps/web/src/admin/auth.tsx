// Firebase Google sign-in for the admin console.
// On Firebase Hosting the web config comes from /__/firebase/init.json (no keys in the build).
// In local dev (`npm run dev`) the API runs with CYPRESS_DEV_USER, so sign-in is skipped.

import { useEffect, useState, type ReactNode } from "react";
import { GoogleAuthProvider, onIdTokenChanged, signInWithPopup, signOut, type User } from "firebase/auth";
import { setToken } from "./api";
import { reportError } from "../lib/report";
import { currentAuth, firebaseAuth } from "../lib/firebase";

type Me = { email: string; role: string | null };
type State =
  | { kind: "loading" }
  | { kind: "signed-out" }
  | { kind: "no-access"; email: string }
  | { kind: "ready"; me: Me }
  | { kind: "error"; message: string };

async function whoami(): Promise<Me | null> {
  const r = await fetch("/api/me", { headers: authHeader() });
  return r.ok ? r.json() : null;
}

let current: string | null = null;
const authHeader = (): Record<string, string> => (current ? { authorization: `Bearer ${current}` } : {});

function friendly(code: string, e: unknown): string {
  if (code === "auth/popup-blocked") return "Your browser blocked the sign-in window. Allow pop-ups for this site and try again.";
  if (code === "auth/network-request-failed") return "Sign-in couldn't reach Google. Check your connection and try again.";
  if (/missing initial state/i.test(String((e as Error)?.message))) {
    return "Your browser's privacy settings interrupted the sign-in. Try again, or use Chrome on a computer.";
  }
  return String((e as Error)?.message ?? e);
}

export function signOutNow() {
  const a = currentAuth();
  if (a) signOut(a);
}

export function AuthGate({ children }: { children: (me: Me) => ReactNode }) {
  const [state, setState] = useState<State>(import.meta.env.DEV ? { kind: "ready", me: { email: "dev", role: "admin" } } : { kind: "loading" });

  useEffect(() => {
    if (import.meta.env.DEV) return;
    let unsub = () => {};
    firebaseAuth().then((a) => {
      unsub = onIdTokenChanged(a, async (user: User | null) => {
        if (!user) { current = null; setToken(null); setState({ kind: "signed-out" }); return; }
        current = await user.getIdToken();
        setToken(current);
        const me = await whoami();
        if (me && ["admin", "board", "staff", "security"].includes(me.role ?? "")) setState({ kind: "ready", me });
        else setState({ kind: "no-access", email: user.email ?? "" });
      });
    }, (e) => { reportError("sign-in", e, "init"); setState({ kind: "error", message: String(e.message ?? e) }); });
    // Firebase ID tokens last an hour; refresh before the API starts rejecting them.
    const t = setInterval(() => currentAuth()?.currentUser?.getIdToken(true), 45 * 60 * 1000);
    return () => { unsub(); clearInterval(t); };
  }, []);

  const signIn = async () => {
    try { await signInWithPopup(await firebaseAuth(), new GoogleAuthProvider()); }
    catch (e) {
      const code = (e as { code?: string }).code ?? "";
      // Closing the popup isn't an error worth logging or showing.
      if (code === "auth/popup-closed-by-user" || code === "auth/cancelled-popup-request") return;
      reportError("sign-in", e, code || "popup");
      setState({ kind: "error", message: friendly(code, e) });
    }
  };

  if (state.kind === "ready") return <>{children(state.me)}</>;
  return (
    <div className="gate">
      <div className="panel">
        <div className="brand"><img className="mark" src="/logo-256.png" alt="" width={36} height={36} /><div><b>Cypress</b><span>LHCA admin</span></div></div>
        {state.kind === "loading" && <p className="muted">Checking sign-in…</p>}
        {state.kind === "signed-out" && <>
          <p>Cypress admins, staff and security sign in with their Google account.</p>
          <button className="btn primary" onClick={signIn}>Sign in with Google</button>
        </>}
        {state.kind === "no-access" && <>
          <p>You're signed in as <b>{state.email}</b>, but this account doesn't have admin access yet. Ask an LHCA admin to grant it.</p>
          <button className="btn" onClick={signOutNow}>Sign out</button>
        </>}
        {state.kind === "error" && <>
          <div className="error">{state.message}</div>
          <button className="btn" onClick={signIn}>Try again</button>
        </>}
      </div>
    </div>
  );
}
