"""Where the MRI images live and how folder names map to buckets.

Dataset: Kaggle `mohamadabouali1/mri-brain-tumor-dataset-4-class-7023-images`.
Its Training/Testing images are the same 7,023 images as
`masoudnickparvar/brain-tumor-mri-dataset` (identical per-class counts), with
renamed folders. It also ships blurred / noisy / motion-artifact copies.

Download into `data/brain-tumor-mri/` (gitignored), from the repo root:

    kaggle datasets download -d mohamadabouali1/mri-brain-tumor-dataset-4-class-7023-images \
        -p data/brain-tumor-mri --unzip
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DATA_ROOT = Path(os.environ.get("CORTEXA_MRI_DIR", REPO_ROOT / "data" / "brain-tumor-mri"))
SPLIT_CSV = Path(__file__).resolve().parent / "splits" / "split.csv"

BUCKETS = ("glioma", "meningioma", "no_tumor", "pituitary")  # class index order
IMAGE_EXTS = {".jpg", ".jpeg", ".png"}

# Folder names in this dataset and in the Masoud original -> our bucket names.
_FOLDER_TO_BUCKET = {
    "glioma": "glioma",
    "meningioma": "meningioma",
    "notumor": "no_tumor",
    "notumour": "no_tumor",
    "pituitary": "pituitary",
}

# Top-level origin of an image, from its path.
CLEAN_ORIGINS = ("training", "testing")
VARIANT_ORIGINS = ("blurred", "noisy", "motion")


def _norm(text: str) -> str:
    return re.sub(r"[^a-z]", "", text.lower())


def bucket_for_folder(name: str) -> str | None:
    return _FOLDER_TO_BUCKET.get(_norm(name))


def origin_for_path(rel: Path) -> str | None:
    parts = [_norm(p) for p in rel.parts]
    for p in parts:
        if p == "training":
            return "training"
        if p == "testing":
            return "testing"
        if p.startswith("blurred"):
            return "blurred"
        if p.startswith("noisy"):
            return "noisy"
        if p.startswith("patientmotion") or p.startswith("motion"):
            return "motion"
    return None


@dataclass(frozen=True)
class ImageRef:
    path: str  # relative to DATA_ROOT, forward slashes
    label: str  # bucket
    origin: str  # training | testing | blurred | noisy | motion


def find_images(root: Path = DATA_ROOT) -> list[ImageRef]:
    """Every image under root, labeled by its class folder. Sorted, so runs are reproducible."""
    if not root.exists():
        raise FileNotFoundError(f"{root} not found. See the download command in classifier/dataset.py.")
    refs = []
    for p in sorted(root.rglob("*")):
        if p.suffix.lower() not in IMAGE_EXTS or not p.is_file():
            continue
        rel = p.relative_to(root)
        label = bucket_for_folder(p.parent.name)
        origin = origin_for_path(rel)
        if label is None or origin is None:
            raise ValueError(f"cannot label {rel}: unknown class folder or origin")
        refs.append(ImageRef(rel.as_posix(), label, origin))
    return refs
