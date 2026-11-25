import { createContext, useContext, useEffect, useMemo, useState, type ReactElement, type ReactNode } from "react";
import { Link } from "react-router-dom";
import {
  AssistantRuntimeProvider, AttachmentPrimitive, ComposerPrimitive, ThreadPrimitive, useLocalRuntime,
  type AssistantRuntime, type AttachmentAdapter, type ChatModelAdapter, type ThreadMessageLike,
} from "@assistant-ui/react";
import { getToken } from "../admin/api";
import { appCheckHeaders } from "../lib/firebase";
import { reportError } from "../lib/report";
import { ConfirmEmail, SignInCard, useMember, type Member } from "./member";

// Chat UI on assistant-ui (headless primitives, our own styles). The model adapter streams
// server-sent events from /api/chat/stream: status updates, answer tokens, then a final "done"
// event with citations and the turn id used for feedback.

type Citation = { n: number; title: string; uri: string; section: string; set: string };
type Custom = { status?: string; citations?: Citation[]; turnId?: string; error?: boolean;
  signIn?: { pending_id: string; title: string | null }; nudge?: boolean; blocked?: boolean };
const MemberCtx = createContext<Member>({ kind: "anon" });
const NUDGE_KEY = "cypress.nudge.dismissed";

// Fallback if the API can't be reached; the live list is edited in Board & contacts.
const SUGGESTIONS = [
  "Is the park open to the public?",
  "What are the park hours?",
  "Can I bring my dog to the park?",
  "How do I rent the clubhouse?",
  "How can I tell if an address has deeded access?",
];
const KEY = "cypress.chat.v2";

type Saved = { thread: string | null; messages: ThreadMessageLike[] };
function load(): Saved {
  try { return JSON.parse(sessionStorage.getItem(KEY) || "") } catch { return { thread: null, messages: [] } }
}
function save(s: Saved) {
  try { sessionStorage.setItem(KEY, JSON.stringify(s)); } catch { /* private mode */ }
}

const plain = (s: string) => s.replace(/\*\*|__/g, "");

const authHeaders = (): Record<string, string> => {
  const token = getToken();
  return token ? { authorization: `Bearer ${token}` } : {};
};
/** Sign-in plus App Check headers, for requests that cost money (chat, uploads). */
const guardedHeaders = async (): Promise<Record<string, string>> => ({ ...authHeaders(), ...(await appCheckHeaders()) });

/** Small JPEG preview for the conversation (the full photo goes to the server). */
async function thumbnail(file: File, max = 360): Promise<string> {
  const bmp = await createImageBitmap(file);
  const scale = Math.min(1, max / Math.max(bmp.width, bmp.height));
  const canvas = document.createElement("canvas");
  canvas.width = Math.round(bmp.width * scale); canvas.height = Math.round(bmp.height * scale);
  canvas.getContext("2d")!.drawImage(bmp, 0, 0, canvas.width, canvas.height);
  return canvas.toDataURL("image/jpeg", 0.7);
}

// Photos and videos for reports, uploaded when the message is sent. Photos go through the API; videos go
// straight to private storage with a one-time signed link (they're too big for an API request).
export const uploadLimits = { videos: true, maxVideoMb: 100 };
let onProgress: (pct: number | null) => void = () => {};

/** First frame of a video as a small JPEG, for the conversation. */
async function videoPoster(file: File, max = 360): Promise<string | null> {
  const url = URL.createObjectURL(file);
  try {
    const v = document.createElement("video");
    v.muted = true; v.playsInline = true; v.preload = "metadata"; v.src = url;
    await new Promise<void>((ok, bad) => { v.onloadeddata = () => ok(); v.onerror = () => bad(new Error("no frame")); setTimeout(() => bad(new Error("timeout")), 4000); });
    v.currentTime = Math.min(0.2, v.duration || 0);
    await new Promise<void>((ok) => { v.onseeked = () => ok(); setTimeout(ok, 1500); });
    const scale = Math.min(1, max / Math.max(v.videoWidth, v.videoHeight));
    const c = document.createElement("canvas");
    c.width = Math.round(v.videoWidth * scale); c.height = Math.round(v.videoHeight * scale);
    c.getContext("2d")!.drawImage(v, 0, 0, c.width, c.height);
    return c.toDataURL("image/jpeg", 0.7);
  } catch { return null; } finally { URL.revokeObjectURL(url); }
}

function putWithProgress(url: string, file: File, headers: Record<string, string>): Promise<void> {
  return new Promise((ok, bad) => {
    const x = new XMLHttpRequest();
    x.open("PUT", url);
    Object.entries(headers).forEach(([k, v]) => x.setRequestHeader(k, v));
    x.upload.onprogress = (e) => { if (e.lengthComputable) onProgress(Math.round((e.loaded / e.total) * 100)); };
    x.onload = () => (x.status < 300 ? ok() : bad(new Error("The video upload failed. Please try again.")));
    x.onerror = () => bad(new Error("The video upload was interrupted. Check your connection and try again."));
    x.send(file);
  });
}

async function sendVideo(att: { id: string; name: string; file: File }) {
  const { file } = att;
  if (!uploadLimits.videos) throw new Error("Videos aren't accepted right now. Try a photo instead.");
  if (file.size > uploadLimits.maxVideoMb * 1024 * 1024) throw new Error(`That video is over ${uploadLimits.maxVideoMb} MB. Try a shorter clip.`);
  const res = await fetch("/api/chat/upload-video", { method: "POST", headers: { "content-type": "application/json", ...(await guardedHeaders()) },
    body: JSON.stringify({ content_type: file.type || "video/mp4", bytes: file.size }) });
  const slot = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(slot.detail || "The video couldn't be uploaded.");
  onProgress(0);
  try {
    await putWithProgress(slot.url, file, slot.headers);
    const done = await fetch(`/api/chat/upload/${slot.upload_id}/done`, { method: "POST" });
    const d = await done.json().catch(() => ({}));
    if (!done.ok) throw new Error(d.detail || "The video couldn't be saved.");
  } finally { onProgress(null); }
  const poster = await videoPoster(file);
  return { id: slot.upload_id, type: "file" as const, name: att.name, contentType: file.type, status: { type: "complete" as const },
           content: poster ? [{ type: "image" as const, image: poster }] : [{ type: "text" as const, text: "🎬 Video" }] };
}

const photoAdapter: AttachmentAdapter = {
  accept: "image/*,video/*",
  async add({ file }) {
    return { id: crypto.randomUUID(), type: file.type.startsWith("video/") ? "file" : "image", name: file.name, contentType: file.type, file,
             status: { type: "requires-action", reason: "composer-send" } };
  },
  async send(att) {
    if (att.file.type.startsWith("video/")) return sendVideo(att);
    const form = new FormData();
    form.append("file", att.file);
    const res = await fetch("/api/chat/upload", { method: "POST", body: form, headers: await guardedHeaders() });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.detail || "The photo couldn't be uploaded.");
    return { id: data.upload_id, type: "image", name: att.name, contentType: "image/jpeg",
             status: { type: "complete" }, content: [{ type: "image", image: await thumbnail(att.file) }] };
  },
  async remove() { /* nothing stored until send */ },
};

function PendingPhoto({ file }: { file?: File }) {
  const url = useMemo(() => (file ? URL.createObjectURL(file) : ""), [file]);
  useEffect(() => () => { if (url) URL.revokeObjectURL(url); }, [url]);
  if (!url) return <span className="small">📎</span>;
  return file?.type.startsWith("video/")
    ? <><video src={url} muted playsInline preload="metadata" /><span className="vid-badge" aria-label="Video">🎬</span></>
    : <img src={url} alt="" />;
}

function makeAdapter(thread: { current: string | null }): ChatModelAdapter {
  return {
    async *run({ messages, abortSignal }) {
      const last = messages[messages.length - 1];
      const photos = last.role === "user" ? (last.attachments ?? []).map((a) => a.id) : [];
      const message = last.content.map((p) => (p.type === "text" ? p.text : "")).join("\n").trim()
        || (photos.length ? "(sent a photo)" : "");
      let res: Response;
      try {
        res = await fetch(import.meta.env.VITE_STREAM_URL || "/api/chat/stream", {
          method: "POST", signal: abortSignal,
          headers: { "content-type": "application/json", ...(await guardedHeaders()) },
          body: JSON.stringify({ message, thread_id: thread.current, attachments: photos }),
        });
      } catch (e) {
        if ((e as Error).name === "AbortError") return;
        reportError("chat-stream", e, "network");
        yield { content: [{ type: "text", text: "I couldn't reach the server. Check your connection and try again." }], metadata: { custom: { error: true } } };
        return;
      }
      if (!res.ok || !res.body) {
        const data = await res.json().catch(() => ({}));
        if (res.status >= 500) reportError("chat-stream", new Error(data.detail || res.statusText), String(res.status));
        // sign_in: a visitor limit or protection check; members aren't affected, so offer sign-in.
        const custom: Custom = data.sign_in ? { error: true, nudge: true, blocked: true } : { error: true };
        yield { content: [{ type: "text", text: data.detail || "Something went wrong. Please try again." }], metadata: { custom } };
        return;
      }
      const reader = res.body.getReader();
      const decoder = new TextDecoder();
      let buf = "", answer = "", status = "Thinking…";
      yield { content: [], metadata: { custom: { status } } };
      for (;;) {
        const { value, done } = await reader.read();
        if (done) break;
        buf += decoder.decode(value, { stream: true });
        let cut: number;
        while ((cut = buf.indexOf("\n\n")) >= 0) {
          const line = buf.slice(0, cut);
          buf = buf.slice(cut + 2);
          if (!line.startsWith("data:")) continue;
          const ev = JSON.parse(line.slice(5));
          if (ev.type === "status") status = ev.text;
          else if (ev.type === "token") { answer += ev.text; status = ""; }
          else if (ev.type === "reset") answer = "";
          else if (ev.type === "done") {
            thread.current = ev.thread_id;
            if (ev.error) reportError("chat-stream", new Error("answer failed on the server"), "done-error");
            const custom: Custom = { citations: ev.citations, turnId: ev.turn_id ?? undefined, error: ev.error,
              signIn: ev.sign_in ?? undefined, nudge: ev.nudge_sign_in ?? undefined };
            yield { content: [{ type: "text", text: ev.answer }], metadata: { custom } };
            return;
          }
          yield { content: answer ? [{ type: "text", text: answer }] : [], metadata: { custom: { status } } };
        }
      }
    },
  };
}

/** Render **bold**, [n] citation markers and line breaks/bullets without pulling in a Markdown library. */
export function Rich({ text, citations }: { text: string; citations?: Citation[] }) {
  const byN = new Map((citations ?? []).map((c) => [c.n, c]));
  // Tokens: **bold**, [text](http(s)/mailto link), [n] citation, bare URL, email. Links are only ever
  // http(s) or mailto, so model output can't inject javascript: URLs.
  const TOKEN = /(\*\*[^*]+\*\*|(?<![\w*])\*[^*\s][^*\n]*?\*(?![\w*])|\[[^\]\n]+\]\((?:https?:\/\/|mailto:)[^)\s]+\)|\[\d{1,2}\]|https?:\/\/[^\s<>()]+|[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,})/g;
  const link = (href: string, label: ReactNode, k: string) => (
    <a key={k} href={href} {...(href.startsWith("mailto:") ? {} : { target: "_blank", rel: "noreferrer" })}>{label}</a>);
  const inline = (s: string, key: string): ReactNode[] =>
    s.split(TOKEN).flatMap((part, i): ReactNode[] => {
      const k = `${key}-${i}`;
      if (!part) return [];
      if (/^\*\*[^*]+\*\*$/.test(part)) return [<strong key={k}>{inline(part.slice(2, -2), k)}</strong>];
      if (/^\*[^*\s][^*\n]*\*$/.test(part)) return [<em key={k}>{inline(part.slice(1, -1), k)}</em>];
      const md = part.match(/^\[([^\]\n]+)\]\(((?:https?:\/\/|mailto:)[^)\s]+)\)$/);
      if (md) return [link(md[2], md[1], k)];
      const m = part.match(/^\[(\d{1,2})\]$/);
      if (m) {
        const c = byN.get(+m[1]);
        return c ? [<a key={k} className="cite" href={c.uri} target="_blank" rel="noreferrer" title={`${c.title}${c.section && c.section !== c.title ? ` — ${plain(c.section)}` : ""}`}>{m[1]}</a>] : [];
      }
      if (/^https?:\/\//.test(part)) {
        const url = part.replace(/[.,;:!?]+$/, ""), rest = part.slice(url.length);
        return [link(url, url.replace(/^https?:\/\//, "").replace(/\/$/, ""), k), rest];
      }
      if (/^[^\s@]+@[^\s@]+$/.test(part)) {
        const email = part.replace(/[.,;:!?]+$/, ""), rest = part.slice(email.length);
        return [link(`mailto:${email}`, email, k), rest];
      }
      return [part];
    });
  const blocks: ReactElement[] = [];
  let list: string[] = [];
  const flush = () => { if (list.length) { blocks.push(<ul key={`ul${blocks.length}`}>{list.map((l, i) => <li key={i}>{inline(l, `li${blocks.length}-${i}`)}</li>)}</ul>); list = []; } };
  text.split("\n").forEach((line, i) => {
    const b = line.match(/^\s*[*\-•]\s+(.*)$/);
    if (b) { list.push(b[1]); return; }
    flush();
    if (line.trim()) blocks.push(<p key={`p${i}`}>{inline(line.replace(/^#+\s*/, ""), `p${i}`)}</p>);
  });
  flush();
  return <>{blocks}</>;
}

async function postFeedback(body: { turn_id: string; rating?: 1 | -1; comment?: string }) {
  await fetch("/api/chat/feedback", {
    method: "POST", headers: { "content-type": "application/json", ...authHeaders() }, body: JSON.stringify(body),
  }).catch(() => undefined);
}

function Feedback({ turnId }: { turnId: string }) {
  const [rating, setRating] = useState<1 | -1 | null>(null);
  const [comment, setComment] = useState<"open" | "sent" | null>(null);
  const [text, setText] = useState("");
  const rate = (r: 1 | -1) => { setRating(r); setComment(r === -1 ? "open" : null); postFeedback({ turn_id: turnId, rating: r }); };
  return (
    <div className="feedback">
      <button className={`thumb${rating === 1 ? " on" : ""}`} aria-label="Good answer" aria-pressed={rating === 1} onClick={() => rate(1)}>👍</button>
      <button className={`thumb${rating === -1 ? " on" : ""}`} aria-label="Bad answer" aria-pressed={rating === -1} onClick={() => rate(-1)}>👎</button>
      {rating && comment !== "open" && <span className="small muted">Thanks for the feedback</span>}
      {comment === "open" && (
        <form className="fb-form" onSubmit={(e) => { e.preventDefault(); if (text.trim()) postFeedback({ turn_id: turnId, comment: text }); setComment("sent"); }}>
          <input autoFocus value={text} maxLength={1000} placeholder="What was wrong? (optional)" onChange={(e) => setText(e.target.value)} />
          <button className="btn small">Send</button>
          <button type="button" className="btn ghost small" onClick={() => setComment("sent")}>Skip</button>
        </form>
      )}
    </div>
  );
}

type MessageView = {
  role: string;
  attachments?: readonly { id: string; content?: readonly { type: string; image?: string; text?: string }[] }[];
  content: readonly { type: string; text?: string }[];
  metadata?: { custom?: Record<string, unknown> };
  status?: { type: string };
};

function Message({ message }: { message: MessageView }) {
  const text = message.content.map((p) => (p.type === "text" ? p.text ?? "" : "")).join("");
  const c = (message.metadata?.custom ?? {}) as Custom;
  if (message.role === "user") {
    const imgs = (message.attachments ?? []).flatMap((a) => (a.content ?? []).filter((p) => p.type === "image" && p.image).map((p) => p.image!));
    const vids = (message.attachments ?? []).filter((a) => (a.content ?? []).some((p) => p.type === "text")).length;
    return (
      <div className="msg user">
        {imgs.length > 0 && <div className="photos">{imgs.map((src, i) => <img key={i} src={src} alt="Attachment" />)}</div>}
        {vids > 0 && <div className="small muted">🎬 {vids} video{vids > 1 ? "s" : ""} attached</div>}
        {text && text !== "(sent a photo)" && <div className="bubble">{text}</div>}
      </div>
    );
  }
  const running = message.status?.type === "running";
  if (!text && running) {
    return (
      <div className="msg assistant">
        <div className="bubble typing" aria-label="Cypress is thinking"><span /><span /><span /></div>
        {c.status && <div className="status small muted" aria-live="polite">{c.status}</div>}
      </div>
    );
  }
  return (
    <div className={`msg assistant${c.error ? " err" : ""}`}>
      <div className="bubble"><Rich text={text} citations={c.citations} />{running && <span className="caret" aria-hidden />}</div>
      {!running && c.citations && c.citations.length > 0 && (
        <ol className="sources">
          {c.citations.map((s) => (
            <li key={s.n} value={s.n}><a href={s.uri} target="_blank" rel="noreferrer">{s.title}</a>
              {s.section && s.section !== s.title && <span className="muted"> · {plain(s.section)}</span>}</li>
          ))}
        </ol>
      )}
      {!running && c.signIn && <SignInPrompt pendingId={c.signIn.pending_id} title={c.signIn.title} />}
      {!running && c.turnId && !c.error && <Feedback turnId={c.turnId} />}
    </div>
  );
}

function SignInPrompt({ pendingId, title }: { pendingId: string; title: string | null }) {
  const member = useContext(MemberCtx);
  if (member.kind === "signed-in") return null;
  return <SignInCard pendingId={pendingId} title={title} />;
}

function Nudge({ blocked }: { blocked?: boolean }) {
  const member = useContext(MemberCtx);
  const [open, setOpen] = useState(!!blocked);
  const [dismissed, setDismissed] = useState(() => { try { return !!localStorage.getItem(NUDGE_KEY); } catch { return false; } });
  if (member.kind !== "anon" || (dismissed && !blocked)) return null;
  const dismiss = () => { setDismissed(true); try { localStorage.setItem(NUDGE_KEY, "1"); } catch { /* private mode */ } };
  return (
    <div className="nudge">
      {open ? <SignInCard compact /> : <>
        <span>LHCA member? Sign in with your email to report issues and see member-only information.</span>
        <span className="nudge-actions">
          <button className="btn small primary" onClick={() => setOpen(true)}>Sign in</button>
          <button className="btn ghost small" onClick={dismiss} aria-label="Dismiss">Not now</button>
        </span></>}
    </div>
  );
}

function Chat({ saved, onReset }: { saved: Saved; onReset: () => void }) {
  const { member, notice, setNotice, needEmail, finish, signOut } = useMember();
  const [showSignIn, setShowSignIn] = useState(false);
  const [suggestions, setSuggestions] = useState(SUGGESTIONS);
  useEffect(() => {
    fetch("/api/chat/suggestions").then((r) => (r.ok ? r.json() : null))
      .then((d) => {
        if (d?.suggestions?.length) setSuggestions(d.suggestions);
        if (d?.uploads) { uploadLimits.videos = d.uploads.videos; uploadLimits.maxVideoMb = d.uploads.max_video_mb; }
      }, () => undefined);
  }, []);
  useEffect(() => { void appCheckHeaders(); }, []);   // warm up App Check so the first question isn't delayed
  const [progress, setProgress] = useState<number | null>(null);
  useEffect(() => { onProgress = setProgress; return () => { onProgress = () => {}; }; }, []);
  useEffect(() => { if (member.kind === "signed-in") setShowSignIn(false); }, [member.kind]);
  const thread = useMemo(() => ({ current: saved.thread }), [saved]);
  const adapter = useMemo(() => makeAdapter(thread), [thread]);
  const runtime: AssistantRuntime = useLocalRuntime(adapter, { initialMessages: saved.messages, adapters: { attachments: photoAdapter } });
  const [empty, setEmpty] = useState(saved.messages.length === 0);

  useEffect(() => runtime.thread.subscribe(() => {
    const st = runtime.thread.getState();
    setEmpty(st.messages.length === 0);
    if (st.isRunning) return;
    save({
      thread: thread.current,
      messages: st.messages.map((m) => ({
        role: m.role, id: m.id,
        content: m.content.filter((p) => p.type === "text").map((p) => ({ type: "text" as const, text: (p as { text: string }).text })),
        metadata: m.role === "assistant" ? { custom: m.metadata.custom } : undefined,
      })),
    });
  }), [runtime, thread]);

  const ask = (text: string) => runtime.thread.append({ role: "user", content: [{ type: "text", text }] });
  const lastCustom = (() => {
    const msgs = runtime.thread.getState().messages;
    const last = msgs[msgs.length - 1];
    return last && last.role === "assistant" ? (last.metadata.custom as Custom) : undefined;
  })();

  return (
    <MemberCtx.Provider value={member}>
    <AssistantRuntimeProvider runtime={runtime}>
      <div className="chat">
        <header className="chat-head">
          <img src="/logo-256.png" alt="" width={40} height={40} />
          <div className="t"><b>Cypress</b><span>Lake Hills Community Association</span></div>
          <span className="spacer" />
          {!empty && <button className="btn ghost small" onClick={onReset}>New chat</button>}
          {member.kind === "signed-in"
            ? <span className="who-chip" title={member.email}>{member.role ? (member.role === "member" ? "Member" : "Team") : "Signed in"}
                <button className="linkish small" onClick={signOut}>Sign out</button></span>
            : member.kind === "anon" && <button className="btn ghost small" aria-expanded={showSignIn} onClick={() => setShowSignIn((v) => !v)}><span className="wide-only">Member sign-in</span><span className="narrow-only">Sign in</span></button>}
          <Link to="/admin" className="small muted">Admin</Link>
        </header>
        {showSignIn && member.kind === "anon" && <div className="chat-banner"><SignInCard compact /></div>}
        {needEmail && <div className="chat-banner"><ConfirmEmail onDone={finish} /></div>}
        {progress !== null && <div className="chat-banner notice ok" role="status"><span>Uploading video… {progress}%</span>
          <div className="meter grow-meter"><span style={{ width: `${progress}%` }} /></div></div>}
        {notice && <div className={`chat-banner notice ${notice.tone}`} role="status">
          <span>{notice.text}</span><button className="linkish small" onClick={() => setNotice(null)} aria-label="Dismiss">Dismiss</button></div>}

        <ThreadPrimitive.Root className="chat-thread">
          <ThreadPrimitive.Viewport className="chat-log" aria-live="polite">
            {empty && (
              <div className="welcome">
                <h1>How can I help?</h1>
                <p>Ask about park hours and rules, the pool, clubhouse rentals, membership and dues, deeded access, or the bylaws. Answers come from LHCA documents, with sources.</p>
                <div className="sugg">{suggestions.map((s) => <button key={s} className="btn" onClick={() => ask(s)}>{s}</button>)}</div>
              </div>
            )}
            <ThreadPrimitive.Messages>{({ message }) => <Message message={message as unknown as MessageView} />}</ThreadPrimitive.Messages>
            {lastCustom?.nudge && <Nudge key={lastCustom.blocked ? "blocked" : "nudge"} blocked={lastCustom.blocked} />}
          </ThreadPrimitive.Viewport>

          <ComposerPrimitive.Root className="chat-input">
            <div className="pending-photos">
              <ComposerPrimitive.Attachments>{({ attachment }) => (
                <AttachmentPrimitive.Root className="pending-photo">
                  <PendingPhoto file={attachment.file} />
                  <AttachmentPrimitive.Remove className="x" aria-label="Remove photo">×</AttachmentPrimitive.Remove>
                </AttachmentPrimitive.Root>
              )}</ComposerPrimitive.Attachments>
            </div>
            <ComposerPrimitive.AddAttachment className="btn ghost icon" aria-label="Attach a photo or video" title="Attach a photo or video">📎</ComposerPrimitive.AddAttachment>
            <ComposerPrimitive.Input rows={1} maxLength={2000} placeholder="Ask a question about LHCA…" aria-label="Message" autoFocus />
            <ThreadPrimitive.If running={false}><ComposerPrimitive.Send className="btn primary">Send</ComposerPrimitive.Send></ThreadPrimitive.If>
            <ThreadPrimitive.If running><ComposerPrimitive.Cancel className="btn">Stop</ComposerPrimitive.Cancel></ThreadPrimitive.If>
          </ComposerPrimitive.Root>
        </ThreadPrimitive.Root>
        <p className="fine">Cypress can make mistakes. To reach the board (all volunteers), contact CAM, our management company, at{" "}
          <a href="mailto:admin@camanagers.com">admin@camanagers.com</a>. Problems with Cypress itself:{" "}
          <a href="mailto:LHCA.votes@gmail.com">LHCA.votes@gmail.com</a>.</p>
      </div>
    </AssistantRuntimeProvider>
    </MemberCtx.Provider>
  );
}

export default function ChatPage() {
  const [saved, setSaved] = useState<Saved>(load);
  const [key, setKey] = useState(0);
  const reset = () => { save({ thread: null, messages: [] }); setSaved({ thread: null, messages: [] }); setKey((k) => k + 1); };
  return <Chat key={key} saved={saved} onReset={reset} />;
}
