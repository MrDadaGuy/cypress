"""Model registry: one interface over Gemini, Claude and open-weights MaaS models on Vertex AI.

The catalog (models, prices, on/off), versioned per-step agent configs (model, sampling, prompt;
optionally per audience), runtime knobs (fallback model, hedge
timing, timeout) and the budget all live in the database and are edited in the admin console;
this module only reads them (30-second cache). Each LangGraph node asks for a model by node name,
and every provider call is metered through finops.record(), including hedges, fallbacks and failures.
"""

import json
import logging
import queue
import re
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass, fields
from functools import lru_cache

from . import finops, tracing
from .config import settings
from .db import tx

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Model:
    id: str
    provider: str      # gemini | claude | maas
    name: str          # provider model name
    region: str = "global"
    label: str = ""
    kind: str = "chat"            # chat | embed | rerank
    thinking: str | None = None   # Gemini thinking level; None = model default
    input_per_m: float | None = None
    cached_per_m: float | None = None
    output_per_m: float | None = None
    per_call_usd: float | None = None
    price_source: str | None = None
    enabled: bool = True
    note: str | None = None

    @property
    def priced(self) -> bool:
        if self.kind == "rerank":
            return self.per_call_usd is not None
        return self.input_per_m is not None and self.output_per_m is not None


# Only used if the database can't be read, so the agent keeps answering. The real catalog is the
# llm_models table (seeded by migration 012, edited in the console).
EMERGENCY = Model("gemini-flash-lite", "gemini", "gemini-3.5-flash-lite", label="Gemini 3.5 Flash-Lite",
                  input_per_m=0.30, cached_per_m=0.03, output_per_m=2.50)

# Graph nodes are code (each is a step in the graph); which model each uses is configuration.
# The second value is the model a node gets when nothing is assigned.
NODES = {
    "route": ("Classify intent and rewrite follow-ups as standalone questions", "gemini-flash-lite"),
    "grade": ("Judge whether retrieved passages answer the question", "gemini-flash-lite"),
    "route_ticket": ("Suggest who should own a new ticket, from people's areas of responsibility", "gemini-flash-lite"),
    "answer": ("Write the cited answer", "gemini-flash"),
    "act": ("Use tools (deeded-access check, and more by role) and explain the result", "gemini-flash"),
    "ocr": ("Read scanned PDF pages during ingest (image input)", "gemini-flash"),
    "eval_judge": ("Grade answers in eval runs against what a good answer needs", "gemini-flash"),
    "consistency": ("Check whether two documents disagree (content checks)", "gemini-flash-lite"),
    "change_summary": ("Summarize what changed when a re-crawl updated a document", "gemini-flash-lite"),
    "consistency_verify": ("Double-check each conflict content checks found, to drop false alarms", "gemini-flash"),
    # Security incident case file (cases.py): a supervisor and sub-agents.
    "case_plan": ("Incident case file: plan which sub-agents to run", "gemini-flash-lite"),
    "plate_read": ("Incident case file: read plates and describe vehicles in photos (image input)", "gemini-flash"),
    "case_match": ("Incident case file: match plates, names and addresses to households (tool calling)", "gemini-flash"),
    "case_policy": ("Incident case file: find the rules and process that apply in the bylaws and rules", "gemini-flash"),
    "case_policy_gen": ("Incident case file, tree-of-thought mode: propose candidate rule readings", "gemini-flash-lite"),
    "case_policy_critic": ("Incident case file, tree-of-thought mode: score candidate readings for fit and scope", "gemini-flash"),
    "case_writer": ("Incident case file: write the board's summary and a draft notice letter", "gemini-flash"),
}
TOOL_PROVIDERS = {"gemini"}      # providers wired for function calling (act) and image input (ocr)
GEMINI_ONLY = {"act", "ocr", "plate_read", "case_match"}
RUNTIME_DEFAULTS = {"fallback_model": "gemini-flash-lite", "hedge_s": 4.0, "stream_hedge_s": 5.0, "timeout_s": 25}
TTL = 30
_cache: dict = {"at": 0.0}
_FIELDS = {f.name for f in fields(Model)}


def _load() -> dict:
    if time.time() - _cache["at"] < TTL:
        return _cache
    catalog, configs = {}, {}
    try:
        with tx() as conn:
            for r in conn.execute("select * from llm_models"):
                catalog[r["id"]] = Model(**{k: (float(v) if (k.endswith("_per_m") or k == "per_call_usd") and v is not None else v)
                                            for k, v in r.items() if k in _FIELDS})
            configs = {(r["node"], r["audience"]): dict(r) for r in conn.execute(
                """select distinct on (node, audience) * from agent_configs order by node, audience, version desc""")}
    except Exception:
        log.exception("reading the model catalog failed; using the emergency model")
    if not catalog:
        catalog = {EMERGENCY.id: EMERGENCY}
    _cache.update(at=time.time(), catalog=catalog, configs=configs)
    return _cache


def clear_cache() -> None:
    _cache["at"] = 0.0
    finops.clear()


def catalog() -> dict[str, Model]:
    return _load()["catalog"]


def runtime() -> dict:
    from . import org
    return {**RUNTIME_DEFAULTS, **(org.get().get("llm_runtime") or {})}


def _usable(mid: str | None, kind: str = "chat") -> Model | None:
    m = catalog().get(mid or "")
    return m if m and m.enabled and m.kind == kind else None


def fallback() -> Model:
    return _usable(runtime()["fallback_model"]) or next(
        (m for m in catalog().values() if m.enabled and m.kind == "chat"), EMERGENCY)


AUDIENCES = ("all", "residents", "team")

# Per-request model overrides (eval runs): {node: model_id}. Never persisted.
import contextvars  # noqa: E402
override: contextvars.ContextVar[dict] = contextvars.ContextVar("model_override", default={})


def live_configs() -> dict[tuple[str, str], dict]:
    """(node, audience) -> the live (highest) version of that agent config."""
    return _load()["configs"]


def config(node: str, audience: str | None = None) -> dict:
    """The live config for a node and audience, with fields falling back audience -> 'all' -> built-in.
    Keys: model_id, temperature, max_tokens, prompt, id (the config row that set the model), audience."""
    aud = audience or finops.audience()
    cfgs = live_configs()
    out = {"model_id": NODES[node][1] if node in NODES else None, "temperature": None, "max_tokens": None, "prompt": None, "id": None,
           "audience": "built-in"}
    for a in ("all", aud) if aud != "all" else ("all",):
        c = cfgs.get((node, a))
        if not c:
            continue
        for k in ("model_id", "temperature", "max_tokens", "prompt"):
            if c.get(k) is not None:
                out[k] = float(c[k]) if k == "temperature" else c[k]
        out["id"], out["audience"] = c["id"], a
    return out


def assignments(audience: str = "all") -> dict[str, str]:
    """node -> configured model id for an audience (what the console shows; may be disabled)."""
    return {n: config(n, audience)["model_id"] for n in NODES}


def model_for(node: str, audience: str | None = None) -> Model:
    """The model a node actually runs on: the budget guard wins, then the live config, then the
    node's default, then the fallback (skipping disabled models)."""
    if (g := finops.guard_model()) and (m := _usable(g)):
        return m
    if (o := override.get().get(node)) and (m := _usable(o)):
        return m
    return _usable(config(node, audience)["model_id"]) or _usable(NODES[node][1]) or fallback()


PROMPT_ONLY = {"about"}           # config keys that hold a prompt but no model (shared text)


def _params(node: str, temperature: float, max_tokens: int) -> tuple[float, int]:
    """Config overrides for sampling settings (null = the value the code asked for)."""
    c = config(node) if node in NODES else {}
    t = c.get("temperature")
    mt = c.get("max_tokens")
    return (t if t is not None else temperature), (int(mt) if mt is not None else max_tokens)


def _rec(model: Model, node: str, purpose: str, usage, latency_ms, ok: bool = True, error: str | None = None) -> None:
    cid = config(node).get("id") if node in NODES else None
    finops.record(model, node, purpose, usage, latency_ms, ok=ok, error=error, config_id=cid)


def rerank_model() -> Model | None:
    """The enabled reranker, or None (reranking off)."""
    return next((m for m in catalog().values() if m.kind == "rerank" and m.enabled), None)


def gemini_for(node: str, audience: str | None = None) -> Model:
    """model_for(), restricted to providers this node supports (tool calling / image input)."""
    m = model_for(node, audience)
    if m.provider in TOOL_PROVIDERS:
        return m
    d = _usable(NODES[node][1])
    if d and d.provider in TOOL_PROVIDERS:
        return d
    fb = fallback()
    return fb if fb.provider in TOOL_PROVIDERS else EMERGENCY


def embed_model() -> Model:
    m = next((m for m in catalog().values() if m.kind == "embed" and m.enabled), None)
    return m or Model("gemini-embedding", "gemini", "gemini-embedding-001", "us-central1", kind="embed")


# ---------------- provider clients ----------------

_gemini_clients: dict = {}
_gemini_lock = threading.Lock()


def _gemini(timeout_s: int, location: str = "global"):
    """One client per (timeout, location), created under a lock. lru_cache isn't atomic: two threads missing at once
    each built a client, only one was cached, and the other caller (holding just `.models`) let its client be
    garbage-collected mid-request, which closes its HTTP connection ("client has been closed")."""
    key = (timeout_s, location)
    if (c := _gemini_clients.get(key)) is None:
        with _gemini_lock:
            if (c := _gemini_clients.get(key)) is None:
                from google import genai
                c = _gemini_clients[key] = genai.Client(vertexai=True, project=settings.project, location=location,
                                                        http_options={"timeout": int(timeout_s * 1000)})
    return c


@lru_cache
def _claude(region: str, timeout_s: int):
    from anthropic import AnthropicVertex
    return AnthropicVertex(project_id=settings.project, region=region, timeout=timeout_s)


def _maas_token() -> str:
    import google.auth
    import google.auth.transport.requests
    creds, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
    creds.refresh(google.auth.transport.requests.Request())
    return creds.token


def _timeout() -> int:
    return int(runtime()["timeout_s"])


def gemini_usage(resp) -> finops.Usage:
    um = getattr(resp, "usage_metadata", None)
    if not um:
        return finops.Usage(estimated=True)
    thinking = um.thoughts_token_count or 0
    return finops.Usage(input_tokens=um.prompt_token_count or 0, cached_tokens=um.cached_content_token_count or 0,
                        output_tokens=(um.candidates_token_count or 0) + thinking, thinking_tokens=thinking)


# Vertex has occasional slow starts (queueing). If a call hasn't returned after hedge_s, a second
# request goes to the fallback model and whichever finishes first wins. Both are metered: the
# hedge's cost is visible in the console as "hedge" spend.
_pool = ThreadPoolExecutor(max_workers=16, thread_name_prefix="llm")


def _submit(fn):
    return _pool.submit(finops.context().run, fn)   # keep the turn tag on the worker thread


def _hedged(primary, backup, after: float):
    """Run primary(); if it isn't done after `after` seconds, also run backup(purpose). First success
    wins. purpose is "hedge" if primary was slow, "fallback" if it had already failed.
    Returns (result, used_backup)."""
    f1 = _submit(primary)
    done, _ = wait([f1], timeout=after)
    if done and not f1.exception():
        return f1.result(), False
    f2 = _submit(lambda: backup("fallback" if done else "hedge"))
    pending = {f1, f2}
    errors = []
    while pending:
        done, pending = wait(pending, timeout=_timeout() + 5, return_when=FIRST_COMPLETED)
        if not done:
            break
        for f in done:
            if f.exception():
                errors.append(f.exception())
            elif f.result():
                return f.result(), f is f2
    raise errors[0] if errors else TimeoutError("model calls timed out")


def generate(node: str, system: str, messages: list[dict], *, json_mode: bool = False,
             max_tokens: int = 1024, temperature: float = 0.2) -> tuple[str, str]:
    """Run the model for `node`, hedged with the fallback model if it's slow, and falling back if
    it errors. messages: [{"role": "user"|"assistant", "content": str}]. Returns (text, model_id)."""
    m, fb = model_for(node), fallback()
    rt = runtime()
    temperature, max_tokens = _params(node, temperature, max_tokens)
    args = (system, messages, json_mode, max_tokens, temperature)
    try:
        text, used_backup = _hedged(lambda: call(m, node, "primary", *args),
                                    lambda purpose: call(fb, node, purpose, *args), float(rt["hedge_s"]))
        if used_backup:
            log.info("%s slow for %s; hedge (%s) answered first", m.id, node, fb.id)
            return text, fb.id
        if text or m.id == fb.id:
            return text, m.id
        log.warning("%s returned empty text for %s; falling back", m.id, node)
    except Exception as e:
        if m.id == fb.id:
            raise
        log.warning("%s failed for %s (%s); falling back to %s", m.id, node, e, fb.id)
    return call(fb, node, "fallback", *args), fb.id


def call(m: Model, node: str, purpose: str, system: str, messages: list[dict], json_mode: bool = False,
         max_tokens: int = 1024, temperature: float = 0.2) -> str:
    """One metered provider call."""
    with tracing.span(f"llm {node}", **_span_attrs(m, node, purpose)):
        return _call_metered(m, node, purpose, system, messages, json_mode, max_tokens, temperature)


def _span_attrs(m: Model, node: str, purpose: str) -> dict:
    return {"gen_ai.system": m.provider, "gen_ai.request.model": m.name, "gen_ai.operation.name": node,
            "cypress.model_id": m.id, "cypress.purpose": purpose}


def _call_metered(m: Model, node: str, purpose: str, system: str, messages: list[dict], json_mode: bool,
                  max_tokens: int, temperature: float) -> str:
    started = time.monotonic()
    try:
        text, usage = _call(m, system, messages, json_mode, max_tokens, temperature)
    except Exception as e:
        # Failed calls can still bill input tokens; record what we sent, estimated.
        _rec(m, node, purpose, finops.Usage(
            input_tokens=finops.estimate_tokens(system, *(x["content"] for x in messages)), estimated=True),
            int((time.monotonic() - started) * 1000), ok=False, error=f"{type(e).__name__}: {e}")
        raise
    _rec(m, node, purpose, usage, int((time.monotonic() - started) * 1000))
    return text


def _call(m: Model, system: str, messages: list[dict], json_mode: bool, max_tokens: int,
          temperature: float) -> tuple[str, finops.Usage]:
    if m.provider == "gemini":
        contents = [{"role": "model" if x["role"] == "assistant" else "user", "parts": [{"text": x["content"]}]}
                    for x in messages]
        cfg = {"system_instruction": system, "temperature": temperature, "max_output_tokens": max_tokens}
        if json_mode:
            cfg["response_mime_type"] = "application/json"
        if m.thinking:
            cfg["thinking_config"] = {"thinking_level": m.thinking}
        r = _gemini(_timeout(), m.region).models.generate_content(model=m.name, contents=contents, config=cfg)
        return (r.text or "").strip(), gemini_usage(r)
    if m.provider == "claude":
        sys = system + ("\nRespond with a single JSON object only." if json_mode else "")
        r = _claude(m.region, _timeout()).messages.create(model=m.name, system=sys, max_tokens=max_tokens,
                                                          temperature=temperature, messages=messages)
        u = r.usage
        cached = (getattr(u, "cache_read_input_tokens", 0) or 0)
        usage = finops.Usage(input_tokens=(u.input_tokens or 0) + cached, cached_tokens=cached,
                             output_tokens=u.output_tokens or 0)
        return "".join(b.text for b in r.content if getattr(b, "type", "") == "text").strip(), usage
    from openai import OpenAI
    host = "aiplatform.googleapis.com" if m.region == "global" else f"{m.region}-aiplatform.googleapis.com"
    base = f"https://{host}/v1/projects/{settings.project}/locations/{m.region}/endpoints/openapi"
    kw = {"response_format": {"type": "json_object"}} if json_mode else {}
    r = OpenAI(base_url=base, api_key=_maas_token(), timeout=_timeout()).chat.completions.create(
        model=m.name, max_tokens=max_tokens, temperature=temperature,
        messages=[{"role": "system", "content": system}, *messages], **kw)
    text = (r.choices[0].message.content or "").strip()
    if r.usage:
        details = getattr(r.usage, "prompt_tokens_details", None)
        usage = finops.Usage(input_tokens=r.usage.prompt_tokens or 0, output_tokens=r.usage.completion_tokens or 0,
                             cached_tokens=(getattr(details, "cached_tokens", 0) or 0) if details else 0)
    else:
        usage = finops.Usage(input_tokens=finops.estimate_tokens(system, *(x["content"] for x in messages)),
                             output_tokens=finops.estimate_tokens(text), estimated=True)
    return text, usage


def probe(model_id: str) -> dict:
    """A tiny metered test call, used by the console to see whether a model works (quota, access)."""
    m = catalog().get(model_id)
    if not m:
        return {"ok": False, "error": "Unknown model"}
    started = time.monotonic()
    try:
        if m.kind == "embed":
            embed_texts(["ok"], node="probe", task="RETRIEVAL_QUERY", model=m)
            text = "(embedding returned)"
        elif m.kind == "rerank":
            from .retrieval import Passage, _local_scores, _vertex_scores
            ps = [Passage(0, 0, "Pool hours", "", "Pool", "", "", "rules", 1, "The pool opens at 10am."),
                  Passage(1, 0, "Dogs", "", "Pets", "", "", "rules", 1, "Dogs must be leashed.")]
            sc = (_local_scores if m.provider == "local" else _vertex_scores)(m, "When does the pool open?", ps)
            _rec(m, "probe", "probe", finops.Usage(requests=1), int((time.monotonic() - started) * 1000))
            text = "pool passage ranked first" if sc.get(0, 0) > sc.get(1, 0) else "unexpected order"
        else:
            text = call(m, "probe", "probe", "Reply with the single word: ok", [{"role": "user", "content": "ping"}],
                        max_tokens=16, temperature=0)
        return {"ok": True, "latency_ms": int((time.monotonic() - started) * 1000), "reply": text[:60]}
    except Exception as e:
        msg = str(e)
        kind = ("quota" if "429" in msg or "RESOURCE_EXHAUSTED" in msg or "Quota" in msg else
                "not_found" if "404" in msg or "NOT_FOUND" in msg else
                "permission" if "403" in msg or "PERMISSION" in msg else "error")
        return {"ok": False, "kind": kind, "error": msg[:300], "latency_ms": int((time.monotonic() - started) * 1000)}


# ---------------- embeddings ----------------

def embed_texts(texts: list[str], *, node: str, task: str, dims: int = 768, model: Model | None = None):
    """Metered embedding call. Returns the raw embedding vectors (not normalized)."""
    from google.genai import types
    m = model or embed_model()
    started = time.monotonic()
    cfg = types.EmbedContentConfig(task_type=task, output_dimensionality=dims)
    try:
        r = _gemini(_timeout() * 2, m.region).models.embed_content(model=m.name, contents=texts, config=cfg)
    except Exception as e:
        _rec(m, node, "primary", finops.Usage(input_tokens=finops.estimate_tokens(*texts), estimated=True),
                      int((time.monotonic() - started) * 1000), ok=False, error=f"{type(e).__name__}: {e}")
        raise
    counts = [getattr(getattr(e, "statistics", None), "token_count", None) for e in r.embeddings]
    if all(c is not None for c in counts):
        usage = finops.Usage(input_tokens=int(sum(counts)))
    else:
        usage = finops.Usage(input_tokens=finops.estimate_tokens(*texts), estimated=True)
    _rec(m, node, "primary", usage, int((time.monotonic() - started) * 1000))
    return [list(e.values) for e in r.embeddings]


def ocr_page(png: bytes, prompt: str) -> str:
    """Metered OCR of one page image with the model assigned to the ocr node."""
    from google.genai import types
    m = gemini_for("ocr")
    started = time.monotonic()
    try:
        r = _gemini(_timeout() * 4, m.region).models.generate_content(
            model=m.name, contents=[types.Part.from_bytes(data=png, mime_type="image/png"), prompt],
            config={"temperature": 0, "max_output_tokens": 8192})
    except Exception as e:
        # An image page is roughly 1,300 input tokens on Gemini.
        _rec(m, "ocr", "primary", finops.Usage(input_tokens=1300, estimated=True),
                      int((time.monotonic() - started) * 1000), ok=False, error=f"{type(e).__name__}: {e}")
        raise
    _rec(m, "ocr", "primary", gemini_usage(r), int((time.monotonic() - started) * 1000))
    return r.text or ""


def generate_vision(node: str, system: str, prompt: str, images: list[tuple[bytes, str]], *,
                    max_tokens: int = 1024, temperature: float = 0.0) -> tuple[str, str]:
    """One metered image-input call (Gemini) for `node`. images: [(bytes, mime type)]. JSON out.
    Returns (text, model_id)."""
    from google.genai import types
    m = gemini_for(node)
    temperature, max_tokens = _params(node, temperature, max_tokens)
    parts = [types.Part.from_bytes(data=b, mime_type=mt) for b, mt in images] + [prompt]
    cfg = {"system_instruction": system, "temperature": temperature, "max_output_tokens": max_tokens,
           "response_mime_type": "application/json"}
    if m.thinking:
        cfg["thinking_config"] = {"thinking_level": m.thinking}
    with tracing.span(f"llm {node}", **_span_attrs(m, node, "primary"), images=len(images)):
        started = time.monotonic()
        try:
            r = _gemini(_timeout() * 2, m.region).models.generate_content(model=m.name, contents=parts, config=cfg)
        except Exception as e:
            _rec(m, node, "primary", finops.Usage(input_tokens=1300 * len(images), estimated=True),
                 int((time.monotonic() - started) * 1000), ok=False, error=f"{type(e).__name__}: {e}")
            raise
        _rec(m, node, "primary", gemini_usage(r), int((time.monotonic() - started) * 1000))
    return r.text or "", m.id


def parse_json(text: str) -> dict:
    """Tolerant JSON parse: strips code fences and finds the first object."""
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())
    try:
        return json.loads(t)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", t, re.S)
        return json.loads(m.group(0)) if m else {}


# ---------------- tool calling ----------------

MAX_TOOL_ROUNDS = 3


def generate_with_tools(node: str, system: str, messages: list[dict], tools: list, run_tool,
                        *, max_tokens: int = 1024, temperature: float = 0.2,
                        max_rounds: int = MAX_TOOL_ROUNDS) -> tuple[str, str, list[dict]]:
    """Function-calling loop. `tools` are agent.tools.Tool; `run_tool(name, args) -> dict`.

    Tool calling is wired for Gemini only so far: a non-Gemini assignment runs on the node's default
    (or the fallback) for this step. Every round is metered.
    Returns (final text, model_id, [{"tool", "args", "result"}]).
    """
    from google.genai import types

    m = gemini_for(node)
    temperature, max_tokens = _params(node, temperature, max_tokens)
    fb = fallback() if fallback().provider in TOOL_PROVIDERS else EMERGENCY
    contents = [types.Content(role="model" if x["role"] == "assistant" else "user", parts=[types.Part(text=x["content"])])
                for x in messages]
    decls = [types.Tool(function_declarations=[
        types.FunctionDeclaration(name=t.name, description=t.description, parameters_json_schema=t.parameters)
        for t in tools])] if tools else None

    def cfg_for(model: Model):
        return types.GenerateContentConfig(
            system_instruction=system, temperature=temperature, max_output_tokens=max_tokens, tools=decls,
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
            thinking_config=types.ThinkingConfig(thinking_level=model.thinking) if model.thinking else None)

    calls: list[dict] = []

    def run(model: Model, purpose: str):
        with tracing.span(f"llm {node}", **_span_attrs(model, node, purpose)):
            return _run(model, purpose)

    def _run(model: Model, purpose: str):
        started = time.monotonic()
        try:
            r = _gemini(_timeout(), model.region).models.generate_content(model=model.name, contents=contents,
                                                                           config=cfg_for(model))
        except Exception as e:
            _rec(model, node, purpose, finops.Usage(
                input_tokens=finops.estimate_tokens(system, *(x["content"] for x in messages)), estimated=True),
                int((time.monotonic() - started) * 1000), ok=False, error=f"{type(e).__name__}: {e}")
            raise
        _rec(model, node, purpose, gemini_usage(r), int((time.monotonic() - started) * 1000))
        return r

    def step():
        nonlocal m
        if m.id != fb.id:
            try:
                return run(m, "primary")
            except Exception as e:
                log.warning("%s failed in tool loop for %s (%s); continuing on %s", m.id, node, e, fb.id)
                m = fb
                return run(fb, "fallback")
        return run(fb, "primary")

    for _ in range(max_rounds + 1):
        r = step()
        fcs = r.function_calls or []
        if not fcs or len(calls) >= max_rounds:
            return (r.text or "").strip(), m.id, calls
        contents.append(r.candidates[0].content)  # keeps Gemini's thought signatures intact
        parts = []
        for fc in fcs:
            with tracing.span(f"tool {fc.name}", **{"gen_ai.tool.name": fc.name}):
                result = run_tool(fc.name, dict(fc.args or {}))
            calls.append({"tool": fc.name, "args": dict(fc.args or {}), "result": result})
            parts.append(types.Part.from_function_response(name=fc.name, response=result))
        contents.append(types.Content(role="user", parts=parts))
    return "", m.id, calls


# ---------------- streaming ----------------

def generate_stream(node: str, system: str, messages: list[dict], on_token, *, on_reset=lambda: None,
                    max_tokens: int = 1024, temperature: float = 0.2) -> tuple[str, str]:
    """Like generate(), but calls on_token(text) as Gemini produces it.

    Hedged: if the first token hasn't arrived after stream_hedge_s, the fallback model is asked too
    and whichever produces text first is used. If the stream breaks mid-answer, on_reset() is
    called and the fallback's whole answer is sent. Non-Gemini models send one chunk. The stream's
    usage (from its final chunk) is metered even if the hedge wins."""
    m, fb = model_for(node), fallback()
    temperature, max_tokens = _params(node, temperature, max_tokens)
    if m.provider != "gemini":
        text, mid = generate(node, system, messages, max_tokens=max_tokens, temperature=temperature)
        on_token(text)
        return text, mid
    contents = [{"role": "model" if x["role"] == "assistant" else "user", "parts": [{"text": x["content"]}]}
                for x in messages]
    cfg = {"system_instruction": system, "temperature": temperature, "max_output_tokens": max_tokens}
    if m.thinking:
        cfg["thinking_config"] = {"thinking_level": m.thinking}
    rt = runtime()
    events: queue.Queue = queue.Queue()
    ctx = finops.context()

    def primary():
        with tracing.span(f"llm {node}", **_span_attrs(m, node, "primary"), stream=True):
            _primary()

    def _primary():
        started, last, out = time.monotonic(), None, []
        try:
            for chunk in _gemini(_timeout(), m.region).models.generate_content_stream(model=m.name, contents=contents,
                                                                                     config=cfg):
                if getattr(chunk, "usage_metadata", None):
                    last = chunk
                if t := chunk.text:
                    out.append(t)
                    events.put(("tok", t))
            usage = gemini_usage(last) if last else finops.Usage(
                input_tokens=finops.estimate_tokens(system, *(x["content"] for x in messages)),
                output_tokens=finops.estimate_tokens("".join(out)), estimated=True)
            _rec(m, node, "primary", usage, int((time.monotonic() - started) * 1000))
            events.put(("end", None))
        except Exception as e:
            usage = gemini_usage(last) if last else finops.Usage(
                input_tokens=finops.estimate_tokens(system, *(x["content"] for x in messages)),
                output_tokens=finops.estimate_tokens("".join(out)), estimated=True)
            _rec(m, node, "primary", usage, int((time.monotonic() - started) * 1000), ok=False,
                          error=f"{type(e).__name__}: {e}")
            events.put(("err", e))

    def backup(purpose: str):
        try:
            events.put(("backup", call(fb, node, purpose, system, messages, False, max_tokens, temperature)))
        except Exception as e:
            events.put(("backup_err", e))

    threading.Thread(target=ctx.run, args=(primary,), daemon=True).start()
    parts: list[str] = []
    backup_started, primary_failed, backup_failed = False, False, False
    deadline = time.monotonic() + float(rt["stream_hedge_s"])

    def start_backup(purpose: str):
        nonlocal backup_started
        if not backup_started:
            backup_started = True
            threading.Thread(target=finops.context().run, args=(backup, purpose), daemon=True).start()

    while True:
        wait_s = max(0.0, deadline - time.monotonic()) if not parts and not backup_started else _timeout() + 5
        try:
            kind, val = events.get(timeout=wait_s)
        except queue.Empty:
            if not parts and not backup_started:
                log.info("%s slow to start for %s; hedging with %s", m.id, node, fb.id)
                start_backup("hedge")
                continue
            break
        if kind == "tok":
            parts.append(val)
            on_token(val)
        elif kind == "end":
            if parts:
                return "".join(parts).strip(), m.id
            primary_failed = True
            start_backup("fallback")
        elif kind == "err":
            log.warning("%s stream failed for %s (%s)", m.id, node, val)
            primary_failed = True
            if parts:
                parts.clear()
                on_reset()
            start_backup("fallback")
        elif kind == "backup" and not parts:
            on_token(val)
            return val, fb.id
        elif kind == "backup_err":
            backup_failed = True
        if primary_failed and backup_failed:
            break
    raise TimeoutError(f"no answer from {m.id} or {fb.id}")
