"""Tree-of-Thought rule finding for the incident case file's policy step.

A linear pass tends to commit to the first plausible passage, stretch a rule to fit, and check a rule's
conditions unevenly. This explores a small tree instead and keeps a runner-up for the board:

  depth 1  which rule applies     generator proposes up to 3 readings; "no rule applies" is always a branch
  depth 2  how it applies         each surviving reading is refined into a strict and a broad reading
  depth 3  what process follows   the process the documents require, for the winning reading only

Search is a beam of width 2. Each node is scored 1-5 as the lower of fit and scope (the critic model). Two code
checks come first, so a fluent reading can't win on style: a reading's quote must appear word for word in the
passage it cites (otherwise it's pruned), and ties go to the higher-authority document, then the narrower
reading. A node scoring under 3 is pruned. "No rule applies" is never pruned; it wins when nothing else survives.

Budget: at most MAX_CALLS model calls and MAX_SECONDS; past either, the best node so far is returned, marked partial.
Every node, score and pruning reason is returned in the trace, so the case file shows what was rejected and why.
"""

import logging
import re
import time

from . import models

log = logging.getLogger(__name__)

BEAM = 2
MAX_CANDIDATES = 3
PRUNE_BELOW = 3
MAX_CALLS = 8
MAX_SECONDS = 60
DOC_ORDER = {"bylaws": 0, "deed": 1, "court_record": 2, "rules": 3, "minutes": 4, "faq": 5}
NONE_ID = "none"


class Budget(Exception):
    pass


def _norm(s: str) -> str:
    s = re.sub(r"[*_`#>\[\]]", "", (s or "").lower())
    s = s.replace("’", "'").replace("“", '"').replace("”", '"')
    return re.sub(r"\s+", " ", re.sub(r"[^\w'\" ]+", " ", s)).strip()


def grounded(quote: str, passage: str) -> bool:
    """The quote's words appear, in order and contiguously, in the passage (ignoring case, punctuation, Markdown)."""
    q = _norm(quote)
    return len(q) >= 8 and q in _norm(passage)


def _system(key: str) -> str:
    from .agent import prompts
    return prompts.system(key)


class Search:
    def __init__(self, report_text: str, passages: list):
        self.report = report_text
        self.passages = passages
        self.numbered = "\n\n".join(f"[{i}] {p.title} - {p.section}\n{p.content[:1500]}" for i, p in enumerate(passages, 1))
        self.calls = 0
        self.started = time.monotonic()
        self.nodes: list[dict] = []

    # ---- model calls, within budget ----
    def _call(self, node: str, user: str, temperature: float) -> dict:
        if self.calls >= MAX_CALLS or time.monotonic() - self.started > MAX_SECONDS:
            raise Budget()
        self.calls += 1
        key = "case_policy_gen" if node == "case_policy_gen" else "case_policy_critic"
        text, _ = models.generate(node, _system(key), [{"role": "user", "content": user}], json_mode=True,
                                  max_tokens=2048, temperature=temperature)
        try:
            j = models.parse_json(text)
        except ValueError:
            j = {}
        if isinstance(j, list):         # some models answer with the bare array; accept it as the list asked for
            first = j[0] if j and isinstance(j[0], dict) else {}
            j = {**first, "candidates": j, "readings": j, "scores": j}
        return j if isinstance(j, dict) else {}

    def generate(self, task: str) -> dict:
        return self._call("case_policy_gen", f"Incident:\n{self.report}\n\nPassages:\n{self.numbered}\n\nTask:\n{task}", 0.3)

    def critique(self, nodes: list[dict]) -> None:
        """Score nodes in place: score = min(fit, scope); missing or unreadable scores count as 1."""
        items = "\n\n".join(f"id: {n['id']}\nrule (passage [{n['source']}]): {n['quote']}\nreading: {n['text']}" for n in nodes)
        j = self._call("case_policy_critic", f"Incident:\n{self.report}\n\nCandidate readings:\n{items}", 0.0)
        by_id = {str(s.get("id")): s for s in j.get("scores") or [] if isinstance(s, dict)}
        for n in nodes:
            s = by_id.get(n["id"]) or {}
            fit, scope = _score(s.get("fit")), _score(s.get("scope"))
            n.update(fit=fit, scope=scope, score=min(fit, scope), reason=str(s.get("reason") or "")[:300])

    # ---- helpers ----
    def passage(self, n: int):
        return self.passages[n - 1] if isinstance(n, int) and 1 <= n <= len(self.passages) else None

    def add(self, **n) -> dict:
        n.setdefault("status", "open")
        self.nodes.append(n)
        return n

    def rank_key(self, n: dict):
        p = self.passage(n.get("source"))
        authority = p.authority if p else 0
        doc = DOC_ORDER.get(p.doc_type if p else "", 9)
        narrower = 1 if n.get("kind") == "strict" else 0
        return (n.get("score", 0), authority, -doc, narrower)

    def select(self, nodes: list[dict]) -> list[dict]:
        """Prune under the threshold, keep the top BEAM, mark the rest."""
        for n in nodes:
            if n.get("score", 0) < PRUNE_BELOW:
                n.update(status="pruned", why_pruned=f"scored {n.get('score', 0)} of 5 (below {PRUNE_BELOW})")
        alive = sorted((n for n in nodes if n["status"] == "open"), key=self.rank_key, reverse=True)
        for n in alive[BEAM:]:
            n.update(status="pruned", why_pruned=f"outside the beam of {BEAM}")
        for n in alive[:BEAM]:
            n["status"] = "kept"
        return alive[:BEAM]


def _score(v) -> int:
    try:
        return max(1, min(5, int(v)))
    except (TypeError, ValueError):
        return 1


DEPTH1 = ("Propose up to three DIFFERENT candidate rules from the passages that could apply to this incident, most likely "
          "first. For each, copy the exact words from the passage that state the rule (a verbatim quote under 40 words) and "
          "say in one sentence how it might apply. Propose fewer, or none, if fewer apply; never invent a rule.\n"
          'JSON: {"candidates": [{"source": 1, "quote": "exact words from passage 1", "rule": "the rule in plain words", '
          '"why": "how it might apply"}]}')
DEPTH2 = ("For each candidate rule below, write two readings of how it applies to this incident. strict: only what the "
          "rule's words plainly cover, given the facts actually in the report. broad: a wider interpretation someone might "
          "argue. List the rule's conditions and whether the report shows each one met, unmet or unknown.\n"
          "Candidates:\n{cands}\n"
          'JSON: {"readings": [{"id": "r1", "kind": "strict", "reading": "...", "conditions": [{"condition": "...", '
          '"status": "met|unmet|unknown"}]}]}')
DEPTH3 = ("This rule applies: {rule} (passage [{source}]). From the passages, what process do the documents require "
          "before or as a penalty (notice, hearing, appeal, fine, towing)? Quote the words you rely on. If the passages "
          "don't say, leave process empty.\n"
          'JSON: {"process": "what the documents require, or empty", "quote": "exact words, or empty", "source": 1}')


def run(report_text: str, passages: list) -> dict:
    """Returns the policy finding in the same shape as the linear step, plus a "tot" trace."""
    s = Search(report_text, passages)
    s.add(id="root", depth=0, parent=None, text="Incident + retrieved passages", status="kept")
    null = s.add(id=NONE_ID, depth=1, parent="root", source=None, quote="", text="No rule in these passages applies",
                 status="held")
    partial = False
    best = None
    try:
        # depth 1: which rule
        j = s.generate(DEPTH1)
        cands = []
        for i, c in enumerate((j.get("candidates") or [])[:MAX_CANDIDATES], 1):
            if not isinstance(c, dict):
                continue
            src = c.get("source") if isinstance(c.get("source"), int) else None
            p = s.passage(src)
            n = s.add(id=f"r{i}", depth=1, parent="root", source=src, quote=str(c.get("quote") or "")[:400],
                      text=str(c.get("rule") or "")[:300], why=str(c.get("why") or "")[:300])
            if not p or not grounded(n["quote"], p.content):
                n.update(status="pruned", score=0,
                         why_pruned="its quote isn't in the passage it cites" if p else "cites no retrieved passage")
            else:
                cands.append(n)
        if cands:
            s.critique(cands)
        level1 = s.select(cands)

        # depth 2: how it applies (strict vs broad)
        if level1:
            listing = "\n".join(f"{n['id']}: {n['text']} (passage [{n['source']}]: \"{n['quote']}\")" for n in level1)
            j = s.generate(DEPTH2.replace("{cands}", listing))
            by_parent = {n["id"]: n for n in level1}
            kids = []
            for r in j.get("readings") or []:
                rid = str(r.get("id") or "") if isinstance(r, dict) else ""
                parent = by_parent.get(rid) or by_parent.get(rid[:2]) or (level1[0] if len(level1) == 1 else None)
                if not isinstance(r, dict) or parent is None:
                    continue
                kind = "broad" if r.get("kind") == "broad" else "strict"
                if any(k["parent"] == parent["id"] and k["kind"] == kind for k in kids):
                    continue
                conds = [c for c in r.get("conditions") or [] if isinstance(c, dict)][:6]
                kids.append(s.add(id=f"{parent['id']}{kind[0]}", depth=2, parent=parent["id"], kind=kind,
                                  source=parent["source"], quote=parent["quote"], rule=parent["text"],
                                  text=str(r.get("reading") or "")[:400], conditions=conds))
            if kids:
                s.critique(kids)
            level2 = s.select(kids)
            # A refinement that fails (unparseable, or every reading pruned) doesn't erase a rule that already survived
            # depth 1: fall back to the best depth-1 reading.
            best = level2[0] if level2 else level1[0]

        # depth 3: what process (winning reading only)
        if best:
            j = s.generate(DEPTH3.replace("{rule}", best["rule"]).replace("{source}", str(best["source"])))
            quote = str(j.get("quote") or "")
            # Models often cite the wrong passage number for a correct quote: accept the passage that contains it.
            src = next((i for i, p in enumerate(s.passages, 1) if quote and grounded(quote, p.content)), None)
            ok = bool(j.get("process")) and src is not None
            s.add(id=f"{best['id']}p", depth=3, parent=best["id"], text=str(j.get("process") or "")[:600] if ok else "",
                  source=src, quote=quote[:400] if ok else "",
                  status="selected" if ok else "pruned",
                  **({} if ok else {"why_pruned": "no process stated with a quote from the passages",
                                    "raw": str(j)[:400]}))
    except Budget:
        partial = True
        log.info("policy ToT stopped at its budget after %s calls", s.calls)
        if best is None:
            open_ = [n for n in s.nodes if n.get("depth", 0) >= 1 and n["id"] != NONE_ID and n.get("status") == "kept"]
            best = max(open_, key=s.rank_key) if open_ else None

    return _result(s, best, null, partial)


def _result(s: Search, best: dict | None, null: dict, partial: bool) -> dict:
    proc = next((n for n in s.nodes if n.get("depth") == 3 and n["status"] == "selected"), None)
    if best:
        best["status"] = "selected"
        null.update(status="pruned", why_pruned="a rule survived")
        score = best.get("score", 0)
        rules = [{"rule": best.get("rule") or best["text"], "source": best["source"],
                  "why": best.get("text") if best.get("depth") == 2 else best.get("why", "")}]
        confidence = "high" if score >= 4 else "medium"
    else:
        null["status"] = "selected"
        rules, confidence = [], "low"
    runner = next((n for n in sorted((n for n in s.nodes if n.get("depth") == 2 and n["status"] == "kept" and n is not best),
                                     key=s.rank_key, reverse=True)), None)
    readings = [{"rule": n["text"], "source": n["source"],
                 "fit": "strong" if n.get("score", 0) >= 4 else "weak" if n.get("score", 0) >= PRUNE_BELOW else "none"}
                for n in s.nodes if n.get("depth") == 1 and n["id"] != NONE_ID]
    return {"rules": rules, "readings": readings, "process": proc["text"] if proc else "", "confidence": confidence,
            "runner_up": ({"rule": runner.get("rule"), "reading": runner["text"], "source": runner["source"],
                           "score": runner.get("score")} if runner else None),
            "tot": {"nodes": [{k: v for k, v in n.items()} for n in s.nodes], "calls": s.calls, "partial": partial,
                    "seconds": round(time.monotonic() - s.started, 1)}}
