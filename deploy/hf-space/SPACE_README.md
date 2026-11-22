---
title: Cortexa API
emoji: 🧠
colorFrom: indigo
colorTo: blue
sdk: docker
app_port: 7860
pinned: false
short_description: Cortexa demo backend (synthetic data only)
---

# Cortexa API

FastAPI backend for Cortexa, built at tidalBYTE '26. The web UI is hosted separately and calls this API.

- **Synthetic data only.** Do not upload real patient images or information.
- **Not a medical device.** The classifier's output is a *suspected* category for a doctor to confirm. It is not a diagnosis.
- Cases and the audit log are kept in temporary storage and are cleared whenever the Space restarts.

Health check: `GET /health` reports which classifier is serving.
