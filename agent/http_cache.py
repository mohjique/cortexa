"""HTTP GET with a disk cache and a committed demo fallback (F7.4, F7.7).

Source mode comes from `CORTEXA_TRIAL_SOURCE`:

- `demo` (default): never touch the network. Read `agent/demo_cache/`.
- `live`: call the API. On success, save the response to `cache/`. On failure,
  use the last saved response, then the demo cache.

The saved file never stores request headers, so API keys stay out of `cache/`.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

import httpx

from agent.audit import audit

REPO_ROOT = Path(__file__).resolve().parents[1]
DEMO_CACHE_DIR = Path(__file__).resolve().parent / "demo_cache"
LIVE_CACHE_DIR = Path(os.environ.get("CORTEXA_CACHE_DIR", REPO_ROOT / "cache"))
USER_AGENT = "cortexa-hackathon-demo/0.1"  # generic client name, no contact details
TIMEOUT_S = 15.0

Mode = Literal["demo", "live"]


class FetchError(Exception):
    """No live response, no saved response, and no demo fallback."""


def source_mode() -> Mode:
    mode = os.environ.get("CORTEXA_TRIAL_SOURCE", "demo").strip().lower()
    if mode not in ("demo", "live"):
        raise ValueError(f"CORTEXA_TRIAL_SOURCE must be 'demo' or 'live', not {mode!r}")
    return mode  # type: ignore[return-value]


@dataclass
class Fetched:
    data: Any
    from_cache: bool
    fetched_at: datetime
    origin: Literal["live", "disk_cache", "demo_cache"]


def demo_manifest_time() -> datetime:
    manifest = json.loads((DEMO_CACHE_DIR / "manifest.json").read_text(encoding="utf-8"))
    return datetime.fromisoformat(manifest["fetched_at"].replace("Z", "+00:00"))


def load_demo(rel_path: str) -> Any:
    return json.loads((DEMO_CACHE_DIR / rel_path).read_text(encoding="utf-8"))


def _cache_key(url: str, params: dict[str, Any]) -> str:
    blob = json.dumps({"url": url, "params": params}, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode()).hexdigest()[:24]


def _cache_path(source: str, url: str, params: dict[str, Any]) -> Path:
    return LIVE_CACHE_DIR / source / f"{_cache_key(url, params)}.json"


def _default_factory() -> httpx.Client:
    return httpx.Client(timeout=TIMEOUT_S, headers={"User-Agent": USER_AGENT})


_client_factory = _default_factory


def set_client_factory(factory: Any) -> None:
    """Swap the HTTP client (tests use httpx.MockTransport)."""
    global _client_factory
    _client_factory = factory or _default_factory


def get_json(
    source: str,
    url: str,
    params: dict[str, Any],
    *,
    headers: dict[str, str] | None = None,
    demo_file: str | None = None,
    case_id: str | None = None,
) -> Fetched:
    """GET url and parse JSON, following the source mode above."""
    if source_mode() == "demo":
        if demo_file is None:
            raise FetchError(f"{source}: demo mode and no demo cache file for this request")
        return Fetched(load_demo(demo_file), True, demo_manifest_time(), "demo_cache")

    path = _cache_path(source, url, params)
    try:
        with _client_factory() as client:
            resp = client.get(url, params=params, headers=headers or {})
            resp.raise_for_status()
            data = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        error = f"{type(exc).__name__}: {exc}"[:300]
        if path.exists():
            saved = json.loads(path.read_text(encoding="utf-8"))
            audit("agent", "api_fallback", {"source": source, "to": "disk_cache", "error": error}, case_id)
            return Fetched(saved["response"], True, datetime.fromisoformat(saved["fetched_at"]), "disk_cache")
        if demo_file is not None:
            audit("agent", "api_fallback", {"source": source, "to": "demo_cache", "error": error}, case_id)
            return Fetched(load_demo(demo_file), True, demo_manifest_time(), "demo_cache")
        raise FetchError(f"{source}: {error}") from exc

    now = datetime.now(timezone.utc)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"fetched_at": now.isoformat(), "url": url, "params": params, "response": data}),
        encoding="utf-8",
    )
    return Fetched(data, False, now, "live")
