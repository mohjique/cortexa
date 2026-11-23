"""LLM runtime for criteria parses (issue 56): prompt, backends, cache, source.

`agent.criteria_parse` decides what a parse is worth. This module gets one:

- `build_messages(criterion)`: the prompt. Its only input is a `Criterion`
  (inclusion/exclusion + public registry text), so no patient fact can reach
  the model (safety rule 9; the LLM never decides eligibility, rule 1).
- `response_schema()`: JSON schema for constrained decoding, built from the
  same closed field list `criteria_parse.FIELDS` validates against.
- Backends, both at temperature 0 with a fixed seed so parses repeat:
  `ServerBackend` talks to an OpenAI-compatible server (llama.cpp
  `llama-server`; used for GPU batch runs with parallel slots), and
  `LlamaCppBackend` runs a GGUF model in-process with `llama-cpp-python`
  (CPU, for the free-tier Space). Neither is imported unless used.
- `ParseCache`: one JSON file of parses, keyed by criterion type + text +
  model + prompt version. A bullet parses the same way in every trial and
  for every patient.
- `LlmParseSource`: the `ParseSource` `check_trial` takes. Cache first; on a
  miss it calls the backend only in `live` mode. Any failure gives None,
  which leaves the regex checker's answer (exactly today's behaviour).

Audit (rule 7): every model call is one `criterion_parsed` entry (model,
prompt version, cache key, duration, ok). Cache hits are counted per trial in
the `eligibility_checked` entry (`parse_stats`), so a 300-trial search does
not write thousands of entries.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal, Protocol

import httpx

from agent.audit import audit
from agent.criteria_parse import ENTITIES, FIELDS, TREATMENTS, Criterion, criterion_key

PROMPT_VERSION = "p1"
CACHE_DIR = Path(__file__).resolve().parent / "parse_cache"
ParseMode = Literal["cached_only", "live"]

# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------

_TREATMENT_MEANING = {
    "surgery": "resection or biopsy of the brain tumor",
    "radiation": "radiotherapy to the brain tumor",
    "temozolomide": "temozolomide only (NOT other chemotherapy)",
    "bevacizumab": "bevacizumab (Avastin)",
    "idh_inhibitor": "an IDH inhibitor (vorasidenib, ivosidenib, olutasidenib)",
    "checkpoint_inhibitor": "a PD-1, PD-L1 or CTLA-4 checkpoint inhibitor",
    "pituitary_radiation": "radiation to the pituitary",
}

_FIELD_DOCS = [
    ("age", "patient age in years; value a number; ops >= <= > < =="),
    ("sex", 'value "female" or "male"; op =='),
    ("diagnosis", f"tumor types named; op in; value a list from {sorted(ENTITIES)} "
                  '("glioma" = any glioma). Not for subtypes such as diffuse midline, pilocytic or '
                  "pediatric-type glioma, and not for other cancers"),
    ("grade", "WHO/CNS tumor grade 1-4; ops >= <= > < ==. Never a CTCAE toxicity grade"),
    ("idh", 'value "mutant" or "wildtype"; ops == !='),
    ("codeletion_1p19q", 'value "codeleted" or "intact"; ops == !='),
    ("mgmt", 'MGMT promoter; value "methylated" or "unmethylated"; ops == !='),
    ("recurrent", "true = recurrent, progressive or relapsed tumor; false = newly diagnosed; op =="),
    ("recurrence_number", "number of recurrences so far (first recurrence = 1); ops >= <= > < =="),
    ("tmb_clia", "tumor mutational burden in mutations/Mb from a CLIA lab; ops >= <= > <"),
    ("can_use_phone_or_tablet", "true/false; op =="),
    *[(f"prior_treatments.{t}", f"true = has received {_TREATMENT_MEANING[t]} before; op ==") for t in TREATMENTS],
    ("prior_treatments.<type>.weeks_ago", "weeks since that treatment ended; ops >= <= > <. "
                                          '"at least 12 weeks since radiation" -> >= 12; "within 4 weeks" -> < 4'),
    ("prior_treatments.temozolomide.cycles", "number of temozolomide cycles; ops >= <= > <"),
    ("prior_treatments.surgery.extent", 'op in; value a list from ["gross_total", "subtotal", "biopsy_only"]'),
]

SYSTEM_PROMPT = """You convert ONE clinical trial eligibility criterion into JSON. You never see a patient \
and never judge eligibility; you only describe what the criterion text says.

Output one JSON object:
{"kind": ..., "applies": ..., "condition": [...], "combine": ..., "requirements": [...]}
Each item of "condition" and "requirements" is {"quote": ..., "field": ..., "op": ..., "value": ...}.

kind:
- "requirement": the text states something about the patient that the fields below can express.
- "heading": a lead-in or label that introduces other items (often ends with ":"), with no requirement itself.
- "arm_option": the text describes one arm, cohort, part or stratum among alternatives.
- "unreadable": nothing in the text fits the fields (labs, performance status, consent, contraception, \
organ function, other cancers, other drugs, and so on).
For any kind other than "requirement", use applies "always", combine "all" and empty lists.

Describe the condition exactly as written. Under an exclusion, describe the excluded condition; do NOT \
invert it. "No prior bevacizumab" -> prior_treatments.bevacizumab == false. "Prior bevacizumab" under \
Exclusion -> prior_treatments.bevacizumab == true.

applies: "always", or "conditional" when the requirement is only for some patients ("if", "for patients \
who", "patients who received X must ..."). Then "condition" says who it applies to and "requirements" what \
it asks of them. "condition" is empty when applies is "always".

combine: "all" when every requirement must hold; "any" when the text joins them with "or".

quote: the exact words from the criterion that state this item, copied character for character. Quote every \
word that belongs to the item, including qualifiers ("at the time of enrollment"). Leave unquoted any part \
the fields cannot express; never stretch a field to cover something it does not mean, and never invent a \
field or value. If a qualifier changes the meaning of an item (a specific subtype, a specific setting, a \
specific drug class), leave that item out rather than simplify it.

Fields:
""" + "\n".join(f"- {name}: {doc}" for name, doc in _FIELD_DOCS) + """
<type> is one of: """ + ", ".join(TREATMENTS) + "."


def _user(c: Criterion) -> str:
    return f"{c.type.capitalize()} criterion:\n{c.text}"


def _ex(kind: str, text: str, out: dict[str, Any]) -> list[dict[str, str]]:
    full = {"kind": "requirement", "applies": "always", "condition": [], "combine": "all", "requirements": []}
    return [{"role": "user", "content": _user(Criterion(kind, text))},  # type: ignore[arg-type]
            {"role": "assistant", "content": json.dumps(full | out, ensure_ascii=False)}]


def _r(quote: str, field: str, op: str, value: Any) -> dict[str, Any]:
    return {"quote": quote, "field": field, "op": op, "value": value}


# Made-up examples, not taken from the labeled live fixtures (that would leak
# the evaluation set into the prompt).
FEW_SHOT: list[dict[str, str]] = [
    *_ex("inclusion", "Age ≥ 18 years at the time of consent.",
         {"requirements": [_r("Age ≥ 18 years at the time of consent", "age", ">=", 18)]}),
    *_ex("inclusion", "Histologically confirmed WHO grade 2 or 3 IDH-mutant astrocytoma or oligodendroglioma.",
         {"requirements": [
             _r("astrocytoma or oligodendroglioma", "diagnosis", "in", ["astrocytoma", "oligodendroglioma"]),
             _r("WHO grade 2 or 3", "grade", ">=", 2),
             _r("WHO grade 2 or 3", "grade", "<=", 3),
             _r("IDH-mutant", "idh", "==", "mutant")]}),
    *_ex("inclusion", "Patients who received prior radiation must have completed it at least 12 weeks before "
                      "enrollment.",
         {"applies": "conditional",
          "condition": [_r("Patients who received prior radiation", "prior_treatments.radiation", "==", True)],
          "requirements": [_r("completed it at least 12 weeks before enrollment",
                              "prior_treatments.radiation.weeks_ago", ">=", 12)]}),
    *_ex("exclusion", "Prior treatment with bevacizumab or any other VEGF-targeted agent.",
         {"combine": "any",
          "requirements": [_r("Prior treatment with bevacizumab", "prior_treatments.bevacizumab", "==", True)]}),
    *_ex("inclusion", "Newly diagnosed tumor; no prior radiation therapy.",
         {"requirements": [_r("Newly diagnosed tumor", "recurrent", "==", False),
                           _r("no prior radiation therapy", "prior_treatments.radiation", "==", False)]}),
    *_ex("inclusion", "First or second recurrence of glioblastoma.",
         {"requirements": [_r("First or second recurrence", "recurrence_number", "<=", 2),
                           _r("First or second recurrence", "recurrent", "==", True),
                           _r("glioblastoma", "diagnosis", "in", ["glioblastoma"])]}),
    *_ex("inclusion", "Cohort B: participants with recurrent disease after standard therapy", {"kind": "arm_option"}),
    *_ex("inclusion", "Adequate organ function as defined by:", {"kind": "heading"}),
    *_ex("exclusion", "Known hypersensitivity to any component of the study drug.", {"kind": "unreadable"}),
]


def build_messages(criterion: Criterion) -> list[dict[str, str]]:
    """Chat messages for one bullet. Takes only the criterion: no patient data."""
    return [{"role": "system", "content": SYSTEM_PROMPT}, *FEW_SHOT, {"role": "user", "content": _user(criterion)}]


def response_schema() -> dict[str, Any]:
    """JSON schema for constrained decoding; fields and ops are closed enums."""
    ops = sorted({op for spec in FIELDS.values() for op in spec.ops})
    value = {"anyOf": [{"type": "number"}, {"type": "boolean"}, {"type": "string"},
                       {"type": "array", "items": {"type": "string"}}]}
    req = {"type": "object", "additionalProperties": False,
           "properties": {"quote": {"type": "string"}, "field": {"enum": sorted(FIELDS)},
                          "op": {"enum": ops}, "value": value},
           "required": ["quote", "field", "op", "value"]}
    return {"type": "object", "additionalProperties": False,
            "properties": {"kind": {"enum": ["requirement", "heading", "arm_option", "unreadable"]},
                           "applies": {"enum": ["always", "conditional"]},
                           "condition": {"type": "array", "items": req},
                           "combine": {"enum": ["all", "any"]},
                           "requirements": {"type": "array", "items": req}},
            "required": ["kind", "applies", "condition", "combine", "requirements"]}


# ---------------------------------------------------------------------------
# Backends
# ---------------------------------------------------------------------------

GEN = {"temperature": 0.0, "seed": 0, "max_tokens": 512}


class Backend(Protocol):
    model_id: str

    def complete(self, messages: list[dict[str, str]], schema: dict[str, Any]) -> str: ...


class ServerBackend:
    """An OpenAI-compatible chat server, such as llama.cpp `llama-server`.

    Run the server with several parallel slots (`-np 8`) for batch throughput.
    """

    def __init__(self, base_url: str, model_id: str, *, timeout: float = 120.0,
                 client: httpx.Client | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self.model_id = model_id
        self._client = client or httpx.Client(timeout=timeout)

    def complete(self, messages: list[dict[str, str]], schema: dict[str, Any]) -> str:
        r = self._client.post(f"{self.base_url}/v1/chat/completions", json={
            "model": self.model_id, "messages": messages, **GEN,
            "response_format": {"type": "json_schema",
                                "json_schema": {"name": "criterion_parse", "strict": True, "schema": schema}}})
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"]


class LlamaCppBackend:
    """A GGUF model run in-process with llama-cpp-python (CPU on the Space).

    Calls are serialized: one llama.cpp context is not thread-safe.
    """

    def __init__(self, model_path: str | Path, model_id: str | None = None, *, n_ctx: int = 4096,
                 n_threads: int | None = None) -> None:
        from llama_cpp import Llama  # optional dependency, only when this backend is used

        self.model_id = model_id or Path(model_path).stem
        self._llm = Llama(model_path=str(model_path), n_ctx=n_ctx, n_threads=n_threads or os.cpu_count(),
                          seed=GEN["seed"], verbose=False)
        self._lock = threading.Lock()

    def complete(self, messages: list[dict[str, str]], schema: dict[str, Any]) -> str:
        with self._lock:
            out = self._llm.create_chat_completion(
                messages=messages, response_format={"type": "json_object", "schema": schema},
                temperature=GEN["temperature"], seed=GEN["seed"], max_tokens=GEN["max_tokens"])
        return out["choices"][0]["message"]["content"]


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------


def slug(model_id: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in model_id.lower())


def cache_key(criterion: Criterion, model_id: str, prompt_version: str = PROMPT_VERSION) -> str:
    return hashlib.sha256(f"{criterion_key(criterion)}\n{model_id}\n{prompt_version}".encode()).hexdigest()


def default_cache_path(model_id: str, prompt_version: str = PROMPT_VERSION) -> Path:
    return CACHE_DIR / f"{slug(model_id)}--{prompt_version}.json"


class ParseCache:
    """Parses on disk: {cache_key: entry}. Each entry keeps the bullet text,
    so a cache can be re-validated or reviewed without the model."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._data: dict[str, dict[str, Any]] = {}
        if path.exists():
            self._data = json.loads(path.read_text(encoding="utf-8"))

    def __len__(self) -> int:
        return len(self._data)

    def __contains__(self, key: str) -> bool:
        return key in self._data

    def get(self, key: str) -> dict[str, Any] | None:
        return self._data.get(key)

    def put(self, key: str, entry: dict[str, Any]) -> None:
        with self._lock:
            self._data[key] = entry

    def save(self) -> None:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._data, indent=1, ensure_ascii=False, sort_keys=True), encoding="utf-8")
            tmp.replace(self.path)


# ---------------------------------------------------------------------------
# Parse source
# ---------------------------------------------------------------------------


def parse_with(backend: Backend, criterion: Criterion) -> Any:
    """One model call. Raises on a transport error or non-JSON output."""
    return json.loads(backend.complete(build_messages(criterion), response_schema()))


class LlmParseSource:
    """A `ParseSource` backed by a cache and, in `live` mode, a model.

    Returns the raw parse or None. None leaves the regex result standing, so
    an unreachable model, a timeout or bad output is today's checker.
    """

    def __init__(self, cache: ParseCache, model_id: str, *, backend: Backend | None = None,
                 mode: ParseMode = "cached_only", prompt_version: str = PROMPT_VERSION,
                 clock: Callable[[], float] = time.perf_counter) -> None:
        if backend is not None and backend.model_id != model_id:
            raise ValueError(f"backend model {backend.model_id!r} does not match cache model {model_id!r}")
        self.cache, self.model_id, self.backend, self.mode = cache, model_id, backend, mode
        self.prompt_version = prompt_version
        self._clock = clock
        self._stats = {"cache_hits": 0, "cache_misses": 0, "model_calls": 0, "model_errors": 0}
        self._stats_lock = threading.Lock()

    def _count(self, name: str) -> None:
        with self._stats_lock:
            self._stats[name] += 1

    def take_stats(self) -> dict[str, Any]:
        """Counts since the last call (one trial, in check_trial), then reset."""
        with self._stats_lock:
            out = {"model": self.model_id, "prompt_version": self.prompt_version, **self._stats}
            self._stats = dict.fromkeys(self._stats, 0)
        return out

    def __call__(self, criterion: Criterion) -> Any:
        key = cache_key(criterion, self.model_id, self.prompt_version)
        hit = self.cache.get(key)
        if hit is not None:
            self._count("cache_hits")
            return hit["raw"]
        self._count("cache_misses")
        if self.mode != "live" or self.backend is None:
            return None
        return self.call_model(criterion, key)

    def call_model(self, criterion: Criterion, key: str | None = None) -> Any:
        """Parse with the model, cache the result and audit the call."""
        assert self.backend is not None
        key = key or cache_key(criterion, self.model_id, self.prompt_version)
        self._count("model_calls")
        start = self._clock()
        raw: Any = None
        error: str | None = None
        try:
            raw = parse_with(self.backend, criterion)
        except Exception as e:  # noqa: BLE001 - any failure falls back to regex
            error = f"{type(e).__name__}: {str(e)[:200]}"
            self._count("model_errors")
        ms = round((self._clock() - start) * 1000)
        if error is None:
            self.cache.put(key, {"criterion_type": criterion.type, "text": criterion.text, "raw": raw,
                                 "model": self.model_id, "prompt_version": self.prompt_version})
        audit("agent", "criterion_parsed", {
            "cache_key": key, "model": self.model_id, "prompt_version": self.prompt_version,
            "cache_hit": False, "ms": ms, "ok": error is None, **({"error": error} if error else {})})
        return raw
