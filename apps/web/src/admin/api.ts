import { reportError } from "../lib/report";
// Thin client for the Cypress admin API. In dev, Vite proxies /api to the FastAPI server,
// which runs with CYPRESS_DEV_USER so no sign-in is needed. Firebase sign-in plugs in here
// later by setting the bearer token.

let token: string | null = null;
export const setToken = (t: string | null) => (token = t);
export const getToken = () => token;

export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
  }
}

async function req<T>(method: string, path: string, body?: unknown): Promise<T> {
  const res = await fetch(`/api/admin${path}`, {
    method,
    headers: {
      ...(body !== undefined ? { "content-type": "application/json" } : {}),
      ...(token ? { authorization: `Bearer ${token}` } : {}),
    },
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
  if (res.status === 204) return undefined as T;
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const err = new ApiError(res.status, describe(data.detail) || res.statusText);
    if (res.status >= 500) reportError("api", err, `${res.status} ${method} ${path.split("?")[0]}`);
    throw err;
  }
  return data as T;
}

function describe(detail: unknown): string {
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail))
    return detail.map((d) => `${(d.loc ?? []).filter((x: unknown) => x !== "body").join(".")}: ${d.msg}`).join("; ");
  return "";
}

export type Visibility = "public" | "members" | "staff" | "security" | "board";
// Mirrors services/agent/cypress/roles.py.
export const VISIBILITIES: { value: Visibility; label: string; who: string }[] = [
  { value: "public", label: "Public", who: "Anyone, including anonymous residents" },
  { value: "members", label: "Members", who: "Signed-in members, staff, security and board" },
  { value: "staff", label: "Staff", who: "Park staff and board" },
  { value: "security", label: "Security", who: "Park security and board" },
  { value: "board", label: "Board only", who: "Board members only" },
];
export const DOC_TYPES = ["bylaws", "rules", "deed", "court_record", "faq", "history", "newsletter", "minutes", "general"] as const;
export type DocType = (typeof DOC_TYPES)[number];
export type JobStatus = "queued" | "running" | "succeeded" | "failed" | "cancelled";

export interface SetSettings { chunk_tokens: number; chunk_overlap: number; top_k: number; min_score: number; rerank: boolean }
export interface JobBrief { id: number; status: JobStatus; created_at: string; finished_at: string | null; error: string | null; stats?: Record<string, number> }

export interface DocSet {
  id: number; slug: string; name: string; description: string; visibility: Visibility; doc_type: DocType;
  authority_rank: number; enabled: boolean; settings: SetSettings; created_by: string; created_at: string; updated_at: string;
  source_count: number; document_count: number; chunk_count: number; last_job: JobBrief | null;
}

export interface WebConfig { max_depth: number; max_pages: number; allow_domains: string[]; include: string[]; exclude: string[]; follow_pdfs: boolean; respect_robots: boolean }
export interface FileConfig { ocr: "auto" | "always" | "never" }

export interface Source {
  id: number; set_id: number; kind: "web" | "pdf" | "upload"; url: string | null; label: string;
  config: Partial<WebConfig & FileConfig>; schedule: "manual" | "daily" | "weekly"; enabled: boolean;
  next_check?: string | null;   // next nightly re-crawl for scheduled sources
  created_by: string; created_at: string; last_job: JobBrief | null;
}

export interface Job {
  id: number; set_id: number; source_id: number | null; kind: string; status: JobStatus; triggered_by: string;
  created_at: string; started_at: string | null; finished_at: string | null; stats: Record<string, number>; error: string | null;
  source_url: string | null; source_label: string | null; set_name: string;
}

export interface DocRow { id: number; uri: string; title: string; mime: string; status: string; version: number; fetched_at: string; chars: number; chunks: number }
export interface Doc extends DocRow { set_id: number; markdown: string; meta: Record<string, unknown>; versions: { version: number; content_hash: string; fetched_at: string }[] }
export interface AuditRow { id: number; at: string; actor: string; action: string; entity: string; entity_id: string | null; detail: Record<string, unknown> }

export interface TurnBrief {
  id: number; public_id: string; at: string; channel: string; user_role: string | null; user_email: string | null;
  question: string; intent: string | null; answer_preview: string | null; latency_ms: number | null; failed: boolean;
  rating: 1 | -1 | null; feedback_comment: string | null; feedback_at: string | null;
}
export interface TurnTotals { turns: number; up: number; down: number; failed: number; p50_ms: number | null }
export interface Turn extends Omit<TurnBrief, "answer_preview" | "failed"> {
  thread_id: string; answer: string | null; error: string | null; feedback_by: string | null; trace_url?: string | null;
  citations: { n: number; title: string; uri: string; section: string; set: string }[];
  retrieval: { queries?: string[]; grade?: { sufficient?: boolean; better_query?: string };
    passages?: { title: string; section: string; set_slug: string; score: number }[];
    tool_calls?: { tool: string; args: Record<string, unknown>; ok: boolean }[] };
  models: Record<string, string>;
  thread: { id: number; at: string; question: string; answer: string | null; rating: 1 | -1 | null }[];
}
export type RatingFilter = "all" | "down" | "up" | "rated" | "unrated";

export interface OrgSettings { board_members: string[]; board_contact: { name: string; email: string }; board_members_source?: "manual" | "terms"; chat_suggestions?: string[];
  uploads?: { videos: boolean; max_video_mb: number } }

export type TicketStatus = "new" | "acknowledged" | "in_progress" | "resolved" | "closed";
export type TicketPriority = "low" | "normal" | "high" | "urgent";
export interface Ticket {
  id: number; created_at: string; updated_at: string; status: TicketStatus; category: string; priority: TicketPriority;
  title: string; description: string; location: string | null; channel: string; reporter_role: string | null;
  reporter_contact?: string | null; assignee: string | null;
}
export interface TicketEvent { at: string; actor: string; kind: string; body: string | null; data: Record<string, unknown> }
export interface LegalHold { set_at: string; set_by: string; reason: string }
export interface TicketDetail extends Ticket {
  events: TicketEvent[]; photos?: string[]; videos?: string[];
  removed?: { photos: number; videos: number; at: string | null };
  hold?: LegalHold | null; hold_all?: boolean; can_hold?: boolean; keep_days?: number;
  case?: CaseFile | null;
}
export interface CasesSettings { full_roles: ("board" | "admin" | "security")[]; vision: boolean; draft_letters: boolean; policy_reasoning: "linear" | "tot" }
export interface CaseVote { status: "open" | "passed" | "failed" | "expired" | "cancelled"; needed: number; seats: number;
  opened_at: string; closes_at: string; yes: number; no: number; votes: { name: string; vote: "yes" | "no"; at: string; via: string }[];
  can_vote: boolean; my_vote: "yes" | "no" | null }
export interface VoteReady { seated: number; on_slack: number; needed: number; seats: number }
export interface VoteSettings { rule: "majority" | "count"; count: number; seats: number; days: number; auto_start: boolean }
export type CaseStatus = "queued" | "running" | "awaiting_board" | "approved" | "dismissed" | "failed";
export interface CaseHousehold { id: number | null; name: string; address: string | null; status: string | null;
  confidence: "high" | "medium" | "low"; basis: string; people?: string[]; emails?: string[]; phones?: string[];
  vehicles?: { plate: string; state?: string; car: string; tag?: string | null }[] }
export interface CaseLetter { subject: string; body: string }
export interface CaseFile {
  status: CaseStatus; matched?: boolean; full: boolean; created_at: string; finished_at: string | null; decided_at: string | null;
  report?: Record<string, string | string[] | null>; plan?: { agents: string[]; reason: string } | null;
  findings?: {
    plate_reader?: { photos_read: number; plates: { text: string; state: string; confidence: string }[]; vehicles: Record<string, string>[]; notes: string };
    matcher?: { lookups: { tool: string; args: Record<string, unknown> }[]; summary: string; unmatched: string[] };
    history?: { earlier: { ticket_id: number; title: string; status: string; at: string }[] };
    policy?: { rules: { rule: string; source: number; why: string }[]; process: string; confidence: string;
      sources: { n: number; title: string; section: string; uri: string }[] };
  };
  profile?: { summary: string; assessment: string; next_step: string; households: CaseHousehold[] } | null;
  letter?: CaseLetter | null; decision?: { action: string; note: string | null } | null; decided_by?: string | null;
  vote?: CaseVote | null; vote_rule?: VoteReady; is_admin?: boolean;
  error?: string | null; trace_url?: string | null;
}
export interface RetentionSettings { unattached_hours: number; closed_ticket_days: number; lookup_log_days: number; hold_all: boolean; hold_all_reason: string | null }
export interface RetentionView {
  settings: RetentionSettings;
  stored: { photos: number; videos: number; bytes: number; held: number };
  holds: (LegalHold & { ticket_id: number; title: string })[];
  last_run: { at: string; summary: { photos: number; videos: number; unattached: number; lookups: number; failed: number; bytes: number; hold_all: boolean } } | null;
  next_run: string;
}

export type Role = "admin" | "board" | "staff" | "security" | "member";
// Mirrors services/agent/cypress/roles.py.
export const ROLE_INFO: Record<Role, string> = {
  admin: "Runs the Cypress console: content, models, prompts, members, users. Sees what the board sees. Can be a board member, CAM, or another volunteer",
  board: "Everything Cypress knows, including board-only documents; works tickets",
  staff: "Staff documents; works tickets in the console and Slack",
  security: "Security documents; works tickets in the console and Slack",
  member: "Member-only documents; no console access",
};
export interface Term {
  title: string; seat: string | null; how: "elected" | "appointed"; start_date: string; end_date: string;
  ended_on: string | null; end_reason: "resigned" | "removed" | "other" | null; notes: string | null;
}
export const OFFICER_TITLES = ["President", "Vice President", "Secretary", "Treasurer", "Director"];
export interface Person {
  id: number; email: string; name: string | null; role: Role | null; active: boolean;
  effective_role: Role | null; on_board: boolean; board_title: string | null; term_end: string | null;
  phone: string | null; responsibilities: string | null; terms: Term[];
  slack_user_id: string | null; slack_name: string | null; firebase_uid: string | null; notes: string | null;
  last_seen_web: string | null; last_seen_slack: string | null; groups: number[];
  created_by: string; created_at: string; updated_by: string | null; updated_at: string;
}
export interface PersonIn {
  email: string; name: string | null; role: Role | null; active: boolean; slack_user_id: string | null; notes: string | null;
  phone: string | null; responsibilities: string | null; groups: number[]; terms: Term[];
}
export interface Group { id: number; name: string; description: string | null; ticket_categories: string[]; members: number[] }
export interface GroupIn { name: string; description: string | null; ticket_categories: string[]; members: number[] }
export interface PeopleData { people: Person[]; groups: Group[]; roles: Role[]; categories: string[]; slack_default_role: Role | null; slack_connected: boolean }
export interface SlackSync { linked: string[]; workspace_members: number; unlisted: { slack_user_id: string; name: string | null; email: string | null; guest: boolean }[] }

// ---------- models & spend (admin only) ----------
export type Audience = "all" | "residents" | "team";
export const AUDIENCES: { value: Audience; label: string; who: string }[] = [
  { value: "all", label: "Everyone", who: "Default for every caller and for background work" },
  { value: "residents", label: "Residents", who: "Anonymous public chat" },
  { value: "team", label: "Team", who: "Signed-in staff, security, board and admin (web and Slack)" },
];
export interface LlmModel {
  id: string; kind: "chat" | "embed" | "rerank"; provider: "gemini" | "claude" | "maas" | "vertex_search" | "local"; name: string; region: string; label: string;
  thinking: string | null; input_per_m: number | null; cached_per_m: number | null; output_per_m: number | null; per_call_usd: number | null;
  price_source: string | null; enabled: boolean; note: string | null; updated_by?: string; updated_at?: string;
  priced?: boolean; calls_30d?: number; cost_30d?: number; unpriced_30d?: number; last_used?: string | null; assigned_to?: string[];
}
export interface AgentConfig { id: number; node: string; audience: Audience; version: number; model_id: string | null; temperature: number | null;
  max_tokens: number | null; prompt: string | null; note: string | null; created_by: string; created_at: string; calls?: number; cost?: number; p50_ms?: number | null }
export interface Resolved { model_id: string; temperature: number | null; max_tokens: number | null; prompt: string | null; id: number | null; audience: string }
export interface NodeInfo { node: string; description: string; default: string; gemini_only: boolean;
  configs: Record<Audience, AgentConfig | null>; resolved: Record<Audience, Resolved>; effective: Record<Audience, string> }
export interface Runtime { fallback_model: string; hedge_s: number; stream_hedge_s: number; timeout_s: number }
export interface Budget { monthly_usd: number; guard: boolean; guard_model: string; alert_pct: number }
export interface BudgetStatus extends Budget { month_to_date: number; projected: number; day: number; days_in_month: number; guard_active: boolean; alert: boolean }
export interface ModelsData { nodes: NodeInfo[]; models: LlmModel[]; runtime: Runtime; budget: BudgetStatus }
export interface ConfigIn { node: string; audience: Audience; model_id: string | null; temperature: number | null; max_tokens: number | null; note: string | null }
export interface Probe { ok: boolean | null; kind?: string; error?: string; latency_ms?: number; reply?: string }
export interface Usage {
  days: number;
  totals: { calls: number; cost: number; turns: number; turn_cost: number; cost_per_turn: number | null; input_tokens: number; output_tokens: number;
    unpriced: number; estimated: number; failed: number; hedge_cost: number; fallback_cost: number; probe_cost: number };
  daily: { day: string; model_id: string; calls: number; cost: number }[];
  by_node: { node: string; model_id: string; calls: number; cost: number; input_tokens: number; cached_tokens: number; output_tokens: number;
    p50_ms: number | null; p95_ms: number | null; failed: number; backups: number }[];
  by_channel: { channel: string; role: string; turns: number; cost: number }[];
  top_turns: { turn_id: string; at: string; channel: string | null; calls: number; cost: number; intent: string | null; interaction_id: number | null }[];
  what_if: { node: string; actual: number; tokens: { input_tokens: number; output_tokens: number };
    options: { model_id: string; label: string; enabled: boolean; gemini_ok: boolean; cost: number }[] }[];
  budget: BudgetStatus;
}

// ---------- members (admin only) ----------
export interface MemberEmail { email: string; name: string | null; last_signed_in?: string | null }
export interface Household { id: number; name: string; address: string | null; account_ref: string | null; status: "active" | "inactive";
  notes: string | null; source: string | null; created_at: string; updated_at: string; emails: MemberEmail[]; phones?: string[];
  people?: string[]; member_type?: string | null; row_color?: string | null }
export interface HouseholdIn { name: string; address: string | null; account_ref: string | null; status: "active" | "inactive"; notes: string | null; emails: MemberEmail[] }
export interface MemberAuth { require_for_reports: boolean; nudge_after_turns: number; pending_hours: number }
export interface MembersData { households: Household[]; total: number; counts: { active: number; households: number; emails: number; signed_in: number }; settings: MemberAuth }
export const IMPORT_FIELDS: { key: string; label: string }[] = [
  { key: "household", label: "Household name" }, { key: "address", label: "Address" }, { key: "account_ref", label: "Account / member id" },
  { key: "email1", label: "Email" }, { key: "email2", label: "Second email" }, { key: "name1", label: "Name for first email" },
  { key: "name2", label: "Name for second email" }, { key: "status", label: "Status (active / lapsed)" },
  { key: "phone1", label: "Phone" }, { key: "phone2", label: "Second phone" },
];
export type TabKind = "households" | "former" | "golf_carts" | "vendors" | "ignore";
export interface WorkbookChange { level: "alert" | "info"; what: string }
export interface WorkbookColor { hex: string; name: string; meaning: string | null; aliases: string[]; known: boolean; fills: number; text: number; where: Record<string, number> }
export interface WorkbookPreview {
  tabs: { name: string; kind: TabKind; rows: number; overridden: boolean; columns: Record<string, string | null> }[];
  counts: { households: number; inactive: number; people: number; vehicles: number; gate_cards: number; golf_carts: number; vendor_cards: number; golf_carts_unmatched: number; contacts?: number; emails?: number };
  colors: WorkbookColor[]; changes: WorkbookChange[]; file_name?: string; modified?: string;
}
export interface LegendColor { name: string; meaning: string | null; effect: string | null; new?: boolean; aliases?: string[]; where?: string[]; count?: number }
export interface MemberTypes { types: { key: string; label: string; notes?: string | null }[]; default: string }
export interface SyncRun { id: number; at: string; trigger: string; status: "ok" | "warning" | "failed" | "skipped";
  stats: { households?: { created: number; updated: number; deactivated: number }; vehicles?: { created: number; updated: number; removed: number };
    golf_carts?: { imported: number; unmatched: number }; gate_cards?: { member: number; vendor: number }; changes?: WorkbookChange[]; warnings?: string[]; file?: string };
  error: string | null }
export interface SyncView {
  settings: { enabled: boolean; file_id: string | null; file_url: string | null; file_name: string | null;
    contacts_file_url?: string | null; contacts_file_name?: string | null; replace: boolean; min_fraction: number; tab_kinds: Record<string, TabKind> };
  service_account: string; runs: SyncRun[]; colors: Record<string, LegendColor>; types: MemberTypes;
}
export interface Vehicle { id: number; plate: string; state: string; make: string | null; model: string | null; color: string | null;
  year: number | null; tag: string | null; notes: string | null; household_id: number | null; household: string | null;
  address: string | null; status: "active" | "inactive" | null; updated_at: string }
export interface VehiclesData { vehicles: Vehicle[]; total: number; counts: { vehicles: number; unmatched: number } }
export const VEHICLE_FIELDS: { key: string; label: string }[] = [
  { key: "plate", label: "License plate" }, { key: "state", label: "Plate state" }, { key: "make", label: "Make" },
  { key: "model", label: "Model" }, { key: "color", label: "Color" }, { key: "year", label: "Year" },
  { key: "tag", label: "Sticker / permit / gate tag" }, { key: "account_ref", label: "Account / member id" },
  { key: "address", label: "Address" }, { key: "owner", label: "Owner name (not imported)" }, { key: "notes", label: "Notes" },
];
export interface VehiclePreview { headers: string[]; mapping: Record<string, string | null>; rows: number; vehicles: number;
  matched?: number; unmatched?: number; sample: { line: number; plate: string; state: string; make: string | null; model: string | null;
  color: string | null; year: number | null; household: string | null }[]; problems: string[]; more_problems?: number }
export interface ImportPreview { headers: string[]; mapping: Record<string, string | null>; rows: number; households: number; emails?: number;
  inactive?: number; no_email?: number; sample: { line: number; name: string; address: string | null; account_ref: string | null; status: string; emails: MemberEmail[] }[];
  problems: string[]; more_problems?: number }
export interface SlackChannel { channel_id: string; name: string | null; private: boolean; members_known: boolean;
  members: { slack_user_id: string; name: string | null; role: Role | null; guest: boolean }[];
  auto_role: Role | null; max_role: string | null; effective_role: Role | null; note: string | null }

async function upload<T>(path: string, form: FormData): Promise<T> {
  const res = await fetch(`/api/admin${path}`, { method: "POST", body: form, headers: token ? { authorization: `Bearer ${token}` } : {} });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new ApiError(res.status, describe(data.detail) || res.statusText);
  return data as T;
}

// ---------- prompts (admin only) ----------
export interface PromptInfo { key: string; description: string; builtin: string; variables: string[]; guardrail: string | null;
  configs: Record<Audience, AgentConfig | null>; effective: Record<Audience, string>; tryable: boolean }
export interface PromptsData { prompts: PromptInfo[]; variables: Record<string, string> }
export interface TryResult { answer: string; citations: { n: number; title: string; uri: string }[]; intent: string | null; cost: number; calls: number; steps: string | null }

// ---------- evals (admin only) ----------
export interface EvalExpect { intent?: string; must_include?: string[][]; must_not_include?: string[]; cite?: string[]; tool?: string; ideal?: string }
export interface EvalCase { id: number; question: string; as_role: string; expect: EvalExpect; tags: string[]; notes: string | null; active: boolean; last_passed?: boolean | null }
export interface EvalCaseIn { question: string; as_role: string; expect: EvalExpect; tags: string[]; notes: string | null; active: boolean }
export interface EvalRunSummary { id: number; label: string; overrides: { prompts?: Record<string, string>; models?: Record<string, string> }; status: "running" | "done" | "cancelled";
  cases: number; started_by: string; started_at: string; finished_at: string | null; finished: number; passed: number; errors: number;
  avg_score: number | null; cost: number; p50_ms: number | null }
export interface EvalResult { run_id: number; case_id: number; status: "pending" | "done" | "error"; answer: string | null; intent: string | null;
  citations: { n: number; title: string; uri?: string }[]; tools: string[]; checks: Record<string, { ok: boolean; detail: string }>;
  judge: { score: number; verdict: string; reason: string; model: string } | null; passed: boolean | null; cost_usd: number | null;
  latency_ms: number | null; error: string | null; question: string; as_role: string; tags: string[]; expect: EvalExpect }
export interface EvalRun extends EvalRunSummary { results: EvalResult[] }
export interface SubNode { node: string; about: string; cases: number; gemini_only: boolean; current: string }
export interface SubRubric { rubric: string; pass_score: number }
export interface SubRunSummary { id: number; label: string; plan: { nodes: string[]; models: string[]; prompts?: Record<string, string> };
  status: "running" | "done" | "cancelled"; cases: number; started_by: string; started_at: string; finished_at: string | null;
  total: number; finished: number; passed: number; cost: number }
export interface SubScore { node: string; model: string; cases: number; finished: number; passed: number; errors: number;
  avg_score: number | null; cost: number; p50_ms: number | null }
export interface SubResult { run_id: number; case_key: string; title: string; node: string; model: string; status: "pending" | "done" | "error";
  output: unknown; checks: Record<string, { ok: boolean; detail: string }>; judge: { score: number; reason: string; criteria?: Record<string, number> } | null;
  passed: boolean | null; cost_usd: number | null; latency_ms: number | null; error: string | null }
export interface SubRun extends SubRunSummary { scoreboard: SubScore[]; results: SubResult[] }

// ---------- content checks (admin only) ----------
export interface ContentIssue { id: number; topic: string; severity: "high" | "medium" | "low"; a_doc_id: number | null; b_doc_id: number | null;
  a_title: string; b_title: string; a_uri: string | null; b_uri: string | null; a_says: string; b_says: string; a_excerpt: string | null;
  b_excerpt: string | null; suggestion: string | null; status: "open" | "dismissed" | "resolved"; found_at: string; last_seen_at: string;
  closed_by: string | null; closed_at: string | null; note: string | null }
export interface ContentScan { id: number; scope: string; status: string; requested_by: string; created_at: string; finished_at: string | null;
  stats: { pairs_checked?: number; conflicts?: number; new?: number; auto_resolved?: number }; error: string | null }
export interface ChecksSettings { enabled: boolean; min_similarity: number; max_pairs: number;
  neighbors: number; same_document: boolean; rules: boolean; verify: boolean;
  track_changes: boolean; summarize_changes: boolean; max_changed_docs: number }
export interface FactChange { what: string; before: string; after: string; important: boolean }
export interface DocChange {
  id: number; at: string; document_id: number; kind: "changed" | "new" | "gone"; version: number; title: string; uri: string | null;
  summary: FactChange[] | null; trivial: boolean; added: number; removed: number; diff: string | null;
  status: "new" | "reviewed"; reviewed_by: string | null; reviewed_at: string | null;
}

/** Photos need the bearer token, so fetch them as blobs and hand back object URLs. */
export async function photoUrl(id: string): Promise<string> {
  const res = await fetch(`/api/admin/uploads/${id}`, { headers: token ? { authorization: `Bearer ${token}` } : {} });
  if (!res.ok) throw new ApiError(res.status, "Photo unavailable");
  return URL.createObjectURL(await res.blob());
}

export const api = {
  contentIssues: (status: string) => req<{ issues: ContentIssue[]; counts: { open: number; high: number }; scans: ContentScan[]; settings: ChecksSettings }>("GET", `/content-issues?status=${status}`),
  contentIssueCount: () => req<{ open: number; high: number }>("GET", "/content-issues/count"),
  setIssueStatus: (id: number, status: string, note?: string) => req<{ ok: boolean }>("PUT", `/content-issues/${id}`, { status, note: note ?? null }),
  scanContent: () => req<{ scan_id: number | null }>("POST", "/content-checks/scan"),
  contentChanges: (status: string) => req<{ changes: DocChange[]; unreviewed: number }>("GET", `/content-changes?status=${status}`),
  setChangeStatus: (id: number, status: "new" | "reviewed") => req<{ ok: boolean }>("PUT", `/content-changes/${id}`, { status }),
  allChangesReviewed: () => req<{ reviewed: number }>("POST", "/content-changes/reviewed"),
  saveChecksSettings: (s: ChecksSettings) => req<ChecksSettings>("PUT", "/content-checks/settings", s),
  evals: () => req<{ cases: EvalCase[]; runs: EvalRunSummary[]; seeded: number; tags: string[] }>("GET", "/evals"),
  addEvalCase: (c: EvalCaseIn) => req<{ id: number }>("POST", "/evals/cases", c),
  saveEvalCase: (id: number, c: EvalCaseIn) => req<{ id: number }>("PUT", `/evals/cases/${id}`, c),
  removeEvalCase: (id: number) => req<void>("DELETE", `/evals/cases/${id}`),
  startEval: (b: { label: string; prompts?: Record<string, string>; models?: Record<string, string>; tag?: string | null; case_ids?: number[] }) =>
    req<{ id: number }>("POST", "/evals/runs", b),
  evalRun: (id: number) => req<EvalRun>("GET", `/evals/runs/${id}`),
  cancelEval: (id: number) => req<{ ok: boolean }>("POST", `/evals/runs/${id}/cancel`),
  subEvals: () => req<{ nodes: SubNode[]; cases: { key: string; node: string; title: string }[]; rubrics: Record<string, SubRubric>; runs: SubRunSummary[] }>("GET", "/evals/subagents"),
  saveRubric: (node: string, r: SubRubric) => req<Record<string, SubRubric>>("PUT", `/evals/subagents/rubrics/${node}`, r),
  startSubEval: (b: { label: string; nodes: string[]; models: string[] }) => req<{ id: number }>("POST", "/evals/subagents/runs", b),
  subEvalRun: (id: number) => req<SubRun>("GET", `/evals/subagents/runs/${id}`),
  cancelSubEval: (id: number) => req<{ ok: boolean }>("POST", `/evals/subagents/runs/${id}/cancel`),
  prompts: () => req<PromptsData>("GET", "/prompts"),
  savePrompt: (key: string, audience: Audience, prompt: string | null, note: string | null) =>
    req<PromptsData & { saved: AgentConfig }>("POST", "/prompts", { key, audience, prompt, note }),
  tryPrompt: (key: string, prompt: string, question: string, as_role: string) =>
    req<TryResult>("POST", "/prompts/try", { key, prompt, question, as_role }),
  members: (q: string, status: string, offset = 0) => req<MembersData>("GET", `/members?${new URLSearchParams({ q, status, offset: String(offset) })}`),
  previewImport: (file: File, mapping?: Record<string, string | null>) => {
    const f = new FormData(); f.append("file", file); if (mapping) f.append("mapping", JSON.stringify(mapping));
    return upload<ImportPreview>("/members/import/preview", f);
  },
  runImport: (file: File, mapping: Record<string, string | null>, replace: boolean) => {
    const f = new FormData(); f.append("file", file); f.append("mapping", JSON.stringify(mapping)); f.append("replace", String(replace));
    return upload<{ created: number; updated: number; emails: number; deactivated: number; problems: number }>("/members/import", f);
  },
  memberSync: () => req<SyncView>("GET", "/member-sync"),
  checkSync: (file: string) => req<WorkbookPreview & { file_id: string }>("POST", "/member-sync/check", { file }),
  saveSync: (s: { file?: string | null; contacts_file?: string | null; enabled: boolean; replace: boolean; min_fraction: number; tab_kinds: Record<string, TabKind> }) => req<SyncView>("PUT", "/member-sync", s),
  runSync: () => req<SyncView>("POST", "/member-sync/run"),
  saveLegend: (colors: Record<string, { name: string; meaning: string | null; effect: string | null }>) => req<SyncView>("PUT", "/member-colors", { colors }),
  saveTypes: (t: MemberTypes) => req<SyncView>("PUT", "/member-types", t),
  previewWorkbook: (file: File) => { const f = new FormData(); f.append("file", file); return upload<WorkbookPreview>("/members/workbook/preview", f); },
  importWorkbook: (file: File, replace: boolean) => { const f = new FormData(); f.append("file", file); f.append("replace", String(replace));
    return upload<{ stats: SyncRun["stats"]; changes: WorkbookChange[]; warnings: string[]; run: SyncRun }>("/members/workbook/import", f); },
  vehicles: (q: string, unmatched = false) => req<VehiclesData>("GET", `/vehicles?${new URLSearchParams({ q, unmatched: String(unmatched) })}`),
  previewVehicles: (file: File, mapping?: Record<string, string | null>) => {
    const f = new FormData(); f.append("file", file); if (mapping) f.append("mapping", JSON.stringify(mapping));
    return upload<VehiclePreview>("/vehicles/import/preview", f);
  },
  importVehicles: (file: File, mapping: Record<string, string | null>, replace: boolean) => {
    const f = new FormData(); f.append("file", file); f.append("mapping", JSON.stringify(mapping)); f.append("replace", String(replace));
    return upload<{ created: number; updated: number; removed: number; unmatched: number; problems: number }>("/vehicles/import", f);
  },
  removeVehicle: (id: number) => req<void>("DELETE", `/vehicles/${id}`),
  addHousehold: (h: HouseholdIn) => req<{ id: number }>("POST", "/households", h),
  saveHousehold: (id: number, h: HouseholdIn) => req<{ id: number }>("PUT", `/households/${id}`, h),
  removeHousehold: (id: number) => req<void>("DELETE", `/households/${id}`),
  saveMemberAuth: (m: MemberAuth) => req<MemberAuth>("PUT", "/member-settings", m),
  memberLookup: () => req<LookupData>("GET", "/member-lookup"),
  saveMemberLookup: (s: LookupSettings) => req<LookupData>("PUT", "/member-lookup", s),
  slackChannels: () => req<{ channels: SlackChannel[] }>("GET", "/slack-channels"),
  saveSlackChannel: (id: string, c: { name: string | null; max_role: string | null; note: string | null }) => req<{ ok: boolean }>("PUT", `/slack-channels/${id}`, c),
  models: () => req<ModelsData>("GET", "/models"),
  usage: (days: number) => req<Usage>("GET", `/usage?days=${days}`),
  saveConfig: (c: ConfigIn) => req<ModelsData & { saved: AgentConfig }>("POST", "/agent-configs", c),
  configHistory: (node: string, audience: Audience) => req<{ versions: AgentConfig[] }>("GET", `/agent-configs/${node}/${audience}`),
  restoreConfig: (id: number, note?: string) => req<ModelsData & { saved: AgentConfig }>("POST", `/agent-configs/${id}/restore`, { note: note ?? null }),
  addModel: (m: LlmModel) => req<ModelsData>("POST", "/models", m),
  saveModel: (m: LlmModel) => req<ModelsData>("PUT", `/models/${m.id}`, m),
  removeModel: (id: string) => req<ModelsData>("DELETE", `/models/${id}`),
  testModel: (id: string) => req<Probe>("POST", `/models/${id}/test`),
  repriceModel: (id: string) => req<{ repriced: number }>("POST", `/models/${id}/reprice`),
  saveLlmSettings: (runtime: Runtime, budget: Budget) => req<ModelsData>("PUT", "/llm-settings", { runtime, budget }),
  protection: (days: number) => req<ProtectionData>("GET", `/protection?days=${days}`),
  saveProtection: (p: ProtectionSettings) => req<ProtectionData>("PUT", "/protection", p),
  people: () => req<PeopleData>("GET", "/people"),
  addPerson: (p: PersonIn) => req<Person>("POST", "/people", p),
  savePerson: (id: number, p: PersonIn) => req<Person>("PUT", `/people/${id}`, p),
  removePerson: (id: number) => req<void>("DELETE", `/people/${id}`),
  slackSync: () => req<SlackSync>("POST", "/people/slack-sync"),
  saveSlackDefault: (role: Role | null) => req<{ slack_default_role: Role | null }>("PUT", "/people-settings", { slack_default_role: role }),
  addGroup: (g: GroupIn) => req<{ id: number }>("POST", "/groups", g),
  saveGroup: (id: number, g: GroupIn) => req<{ id: number }>("PUT", `/groups/${id}`, g),
  removeGroup: (id: number) => req<void>("DELETE", `/groups/${id}`),
  assignees: () => req<{ people: string[]; groups: string[] }>("GET", "/assignees"),
  tickets: (status: string) => req<Ticket[]>("GET", `/tickets?status=${status}`),
  ticket: (id: number) => req<TicketDetail>("GET", `/tickets/${id}`),
  patchTicket: (id: number, p: { status?: TicketStatus; priority?: TicketPriority; assignee?: string; note?: string }) =>
    req<TicketDetail>("PATCH", `/tickets/${id}`, p),
  commentTicket: (id: number, body: string) => req<TicketDetail>("POST", `/tickets/${id}/comments`, { body }),
  org: () => req<OrgSettings>("GET", "/org"),
  saveOrg: (o: OrgSettings) => req<OrgSettings>("PUT", "/org", o),
  videoLink: (id: string) => req<{ url: string }>("GET", `/uploads/${id}/link`),
  saveUploads: (u: { videos: boolean; max_video_mb: number }) => req<{ videos: boolean; max_video_mb: number }>("PUT", "/org/uploads", u),
  retention: () => req<RetentionView>("GET", "/retention"),
  saveRetention: (r: RetentionSettings) => req<RetentionView>("PUT", "/retention", r),
  casesSettings: () => req<CasesSettings>("GET", "/cases-settings"),
  saveCasesSettings: (s: CasesSettings) => req<CasesSettings>("PUT", "/cases-settings", s),
  openVote: (id: number) => req<{ case: CaseFile }>("POST", `/tickets/${id}/case/vote/open`),
  castVote: (id: number, vote: "yes" | "no") => req<{ case: CaseFile }>("POST", `/tickets/${id}/case/vote`, { vote }),
  cancelVote: (id: number) => req<{ case: CaseFile }>("DELETE", `/tickets/${id}/case/vote`),
  editLetter: (id: number, letter: CaseLetter) => req<{ case: CaseFile }>("PUT", `/tickets/${id}/case/letter`, letter),
  voteSettings: () => req<{ settings: VoteSettings; ready: VoteReady }>("GET", "/org/votes"),
  saveVoteSettings: (s: VoteSettings) => req<{ settings: VoteSettings; ready: VoteReady }>("PUT", "/org/votes", s),
  decideCase: (id: number, action: "approve" | "dismiss", note?: string, letter?: CaseLetter) =>
    req<{ case: CaseFile }>("PUT", `/tickets/${id}/case`, { action, note, letter }),
  setHold: (id: number, on: boolean, reason?: string) => req<{ hold: LegalHold | null }>("PUT", `/tickets/${id}/hold`, { on, reason }),
  saveSuggestions: (suggestions: string[]) => req<{ suggestions: string[] }>("PUT", "/org/suggestions", { suggestions }),
  interactions: (p: { rating: RatingFilter; q?: string; channel?: string }) =>
    req<{ items: TurnBrief[]; totals_30d: TurnTotals }>("GET", `/interactions?${new URLSearchParams(
      Object.entries(p).filter(([, v]) => v) as [string, string][])}`),
  interaction: (id: number) => req<Turn>("GET", `/interactions/${id}`),
  sets: () => req<DocSet[]>("GET", "/sets"),
  set: (id: number) => req<DocSet & { sources: Source[] }>("GET", `/sets/${id}`),
  createSet: (b: Partial<DocSet>) => req<DocSet>("POST", "/sets", b),
  updateSet: (id: number, b: Partial<DocSet>) => req<DocSet>("PATCH", `/sets/${id}`, b),
  deleteSet: (id: number) => req<void>("DELETE", `/sets/${id}`),
  createSource: (setId: number, b: { kind: string; url: string; label?: string; schedule?: string; config?: object }) =>
    req<Source>("POST", `/sets/${setId}/sources`, b),
  updateSource: (id: number, b: Partial<Source>) => req<Source>("PATCH", `/sources/${id}`, b),
  deleteSource: (id: number) => req<void>("DELETE", `/sources/${id}`),
  runSet: (id: number) => req<{ queued: Job[] }>("POST", `/sets/${id}/run`),
  runSource: (id: number) => req<{ queued: Job[] }>("POST", `/sources/${id}/run`),
  jobs: (setId?: number) => req<Job[]>("GET", `/jobs${setId ? `?set_id=${setId}` : ""}`),
  cancelJob: (id: number) => req<Job>("POST", `/jobs/${id}/cancel`),
  documents: (setId: number, q = "") => req<DocRow[]>("GET", `/sets/${setId}/documents?q=${encodeURIComponent(q)}`),
  document: (id: number) => req<Doc>("GET", `/documents/${id}`),
  setDocumentStatus: (id: number, status: "active" | "excluded") => req<Doc>("PATCH", `/documents/${id}?status=${status}`),
  audit: () => req<AuditRow[]>("GET", "/audit"),
};

export type ProtectionSettings = {
  app_check: "off" | "monitor" | "enforce";
  anon_per_hour: number; anon_per_day: number; user_per_hour: number; user_per_day: number;
  anon_daily_usd: number;
  off_topic_strikes: number;
};
export type ProtectionData = {
  settings: ProtectionSettings;
  days: number;
  by_reason: { reason: string; enforced: boolean; n: number; visitors: number; last: string }[];
  daily: { day: string; blocked: number; flagged: number }[];
  top_visitors: { visitor: string; n: number; last: string; reasons: string[] }[];
  app_check: Partial<Record<"verified" | "missing" | "invalid", number>>;
  anon_spend_today: number;
};

export type LookupMode = "details" | "confirm" | "off";
export type LookupSettings = { access: Record<"security" | "board" | "admin", LookupMode>; per_hour: number; per_day: number };
export type LookupLog = { id: number; at: string; actor: string; role: string | null; channel: string; address_q: string | null;
  name_q: string | null; plate_q?: string | null; mode: string; result: string; matches: number };
export type LookupData = { settings: LookupSettings; log: LookupLog[] };
