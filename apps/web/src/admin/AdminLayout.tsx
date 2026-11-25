import { useEffect, useState } from "react";
import { Navigate, NavLink, Outlet, useLocation, useOutletContext } from "react-router-dom";
import { AuthGate, signOutNow } from "./auth";

const RANK: Record<string, number> = { staff: 1, security: 1, board: 2, admin: 3 };
const rank = (role: string | null) => (import.meta.env.DEV ? 3 : RANK[role ?? ""] ?? 0);

// Items default to admin-only; `min: 1` opens them to staff and security.
const NAV: { group: string; items: { to?: string; label: string; min?: number }[] }[] = [
  { group: "Content", items: [{ to: "/admin/sets", label: "Document sets" }, { to: "/admin/jobs", label: "Ingest runs" }, { to: "/admin/checks", label: "Content checks" }] },
  { group: "Agent", items: [{ to: "/admin/conversations", label: "Conversations" }, { to: "/admin/models", label: "Models & spend" }, { to: "/admin/prompts", label: "Prompts" }, { to: "/admin/evals", label: "Evals" }, { label: "Analytics" }] },
  { group: "Operations", items: [{ to: "/admin/tickets", label: "Tickets", min: 1 }, { label: "Notices" }, { label: "Calendar" }, { label: "Access exceptions" }] },
  { group: "People", items: [{ to: "/admin/org", label: "Board & contacts" }, { to: "/admin/people", label: "Users & roles" }, { to: "/admin/members", label: "Members" }, { to: "/admin/audit", label: "Audit log" }] },
];

export default function AdminLayout() {
  return <AuthGate>{(me) => <Shell email={me.email} role={me.role} />}</AuthGate>;
}

function Shell({ email, role }: { email: string; role: string | null }) {
  const [open, setOpen] = useState(false);
  const { pathname } = useLocation();
  useEffect(() => setOpen(false), [pathname]);   // close the phone menu after navigating
  return (
    <div className="shell">
      <aside className={`side${open ? " open" : ""}`}>
        <div className="side-top">
          <div className="brand">
            <img className="mark" src="/logo-256.png" alt="" width={36} height={36} />
            <div><b>Cypress</b><span>LHCA admin</span></div>
          </div>
          <button className="btn ghost menu-btn" aria-expanded={open} aria-controls="admin-nav" onClick={() => setOpen((o) => !o)}>
            {open ? "Close" : "Menu"}
          </button>
        </div>
        <nav id="admin-nav">
          {NAV.filter((g) => g.items.some((i) => rank(role) >= (i.min ?? 3))).map((g) => (
            <div key={g.group} className="group">
              <div className="gl">{g.group}</div>
              {g.items.filter((i) => rank(role) >= (i.min ?? 3)).map((i) => i.to
                ? <NavLink key={i.label} to={i.to}>{i.label}</NavLink>
                : <span key={i.label} className="soon" title="Coming during build week">{i.label}</span>)}
            </div>
          ))}
          <a className="nav-chat" href="/">Open resident chat ↗</a>
        </nav>
        <div className="who small">
          {import.meta.env.DEV ? "Dev mode · signed in as admin" : <><span className="break">{email}</span><br />{role} · <button className="linkish" onClick={signOutNow}>Sign out</button></>}
        </div>
      </aside>
      <main className="main"><Outlet context={{ role }} /></main>
    </div>
  );
}

/** /admin lands on content for admins, on tickets for staff and security. */
export function AdminHome() {
  const { role } = useOutletContext<{ role: string | null }>();
  return <Navigate to={rank(role) >= 3 ? "sets" : "tickets"} replace />;
}
