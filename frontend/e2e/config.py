"""Settings shared by the browser checks. Override with environment variables."""
import json
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
    return f"localStorage.setItem('cortexa.workspace_id',{json.dumps(ws)}); localStorage.setItem('cortexa.tutorial.v1','1')"


def pin_in_view(pg, selector: str = ".trial-pin"):
    """A pin matching selector that sits well inside the map, or None.

    If no pin is, clicks "Fit all trials" (shown only when there are more than
    10 pins) and looks again, then accepts any pin fully inside the map (a lone
    site sits near the edge of the default view). Never returns an off-screen pin.
    """

    def find(margin: bool) -> int:
        x, top, bottom = (50, 60, 30) if margin else (0, 0, 0)
        return pg.evaluate(f"""() => {{ const box = document.querySelector('.map').getBoundingClientRect();
          return [...document.querySelectorAll({json.dumps(selector)})].findIndex(e => {{ const r = e.querySelector('span').getBoundingClientRect();
            return r.width > 0 && r.left > box.left + {x} && r.right < box.right - {x} && r.top > box.top + {top} && r.bottom < box.bottom - {bottom}; }}); }}""")

    idx = find(True)
    fit = pg.get_by_role("button", name="Fit all trials")
    if idx < 0 and fit.count():
        fit.click()
        pg.wait_for_timeout(1200)
        idx = find(True)
    if idx < 0:
        idx = find(False)
    return pg.locator(selector).nth(idx) if idx >= 0 else None


class Checks:
    """Collects PASS/FAIL lines and counts failures."""

    def __init__(self) -> None:
        self.failures = 0

    def ok(self, name: str, cond: bool, extra: str = "") -> None:
        self.failures += 0 if cond else 1
        print(("  PASS " if cond else "  FAIL ") + name, extra)
