# Deploy: backend on a Hugging Face Space, UI on Vercel

```
Browser ──> Vercel (frontend/, static React) ──VITE_API_URL──> HF Docker Space (agent/api.py + ResNet-18)
```

Vercel only serves the UI. The backend needs torch, the 45 MB checkpoint and a writable disk, so it runs in a Docker Space.

## 1. Backend (Hugging Face Space)

One time: create a Hugging Face account and a token with **write** access, then

```bash
pip install huggingface_hub
hf auth login
```

Deploy (creates the Space on the first run, updates it after that):

```bash
python deploy/hf-space/deploy.py --space <hf-user>/cortexa-api \
    --cors-origins https://<your-app>.vercel.app,http://localhost:5173
```

The script stages only what the backend reads at runtime (`agent/`, `classifier/`, `mutation_check/`, `clinical/patients/`, `policies/policy.yaml`, `artifacts/classifier/model.pt`), uploads it, and sets the CORS variable on the Space. The Space then builds for a few minutes. Watch the **Logs** tab on the Space page.

Check that it is up and using the real model:

```bash
curl https://<hf-user>-cortexa-api.hf.space/health
# {"status":"ok","classifier":"ResNetClassifier"}
```

The Space must be **public**: a browser cannot call a private Space without a Hugging Face token. It holds synthetic data only and has no login. Each browser gets its own workspace (a random ID it keeps in localStorage), so visitors don't see each other's cases. That is isolation, not authentication: anyone with a workspace ID can open it.

## 2. Frontend (Vercel)

New Project → import the repo → **Root Directory: `frontend`**. Add the environment variable

```
VITE_API_URL=https://<hf-user>-cortexa-api.hf.space
```

(no trailing slash) for Production and Preview, then deploy. If the Vercel domain changes, rerun the deploy script with the new `--cors-origins`, or edit `CORTEXA_CORS_ORIGINS` under the Space's Settings → Variables. For preview deploys, `--cors-regex '^https://<project>(-[a-z0-9-]+)?\.vercel\.app$'` allows every preview URL.

## Settings baked into the image (`Dockerfile`)

| Variable | Value | Why |
|---|---|---|
| `CORTEXA_CLASSIFIER` | `real` | Loads `model.pt` at startup. A missing or broken model stops the Space instead of silently serving the stub. |
| `CORTEXA_DATA_DIR` | `/tmp/cortexa-data` | One folder per browser workspace (`workspaces/<id>/`: cases, audit log, scans, heatmaps). **Cleared on every restart.** |
| `CORTEXA_WORKSPACE_TTL_HOURS` | `24` (code default) | Workspaces unused this long are deleted when a new one starts. |
| `CORTEXA_MAX_WORKSPACES` | `200` (code default) | Cap on workspaces, least recently used deleted first, so visitors can't fill the disk. |
| `CORTEXA_TRIAL_SOURCE` | `demo` | Trials, geocoding and drive times come from `agent/demo_cache/`. No outside calls, no API keys. |

## Before judging

- Free Spaces sleep after a period without traffic. Open `/health` a few minutes before the demo so the model is loaded.
- A restart clears all cases. Upload the demo cases again after any restart or redeploy.
- Cases are per browser. Open the demo cases in the browser you will present from. **Reset demo** in the header clears only that browser's cases.
- Redeploying after a `policy.yaml` commit rebuilds the Space. Allow a few minutes.

## Test the image locally

```bash
python deploy/hf-space/deploy.py --stage-only build/hf-space
docker build -t cortexa-api build/hf-space
docker run --rm -p 8000:7860 -e CORTEXA_CORS_ORIGINS=http://localhost:5173 cortexa-api
# then: cd frontend && npm run dev   (Vite proxies /api to :8000)
```
