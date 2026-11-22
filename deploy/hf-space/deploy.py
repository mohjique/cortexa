"""Stage the Cortexa backend and push it to a Hugging Face Docker Space.

Copies only what the backend needs at runtime, including the committed
classifier weights (artifacts/classifier/model.pt), into one folder and uploads it.

    # once: pip install huggingface_hub && hf auth login   (token with write access)
    python deploy/hf-space/deploy.py --space <hf-user>/cortexa-api \
        --cors-origins https://<your-app>.vercel.app

    python deploy/hf-space/deploy.py --stage-only build/hf-space   # stage, no upload (docker build test)
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
MODEL = REPO / "artifacts" / "classifier" / "model.pt"

# What the backend reads at runtime, and nothing else.
TREES = {
    "agent": ["tests", "data", "__pycache__"],  # demo_cache/ is needed: trials, geocoding, drive times
    "classifier": ["tests", "splits", "__pycache__", "*.ipynb"],
    "mutation_check": ["tests", "figures", "__pycache__"],
}
FILES = ["policies/policy.yaml"]
GLOBS = ["clinical/patients/demo-*.json"]  # the mutation check reads the synthetic 96-channel counts
SPACE_FILES = {"Dockerfile": "Dockerfile", "requirements.txt": "requirements.txt", "SPACE_README.md": "README.md"}
# Remote paths the upload replaces, so files deleted locally disappear from the Space too.
MANAGED = ["agent/**", "classifier/**", "mutation_check/**", "clinical/**", "policies/**", "artifacts/**"]


def stage(out: Path) -> Path:
    if not MODEL.exists():
        sys.exit(f"no model at {MODEL}: train it or copy it there first")
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    for tree, skip in TREES.items():
        shutil.copytree(REPO / tree, out / tree, ignore=shutil.ignore_patterns(*skip, "*.pyc"))
    for rel in FILES:
        (out / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(REPO / rel, out / rel)
    for pattern in GLOBS:
        for p in sorted(REPO.glob(pattern)):
            rel = p.relative_to(REPO)
            (out / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(p, out / rel)
    (out / "artifacts" / "classifier").mkdir(parents=True)
    shutil.copy2(MODEL, out / "artifacts" / "classifier" / "model.pt")
    for src, dst in SPACE_FILES.items():
        shutil.copy2(HERE / src, out / dst)
    n = sum(1 for p in out.rglob("*") if p.is_file())
    print(f"staged {n} files in {out} (model {MODEL.stat().st_size / 1e6:.1f} MB)")
    return out


def push(folder: Path, space: str, private: bool, cors_origins: str | None, cors_regex: str | None) -> None:
    from huggingface_hub import HfApi

    api = HfApi()
    api.create_repo(space, repo_type="space", space_sdk="docker", private=private, exist_ok=True)
    if cors_origins:
        api.add_space_variable(space, "CORTEXA_CORS_ORIGINS", cors_origins)
    if cors_regex:
        api.add_space_variable(space, "CORTEXA_CORS_ORIGIN_REGEX", cors_regex)
    api.upload_folder(
        folder_path=str(folder),
        repo_id=space,
        repo_type="space",
        delete_patterns=MANAGED,
        commit_message="Deploy Cortexa backend",
    )
    owner, name = space.split("/")
    print(f"pushed. The Space builds now: https://huggingface.co/spaces/{space}")
    print(f"API URL for Vercel (VITE_API_URL): https://{owner}-{name}.hf.space".replace("_", "-").lower())


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--space", help="<hf-user-or-org>/<space-name>")
    ap.add_argument("--stage-only", type=Path, help="stage into this folder and stop")
    ap.add_argument("--cors-origins", help="comma-separated, e.g. https://cortexa.vercel.app,http://localhost:5173")
    ap.add_argument("--cors-regex", help=r"optional, e.g. ^https://cortexa(-[a-z0-9-]+)?\.vercel\.app$")
    ap.add_argument("--private", action="store_true",
                    help="private Space (the browser UI cannot reach it without an HF token, so the demo breaks)")
    a = ap.parse_args()
    if a.stage_only:
        stage(a.stage_only.resolve())
        return
    if not a.space:
        ap.error("--space is required unless --stage-only is given")
    if not (a.cors_origins or a.cors_regex):
        print("warning: no --cors-origins: the Vercel UI will be blocked until CORTEXA_CORS_ORIGINS is set on the Space")
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        push(stage(Path(tmp) / "space"), a.space, a.private, a.cors_origins, a.cors_regex)


if __name__ == "__main__":
    main()
