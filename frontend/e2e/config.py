"""Settings shared by the browser checks. Override with environment variables."""
import os
from pathlib import Path

E2E_DIR = Path(__file__).resolve().parent
REPO = E2E_DIR.parent.parent

# Backend (uvicorn agent.api:app) and the Vite dev server that proxies /api to it.
API = os.environ.get("CORTEXA_E2E_API", "http://127.0.0.1:8000").rstrip("/")
APP = os.environ.get("CORTEXA_E2E_APP", "http://localhost:5173").rstrip("/")
# Screenshots land here (gitignored).
OUT = Path(os.environ.get("CORTEXA_E2E_OUT", E2E_DIR / "out"))
# "chrome" uses the installed Google Chrome; set to "" to use Playwright's bundled
# Chromium (after `python -m playwright install chromium`).
CHANNEL = os.environ.get("CORTEXA_E2E_CHANNEL", "chrome") or None

DOCTOR_ID = "dr-synthetic-01"


def launch(p):
    return p.chromium.launch(channel=CHANNEL) if CHANNEL else p.chromium.launch()


def init_script(ws: str) -> str:
    """Pin the browser to a workspace and skip the first-visit tour."""
    return f"localStorage.setItem('cortexa.workspace_id','{ws}'); localStorage.setItem('cortexa.tutorial.v1','1')"


class Checks:
    """Collects PASS/FAIL lines and counts failures."""

    def __init__(self) -> None:
        self.failures = 0

    def ok(self, name: str, cond: bool, extra: str = "") -> None:
        self.failures += 0 if cond else 1
        print(("  PASS " if cond else "  FAIL ") + name, extra)
