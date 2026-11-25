// Shared Firebase setup for the admin console (Google sign-in), the resident chat (passwordless email
// link for members) and App Check (proves chat requests come from this site, not a script). On Firebase
// Hosting the web config comes from /__/firebase/init.json, so no keys are in the build.

import { initializeApp, type FirebaseApp } from "firebase/app";
import { getAuth, type Auth } from "firebase/auth";
import { getToken as getAppCheckToken, initializeAppCheck, ReCaptchaEnterpriseProvider, type AppCheck } from "firebase/app-check";

let app: Promise<FirebaseApp> | null = null;
let auth: Auth | null = null;

function firebaseApp(): Promise<FirebaseApp> {
  if (app) return app;
  app = (async () => {
    const res = await fetch("/__/firebase/init.json");
    if (!res.ok) throw new Error("Sign-in isn't available here (Firebase config not found).");
    const config = await res.json();
    // Sign in through this site's own /__/auth/handler instead of lhca-cypress.firebaseapp.com.
    // Browsers that partition third-party storage (Safari, iOS, Firefox, Brave, Chrome incognito) break
    // the cross-domain handshake with "missing initial state". Needs this domain's handler URL on the
    // OAuth client's authorized redirect URIs.
    if (import.meta.env.VITE_AUTH_SAME_DOMAIN === "1") config.authDomain = location.host;
    return initializeApp(config);
  })();
  app.catch(() => { app = null; });
  return app;
}

export async function firebaseAuth(): Promise<Auth> {
  if (auth) return auth;
  auth = getAuth(await firebaseApp());
  return auth;
}

export function currentAuth(): Auth | null {
  return auth;
}

let appCheck: Promise<AppCheck> | null = null;

/** The App Check header for chat requests, or nothing if App Check isn't set up or can't load (an ad
 *  blocker, say). The API decides what a missing token means; signed-in members are never blocked. */
export async function appCheckHeaders(): Promise<Record<string, string>> {
  const siteKey = import.meta.env.VITE_RECAPTCHA_SITE_KEY;
  if (!siteKey) return {};
  try {
    appCheck ??= firebaseApp().then((a) => initializeAppCheck(a, { provider: new ReCaptchaEnterpriseProvider(siteKey), isTokenAutoRefreshEnabled: true }));
    const timeout = new Promise<never>((_, bad) => setTimeout(() => bad(new Error("App Check timed out")), 5000));
    const { token } = await Promise.race([appCheck.then((ac) => getAppCheckToken(ac)), timeout]);
    return { "x-firebase-appcheck": token };
  } catch {
    return {};
  }
}
