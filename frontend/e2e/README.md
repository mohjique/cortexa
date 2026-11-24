# Browser checks: trial shortlist map

Playwright checks for the shortlist map. They cover:

- the default view: centered on the patient's ZIP, with about 10 pins in view
- numbered pins that match the trial cards
- shared-site pins and their trial lists
- "Show on map" on each card
- keyboard use

They drive the real app (FastAPI backend plus the Vite dev server) with the synthetic demo cases in `clinical/patients/`. No real patient data is involved.

## Setup

```bash
pip install -r frontend/e2e/requirements.txt
```

The checks use your installed Google Chrome. If Chrome isn't installed, use Playwright's bundled Chromium instead:

```bash
python -m playwright install chromium
export CORTEXA_E2E_CHANNEL=
```

## Run

Start the backend and the dev server as in `frontend/README.md`. Then run the suite for the mode the backend is in.

```bash
# backend on the synthetic demo cache (default)
.venv/bin/uvicorn agent.api:app
python frontend/e2e/run_suite.py --mode demo

# backend on live ClinicalTrials.gov (calls the public APIs; ZIP only is geocoded)
CORTEXA_TRIAL_SOURCE=live .venv/bin/uvicorn agent.api:app
python frontend/e2e/run_suite.py --mode live
```

Both modes are needed for full coverage:

| Mode | Covers |
|---|---|
| `demo` | shortlists of 1 to 3 trials; a site 0 mi from the patient (closest-zoom limit); switching cases without a page reload; phone width; read-only cards after the review is recorded |
| `live` | 33 to 95 pins; trials without coordinates; phone width; boundary variants (exactly 10 and 11 pins, 10 pins with one shared, no coordinates, every trial at one site, mixed); the numbered-pin, shared-list, "Show on map" and keyboard checks |

Each run creates a fresh workspace (`e2e-<mode>-<timestamp>`), so it never touches your own cases. The live run takes a few minutes, because each case calls the trial APIs.

The boundary variants take a real `/record` response and edit its shortlist in the browser, for example by keeping exactly 10 sites. The backend never returns those exact counts on its own. The app code is not stubbed.

Screenshots go to `frontend/e2e/out/` (gitignored). The script exits with code 1 if any check fails.

## Settings

| Variable | Default |
|---|---|
| `CORTEXA_E2E_API` | `http://127.0.0.1:8000` |
| `CORTEXA_E2E_APP` | `http://localhost:5173` |
| `CORTEXA_E2E_OUT` | `frontend/e2e/out` |
| `CORTEXA_E2E_CHANNEL` | `chrome` (empty uses bundled Chromium) |

## Single scripts

```bash
python frontend/e2e/drive_case.py WORKSPACE demo-016 [--reset]
python frontend/e2e/map_checks.py WORKSPACE demo-016 demo-008 [--phone] [--variant ten] [--switch demo-016,demo-008] [--review demo-007]
python frontend/e2e/map_extras.py WORKSPACE demo-016    # needs a case with shared and single pins (live mode)
```
