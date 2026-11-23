"""Batch-parse criteria ahead of time (issue 56) and report timing.

Parses every unique bullet once and writes the cache that `LlmParseSource`
reads. Bullets already in the cache are skipped, so a rerun only parses new
or changed trials. Run from the repo root against a local llama.cpp server:

    llama-server -m model.gguf -ngl 99 -np 8 -c 32768 --port 8080
    python -m agent.parse_batch --server http://127.0.0.1:8080 --model-id qwen3-4b-q4 --source fixtures
    python -m agent.parse_batch ... --source search --limit 100   # time 100 bullets first

Sources:
- `fixtures`: the labeled live criteria in agent/tests/fixtures/live_criteria.
- `search`: the nationwide trial search for each bucket's condition, as
  matching runs it (`CORTEXA_TRIAL_SOURCE=live` for ClinicalTrials.gov).

Only public registry text is sent to the model. With `-np N` on the server,
use `--workers N` here.
"""

from __future__ import annotations

import argparse
import sys
import time
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path

from agent.criteria_llm import (
    PROMPT_VERSION,
    LlmParseSource,
    ParseCache,
    ServerBackend,
    cache_key,
    default_cache_path,
)
from agent.criteria_parse import Invalid, validate_parse
from agent.eligibility import Criterion, split_criteria


def from_fixtures() -> list[Criterion]:
    from agent.live_eval import load_fixtures

    return [c for f in load_fixtures().values() for c in split_criteria(f["criteria_text"])]


def from_search() -> list[Criterion]:
    from agent.matching import CONDITION_FOR_BUCKET
    from agent.trials import search_trials

    out: list[Criterion] = []
    seen: set[str] = set()
    for condition in dict.fromkeys(CONDITION_FOR_BUCKET.values()):
        for t in search_trials(condition=condition):
            if t.trial_id not in seen:
                seen.add(t.trial_id)
                out += split_criteria(t.criteria_text)
    return out


SOURCES = {"fixtures": from_fixtures, "search": from_search}


def unique(criteria: Iterable[Criterion], model_id: str) -> dict[str, Criterion]:
    """One bullet per cache key (repeated boilerplate parses once)."""
    out: dict[str, Criterion] = {}
    for c in criteria:
        out.setdefault(cache_key(c, model_id), c)
    return out


@dataclass
class BatchReport:
    bullets: int = 0
    unique: int = 0
    already_cached: int = 0
    parsed: int = 0
    errors: int = 0
    invalid: int = 0  # parsed but rejected by criteria_parse.validate_parse
    seconds: float = 0.0

    def line(self) -> str:
        rate = self.parsed / self.seconds if self.seconds else 0.0
        per = self.seconds / self.parsed if self.parsed else 0.0
        return (f"bullets {self.bullets}, unique {self.unique}, already cached {self.already_cached}, "
                f"parsed {self.parsed}, errors {self.errors}, invalid {self.invalid}; "
                f"{self.seconds:.0f} s wall, {rate:.2f} bullets/s ({per:.2f} s each)")


def run(source: LlmParseSource, criteria: list[Criterion], *, workers: int = 4, limit: int | None = None,
        save_every: int = 50, log=print) -> BatchReport:
    rep = BatchReport(bullets=len(criteria))
    todo = unique(criteria, source.model_id)
    rep.unique = len(todo)
    pending = [(k, c) for k, c in todo.items() if k not in source.cache]
    rep.already_cached = rep.unique - len(pending)
    if limit is not None:
        pending = pending[:limit]
    start = time.perf_counter()
    done = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(source.call_model, c, k): (k, c) for k, c in pending}
        for fut in as_completed(futures):
            k, c = futures[fut]
            if k not in source.cache:
                rep.errors += 1
            else:
                rep.parsed += 1
                if isinstance(validate_parse(fut.result(), c), Invalid):
                    rep.invalid += 1
            done += 1
            if done % save_every == 0:
                source.cache.save()
                el = time.perf_counter() - start
                log(f"  {done}/{len(pending)}  {el:.0f} s, ETA {el / done * (len(pending) - done):.0f} s")
    rep.seconds = time.perf_counter() - start
    source.cache.save()
    return rep


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Batch-parse criteria bullets into the parse cache")
    ap.add_argument("--server", required=True, help="OpenAI-compatible server URL (llama.cpp llama-server)")
    ap.add_argument("--model-id", required=True, help="model name; part of the cache key and file name")
    ap.add_argument("--source", choices=sorted(SOURCES), default="fixtures")
    ap.add_argument("--workers", type=int, default=4, help="parallel requests (match the server's -np)")
    ap.add_argument("--limit", type=int, help="parse at most this many uncached bullets (timing run)")
    ap.add_argument("--cache", type=Path, help=f"cache file (default agent/parse_cache/<model>--{PROMPT_VERSION}.json)")
    args = ap.parse_args(argv)

    path = args.cache or default_cache_path(args.model_id)
    cache = ParseCache(path)
    source = LlmParseSource(cache, args.model_id, backend=ServerBackend(args.server, args.model_id), mode="live")
    criteria = SOURCES[args.source]()
    print(f"{len(criteria)} bullets from {args.source}; cache {path} has {len(cache)} parses")
    rep = run(source, criteria, workers=args.workers, limit=args.limit)
    print(rep.line())
    return 1 if rep.errors and not rep.parsed else 0


if __name__ == "__main__":
    sys.exit(main())
