"""Tree-of-thought policy search: grounding gate, beam, protected null branch, ties, budget. Model replies are scripted."""

import json
from dataclasses import dataclass

import pytest

from cypress import models, policy_tot as tot


@dataclass
class P:
    title: str
    section: str
    content: str
    authority: int = 50
    doc_type: str = "rules"
    uri: str = ""


PASSAGES = [
    P("Park rules", "Park rules", "Docks are for temporary docking only.\n- No cars, boats, trailers or vehicles of any kind "
      "may be parked overnight on park grounds. Violators will be towed."),
    P("Golf cart agreement", "Agreement", "Golf carts must be parked in the designated area when not in use."),
    P("Bylaws", "Article IV", "The board shall give written notice and an opportunity to be heard before any fine.",
      authority=90, doc_type="bylaws"),
]


@pytest.fixture
def script(monkeypatch):
    """Queue replies per node; record calls."""
    replies = {"case_policy_gen": [], "case_policy_critic": []}
    calls = []

    def fake(node, system, messages, **kw):
        calls.append(node)
        return json.dumps(replies[node].pop(0)), "fake"
    monkeypatch.setattr(models, "generate", fake)
    monkeypatch.setattr(tot, "_system", lambda key: key)
    return replies, calls


def test_grounded_ignores_case_punctuation_and_markdown():
    assert tot.grounded("no cars, boats, TRAILERS or vehicles of any kind", "- **No cars, boats, trailers** or vehicles of any kind may")
    assert not tot.grounded("no trailers after dark", PASSAGES[0].content)
    assert not tot.grounded("no", PASSAGES[0].content)          # too short to mean anything


def test_full_search_picks_strict_reading_with_process(script):
    replies, calls = script
    replies["case_policy_gen"] += [
        {"candidates": [
            {"source": 1, "quote": "No cars, boats, trailers or vehicles of any kind may be parked overnight on park grounds",
             "rule": "No overnight parking", "why": "trailer left overnight"},
            {"source": 2, "quote": "Golf carts must be parked in the designated area", "rule": "Golf cart parking", "why": "parking"},
            {"source": 1, "quote": "Trailers are banned at all times", "rule": "Invented", "why": "made up"}]},
        {"readings": [{"id": "r1", "kind": "strict", "reading": "A trailer stayed on park grounds overnight."},
                      {"id": "r1", "kind": "broad", "reading": "Any parking at all is suspect."}]},
        {"process": "Written notice and a chance to be heard before any fine.",
         "quote": "written notice and an opportunity to be heard before any fine", "source": 3}]
    replies["case_policy_critic"] += [
        {"scores": [{"id": "r1", "fit": 5, "scope": 5}, {"id": "r2", "fit": 2, "scope": 5}]},
        {"scores": [{"id": "r1s", "fit": 5, "scope": 5}, {"id": "r1b", "fit": 3, "scope": 4}]}]
    out = tot.run("Trailer left overnight in the park lot", PASSAGES)
    nodes = {n["id"]: n for n in out["tot"]["nodes"]}
    assert nodes["r3"]["status"] == "pruned" and "quote" in nodes["r3"]["why_pruned"]   # invented quote: code gate
    assert nodes["r2"]["status"] == "pruned"                                            # scored 2
    assert nodes["r1s"]["status"] == "selected" and nodes["none"]["status"] == "pruned"
    assert out["rules"][0]["source"] == 1 and out["confidence"] == "high"
    assert "notice" in out["process"]
    assert out["runner_up"]["reading"].startswith("Any parking")
    assert out["tot"]["calls"] == 5 == len(calls) and not out["tot"]["partial"]


def test_nothing_survives_so_no_rule_applies(script):
    replies, _ = script
    replies["case_policy_gen"] += [{"candidates": [
        {"source": 1, "quote": "No cars, boats, trailers or vehicles of any kind may be parked overnight", "rule": "Parking",
         "why": "a stretch"}]}]
    replies["case_policy_critic"] += [{"scores": [{"id": "r1", "fit": 1, "scope": 1, "reason": "noise at a private home"}]}]
    out = tot.run("Loud music at a private home off association property", PASSAGES)
    assert out["rules"] == [] and out["confidence"] == "low" and out["process"] == ""
    assert next(n for n in out["tot"]["nodes"] if n["id"] == "none")["status"] == "selected"
    assert all(r["fit"] != "strong" for r in out["readings"])


def test_ties_go_to_higher_authority(script):
    replies, _ = script
    replies["case_policy_gen"] += [
        {"candidates": [{"source": 1, "quote": "Violators will be towed", "rule": "Towing", "why": ""},
                        {"source": 3, "quote": "The board shall give written notice", "rule": "Notice", "why": ""}]},
        {"readings": [{"id": "r1", "kind": "strict", "reading": "a"}, {"id": "r2", "kind": "strict", "reading": "b"}]},
        {"process": "", "quote": "", "source": None}]
    replies["case_policy_critic"] += [{"scores": [{"id": "r1", "fit": 4, "scope": 4}, {"id": "r2", "fit": 4, "scope": 4}]},
                                      {"scores": [{"id": "r1s", "fit": 4, "scope": 4}, {"id": "r2s", "fit": 4, "scope": 4}]}]
    out = tot.run("x", PASSAGES)
    assert out["rules"][0]["source"] == 3                       # bylaws (authority 90) beat park rules (50)


def test_budget_stops_early_and_marks_partial(script, monkeypatch):
    replies, _ = script
    monkeypatch.setattr(tot, "MAX_CALLS", 2)
    replies["case_policy_gen"] += [{"candidates": [
        {"source": 1, "quote": "No cars, boats, trailers or vehicles of any kind may be parked overnight", "rule": "Parking", "why": ""}]}]
    replies["case_policy_critic"] += [{"scores": [{"id": "r1", "fit": 4, "scope": 5}]}]
    out = tot.run("Trailer overnight", PASSAGES)
    assert out["tot"]["partial"] and out["tot"]["calls"] == 2
    assert out["rules"][0]["source"] == 1 and out["confidence"] == "high"   # best node so far
