"""Copy the 23 demo MRIs into classifier/demo_scans/, named by case ID.

    python -m classifier.demo_scans      # needs the Kaggle dataset in data/brain-tumor-mri/

The files are byte-for-byte copies of the images listed in
classifier/splits/demo_images.json, so the classifier sees exactly the image it
was evaluated on. They are renamed `demo-001.jpg` and so on because the Kaggle
file names carry the label ("glioma", "No-tumor"): the doctor has to read the
scan, not the file name. They are committed (0.16 MB) so the demo and the
deployed backend work without the dataset.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from classifier.dataset import DATA_ROOT

HERE = Path(__file__).resolve().parent
DEMO_JSON = HERE / "splits" / "demo_images.json"
OUT = HERE / "demo_scans"


def build() -> list[Path]:
    OUT.mkdir(exist_ok=True)
    written = []
    for d in json.loads(DEMO_JSON.read_text(encoding="utf-8")):
        src = DATA_ROOT / d["image_file"]
        dst = OUT / f"{d['case_id']}{src.suffix.lower()}"
        shutil.copyfile(src, dst)
        written.append(dst)
    return written


if __name__ == "__main__":
    for p in build():
        print(p.relative_to(HERE.parent))
