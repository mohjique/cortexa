# Cortexa backend (FastAPI + ResNet-18 classifier) for a Hugging Face Docker Space.
# Built from the folder that deploy/hf-space/deploy.py stages; see deploy/hf-space/README.md.
FROM python:3.12-slim

# Spaces run the container as uid 1000.
RUN useradd -m -u 1000 user
USER user
ENV HOME=/home/user \
    PATH=/home/user/.local/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1
WORKDIR /home/user/app

# CPU-only torch from the PyTorch index (the PyPI default pulls CUDA, several GB).
RUN pip install --user torch==2.15.0 torchvision==0.29.2 --index-url https://download.pytorch.org/whl/cpu
COPY --chown=user requirements.txt .
RUN pip install --user -r requirements.txt

COPY --chown=user . .

# real: load model.pt at startup and refuse to boot without it (never a silent stub).
# Case files, audit log and heatmaps live in /tmp: they reset when the Space restarts.
ENV CORTEXA_CLASSIFIER=real \
    CORTEXA_DATA_DIR=/tmp/cortexa-data \
    CORTEXA_CACHE_DIR=/tmp/cortexa-cache \
    CORTEXA_TRIAL_SOURCE=demo

EXPOSE 7860
CMD ["uvicorn", "agent.api:app", "--host", "0.0.0.0", "--port", "7860"]
