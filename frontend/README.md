# frontend/

React + Vite + TypeScript app for the doctor-facing side of Cortexa. It reads case state from the agent backend (`agent/api.py`) and sends doctor decisions to it. The backend enforces every gate, policy rule and audit entry; the UI never moves a case on by itself.

`mockup/index.html` is the original static mockup, kept for reference.

## Screens

| Screen | Where | Backend |
|---|---|---|
| Care board (kanban by stage) | `/` | `GET /cases` |
| Case pathway: decision tree with the case's route | `/cases/:id` → Pathway | `GET /cases/{id}/record` |
| Confirm / correct bucket, classifier probabilities | detail panel | `POST /cases/{id}/decisions` (`confirm_bucket`) |
| Watch or treat, hormone labs, surgery or meds | detail panel | `watch_or_treat`, `hormone_labs`, `surgery_or_meds` |
| Pre-op screen: tests needed, enroll-before-surgery trials | Pre-op screen tab | `record.preop` |
| Enter pathology results | Enter results tab | `enter_results` |
| Trial shortlist: per-criterion evidence, map, travel time, keep/reject | Trial shortlist tab | `review_shortlist` |
| No-match plan: near misses, alert, tumor board log | No-match plan tab | `record.no_match` |
| Audit log | Audit log tab | `GET /cases/{id}/audit` |
| MRI upload | `/new` | `POST /cases` (**not built yet**, see below) |
| Retry a failed agent step | detail panel | `POST /cases/{id}/run` |

Safety rules the UI follows: model output is always labelled "suspected"; the confirm screen never pre-selects a bucket; results fields start blank (the doctor picks `pending` or `unknown` explicitly); only the ZIP is entered, never an address; the mutation check is shown as context only; there is no patient-contact feature. The doctor ID typed in the header is recorded on every decision.

## Run locally

```bash
# terminal 1, repo root: backend on :8000
.venv/bin/uvicorn agent.api:app --reload
```

```bash
# terminal 2
cd frontend && npm install && npm run dev
```

Open http://localhost:5173. In dev, Vite proxies `/api/*` to `http://127.0.0.1:8000`, so no CORS setup is needed.

## Deploy on Vercel

The frontend is a static build. The FastAPI backend needs its own host (Render, Railway, Fly, a VM); Vercel only serves the UI.

1. Deploy the backend and note its URL, e.g. `https://cortexa-api.example.com`.
2. On the backend, allow the Vercel origin:
   `CORTEXA_CORS_ORIGINS=https://<your-app>.vercel.app,http://localhost:5173`
3. In Vercel: New Project → import the repo → **Root Directory: `frontend`**. Framework, build command and output folder come from `vercel.json`. Keep "Include files outside the root directory" on (the default); the results form bundles the `doctor_decisions` blocks of `clinical/patients/demo-*.json` for its "Fill from synthetic demo record" button.
4. Add the environment variable `VITE_API_URL=https://cortexa-api.example.com` (no trailing slash) for Production and Preview, then deploy. If it is missing, the app shows a red banner.

`vercel.json` rewrites every path to `index.html` so deep links like `/cases/demo-013` work, and sets `noindex` and basic security headers.

## Waiting on other folders

- **MRI intake endpoint (agent/).** The upload page posts `multipart/form-data` with `case_id` and `file` to `POST /cases` and expects a `CaseState` back. The backend should create the case, run the classifier (stub now, real model later, same `Classifier` protocol) and call `workflow.attach_classification`, leaving the case at `confirm_bucket`. Until then the page says the endpoint isn't there, and cases created on the backend still show up.
- **Grad-CAM heatmap.** `heatmap_path` is a local file path today. The UI shows it only if it is an `http(s):` or `data:` URL, so the backend needs to serve it (e.g. `GET /cases/{id}/heatmap`) or return a URL.
- **No auth.** The doctor ID is typed in, not verified. Fine for a synthetic-data demo; put the deployment behind Vercel password protection if it should not be public.
