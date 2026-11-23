"""LLM runtime for criteria parses (issue 56): prompt, schema, cache, source, batch.

No model runs here: a fake backend stands in, and the server backend talks
to an httpx mock transport. What is pinned is the plumbing and its safety:
the prompt carries no patient data, every failure falls back to regex, every
model call is audited, and the cache key covers model and prompt version.
"""

from __future__ import annotations

import inspect
import json

import httpx
import pytest

from agent import criteria_llm as L
from agent import parse_batch
from agent.criteria_parse import FIELDS, Invalid, Parse, validate_parse
from agent.eligibility import Criterion, Facts, check_trial
from agent.schemas import TrialRecord

C = Criterion("inclusion", "Participants must be ≥12 weeks from completion of radiation.")
GOOD = {"kind": "requirement", "applies": "always", "condition": [], "combine": "all", "requirements": [
    {"quote": "≥12 weeks from completion of radiation", "field": "prior_treatments.radiation.weeks_ago",
     "op": ">=", "value": 12}]}


class FakeBackend:
    def __init__(self, model_id: str = "fake-model", reply: str | Exception = json.dumps(GOOD)) -> None:
        self.model_id = model_id
        self.reply = reply
        self.calls: list[list[dict[str, str]]] = []

    def complete(self, messages, schema):
        self.calls.append(messages)
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply


def _source(tmp_path, backend: FakeBackend | None = None, mode: L.ParseMode = "live") -> L.LlmParseSource:
    return L.LlmParseSource(L.ParseCache(tmp_path / "p.json"), "fake-model", backend=backend, mode=mode)


# ---------------------------------------------------------------------------
# Prompt and schema
# ---------------------------------------------------------------------------


def test_prompt_takes_only_the_criterion():
    """Rule 9 / issue 56 scope: no patient fact can reach the model."""
    assert list(inspect.signature(L.build_messages).parameters) == ["criterion"]
    msgs = L.build_messages(C)
    assert msgs[0]["role"] == "system" and msgs[-1] == {"role": "user", "content": f"Inclusion criterion:\n{C.text}"}


def test_few_shot_examples_pass_validation():
    shots = L.FEW_SHOT
    for user, assistant in zip(shots[::2], shots[1::2]):
        kind, text = user["content"].split(" criterion:\n", 1)
        out = validate_parse(json.loads(assistant["content"]), Criterion(kind.lower(), text))  # type: ignore[arg-type]
        assert isinstance(out, Parse), (text, out)


def test_schema_fields_are_the_validator_fields():
    s = L.response_schema()
    req = s["properties"]["requirements"]["items"]
    assert req["properties"]["field"]["enum"] == sorted(FIELDS)
    assert s["properties"]["condition"]["items"] == req
    assert set(s["required"]) == set(s["properties"])


def test_prompt_documents_every_field():
    for name in FIELDS:
        generic = name.split(".")[0] + ".<type>." + name.split(".")[-1] if name.count(".") == 2 else name
        assert name in L.SYSTEM_PROMPT or generic in L.SYSTEM_PROMPT, name


# ---------------------------------------------------------------------------
# Cache and source
# ---------------------------------------------------------------------------


def test_cache_key_covers_model_and_prompt_version():
    k = L.cache_key(C, "a")
    assert k != L.cache_key(C, "b")
    assert k != L.cache_key(C, "a", "p0")
    assert k != L.cache_key(Criterion("exclusion", C.text), "a")
    assert k == L.cache_key(Criterion("inclusion", "Participants must be  ≥12 weeks from completion of\nradiation."), "a")


def test_live_miss_calls_model_caches_and_audits(tmp_path, audit_log):
    be = FakeBackend()
    src = _source(tmp_path, be)
    assert src(C) == GOOD
    assert src(C) == GOOD
    assert len(be.calls) == 1
    stats = src.take_stats()
    assert stats | {} == {"model": "fake-model", "prompt_version": L.PROMPT_VERSION, "cache_hits": 1,
                          "cache_misses": 1, "model_calls": 1, "model_errors": 0}
    entries = [e for e in audit_log.read() if e.action == "criterion_parsed"]
    assert len(entries) == 1
    d = entries[0].detail
    assert d["model"] == "fake-model" and d["prompt_version"] == L.PROMPT_VERSION
    assert d["cache_hit"] is False and d["ok"] is True
    assert src.take_stats()["cache_hits"] == 0  # reset after take


def test_cache_persists(tmp_path):
    src = _source(tmp_path, FakeBackend())
    src(C)
    src.cache.save()
    again = L.LlmParseSource(L.ParseCache(tmp_path / "p.json"), "fake-model", mode="cached_only")
    assert again(C) == GOOD
    entry = again.cache.get(L.cache_key(C, "fake-model"))
    assert entry["text"] == C.text and entry["criterion_type"] == "inclusion"


def test_cached_only_never_calls_the_model(tmp_path):
    be = FakeBackend()
    assert _source(tmp_path, be, mode="cached_only")(C) is None
    assert be.calls == []


@pytest.mark.parametrize("reply", [httpx.ConnectError("down"), "not json {", TimeoutError("slow")])
def test_model_failure_is_none_and_audited(tmp_path, audit_log, reply):
    src = _source(tmp_path, FakeBackend(reply=reply))
    assert src(C) is None
    assert len(src.cache) == 0
    e = [e for e in audit_log.read() if e.action == "criterion_parsed"][-1]
    assert e.detail["ok"] is False and "error" in e.detail


def test_backend_model_must_match_cache_model(tmp_path):
    with pytest.raises(ValueError):
        L.LlmParseSource(L.ParseCache(tmp_path / "p.json"), "other", backend=FakeBackend())


def test_server_backend_request():
    seen = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["url"] = str(req.url)
        seen["body"] = json.loads(req.content)
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(GOOD)}}]})

    be = L.ServerBackend("http://127.0.0.1:8080/", "m", client=httpx.Client(transport=httpx.MockTransport(handler)))
    assert L.parse_with(be, C) == GOOD
    body = seen["body"]
    assert seen["url"] == "http://127.0.0.1:8080/v1/chat/completions"
    assert body["temperature"] == 0 and body["seed"] == 0
    assert body["response_format"]["json_schema"]["schema"] == L.response_schema()


# ---------------------------------------------------------------------------
# check_trial and the batch runner
# ---------------------------------------------------------------------------


def _trial(text: str) -> TrialRecord:
    return TrialRecord(trial_id="NCT99000001", title="t", source="ctgov", recruitment_status="RECRUITING",
                       criteria_text=text, remote_participation=False,
                       fetched_at="2026-01-01T00:00:00Z", from_cache=True)


def test_check_trial_audits_parse_stats(tmp_path, audit_log):
    from agent.schemas import PriorTreatment

    facts = Facts(stage="post_pathology", bucket="glioma", prior_treatments=(
        PriorTreatment(type="radiation", weeks_ago=40),))
    src = _source(tmp_path, FakeBackend())
    rows = check_trial(_trial(f"Inclusion Criteria:\n* {C.text}"), facts, parses=src)
    assert [r.status for r in rows] == ["met"]
    e = [e for e in audit_log.read() if e.action == "eligibility_checked"][-1]
    assert e.detail["decided_by_parse"] == 1
    assert e.detail["parse_stats"]["model_calls"] == 1


def test_batch_dedupes_skips_cached_and_reports(tmp_path):
    be = FakeBackend()
    src = _source(tmp_path, be)
    dup = Criterion("inclusion", "Participants  must be ≥12 weeks from completion of radiation.")
    other = Criterion("exclusion", "Known hypersensitivity to the study drug.")
    rep = parse_batch.run(src, [C, dup, other], workers=2, log=lambda *_: None)
    assert (rep.bullets, rep.unique, rep.parsed, rep.errors) == (3, 2, 2, 0)
    assert rep.invalid == 1  # GOOD's quote is not in the hypersensitivity bullet
    assert (tmp_path / "p.json").exists()
    rep2 = parse_batch.run(src, [C, other], workers=2, log=lambda *_: None)
    assert (rep2.already_cached, rep2.parsed) == (2, 0)
    assert len(be.calls) == 2


def test_batch_fixture_source_has_the_labeled_bullets():
    crit = parse_batch.from_fixtures()
    assert len(crit) >= 658
    assert isinstance(validate_parse(GOOD, C), Parse) and not isinstance(validate_parse(GOOD, C), Invalid)
