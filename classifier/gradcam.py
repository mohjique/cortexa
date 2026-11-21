"""Grad-CAM heatmaps and the source-artifact sanity check (F2.2).

Grad-CAM (Selvaraju et al. 2017) on ResNet-18's last conv block (`layer4`):
weight each 7x7 feature map by the mean gradient of the target class logit,
sum, ReLU, upsample to the input size. No extra dependencies.

The "no tumor" class comes from a different source dataset (Br35H) than the
tumor classes, so the model could learn the source (frame, borders, text,
contrast) instead of anatomy. Two checks, from the repo root:

    python -m classifier.gradcam demo    # heatmaps for the 23 demo images
    python -m classifier.gradcam check   # artifact check on the test split

`check` reports, per true class:
- where the heatmap mass falls: inside the head, on dark background, or in
  the outer border band where frames and burned-in text live;
- border blackout: accuracy before and after blacking out the outer border.
  If "no tumor" accuracy collapses, the model was reading the border.
It also writes a montage per class to look at by eye.

Outputs go to artifacts/classifier/heatmaps/ (gitignored). Images are from a
public research dataset; no patient data.
"""

from __future__ import annotations

import argparse
import csv
import json
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import torch
from PIL import Image, ImageDraw, ImageFilter
from torch.nn import functional as F

from classifier.dataset import BUCKETS, DATA_ROOT, REPO_ROOT, SPLIT_CSV
from classifier.model import LoadedModel

MODEL_PATH = REPO_ROOT / "artifacts" / "classifier" / "model.pt"
OUT_DIR = REPO_ROOT / "artifacts" / "classifier" / "heatmaps"
DEMO_JSON = Path(__file__).resolve().parent / "splits" / "demo_images.json"
BORDER = 0.12  # outer band, as a fraction of each side
HEAD_THRESHOLD = 0.08  # grayscale [0,1] above this (after blur) counts as head


@dataclass
class CamResult:
    cam: np.ndarray  # (H, W) in [0, 1], at the model's input size
    probs: dict[str, float]  # calibrated
    target: str  # class the heatmap explains


def gradcam(m: LoadedModel, image: Image.Image, target: str | None = None) -> CamResult:
    """Heatmap for `target` (default: the predicted class)."""
    model = m.model
    feats: dict[str, torch.Tensor] = {}

    def keep(_mod, _inp, out):
        out.retain_grad()
        feats["a"] = out

    handle = model.layer4.register_forward_hook(keep)
    try:
        x = m.transform(image.convert("RGB")).unsqueeze(0).to(m.device)
        model.zero_grad(set_to_none=True)
        with torch.enable_grad():
            logits = model(x)
            idx = BUCKETS.index(target) if target else int(logits.argmax(1))
            logits[0, idx].backward()
    finally:
        handle.remove()
    a = feats["a"]
    weights = a.grad.mean(dim=(2, 3), keepdim=True)  # (1, C, 1, 1)
    cam = F.relu((weights * a).sum(1, keepdim=True))
    cam = F.interpolate(cam, size=x.shape[-2:], mode="bilinear", align_corners=False)[0, 0]
    cam = cam.detach().cpu().numpy()
    cam = cam / cam.max() if cam.max() > 0 else cam
    p = torch.softmax(logits.detach() / m.temperature, 1)[0].cpu()
    return CamResult(cam, {b: float(p[i]) for i, b in enumerate(BUCKETS)}, BUCKETS[idx])


def _jet(v: np.ndarray) -> np.ndarray:
    """Jet colormap for v in [0, 1] -> uint8 RGB."""
    r = np.clip(1.5 - np.abs(4 * v - 3), 0, 1)
    g = np.clip(1.5 - np.abs(4 * v - 2), 0, 1)
    b = np.clip(1.5 - np.abs(4 * v - 1), 0, 1)
    return (np.stack([r, g, b], -1) * 255).astype(np.uint8)


def overlay(image: Image.Image, cam: np.ndarray, alpha: float = 0.45) -> Image.Image:
    """Heatmap over the grayscale scan, at the heatmap's size."""
    h, w = cam.shape
    base = np.asarray(image.convert("L").resize((w, h)), dtype=np.float32)[..., None].repeat(3, -1)
    out = (1 - alpha) * base + alpha * _jet(cam).astype(np.float32)
    return Image.fromarray(out.clip(0, 255).astype(np.uint8))


def head_mask(image: Image.Image, size: tuple[int, int]) -> np.ndarray:
    """True where the scan shows the head (not dark background)."""
    g = image.convert("L").resize(size).filter(ImageFilter.GaussianBlur(3))
    return np.asarray(g, dtype=np.float32) / 255.0 > HEAD_THRESHOLD


def border_mask(size: tuple[int, int], frac: float = BORDER) -> np.ndarray:
    w, h = size
    bw, bh = max(1, int(w * frac)), max(1, int(h * frac))
    m = np.ones((h, w), dtype=bool)
    m[bh:h - bh, bw:w - bw] = False
    return m


def cam_shares(image: Image.Image, cam: np.ndarray) -> dict[str, float]:
    """Fraction of heatmap mass on the head, on background, and in the border band."""
    size = (cam.shape[1], cam.shape[0])
    total = float(cam.sum()) or 1.0
    head = head_mask(image, size)
    return {
        "head": float(cam[head].sum()) / total,
        "background": float(cam[~head].sum()) / total,
        "border": float(cam[border_mask(size)].sum()) / total,
    }


def black_out_border(image: Image.Image, frac: float = BORDER) -> Image.Image:
    img = image.convert("RGB").copy()
    w, h = img.size
    bw, bh = int(w * frac), int(h * frac)
    d = ImageDraw.Draw(img)
    for box in ((0, 0, w, bh), (0, h - bh, w, h), (0, 0, bw, h), (w - bw, 0, w, h)):
        d.rectangle(box, fill=(0, 0, 0))
    return img


def montage(tiles: list[Image.Image], labels: list[str], cols: int = 4, tile: int = 224) -> Image.Image:
    rows = (len(tiles) + cols - 1) // cols
    sheet = Image.new("RGB", (cols * tile, rows * (tile + 16)), "white")
    d = ImageDraw.Draw(sheet)
    for i, (t, lab) in enumerate(zip(tiles, labels)):
        x, y = (i % cols) * tile, (i // cols) * (tile + 16)
        sheet.paste(t.resize((tile, tile)), (x, y + 16))
        d.text((x + 3, y + 2), lab, fill="black")
    return sheet


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def run_demo(m: LoadedModel, out: Path) -> list[dict]:
    """One heatmap per demo image: artifacts/classifier/heatmaps/demo/<case_id>.png."""
    (out / "demo").mkdir(parents=True, exist_ok=True)
    rows = []
    for d in json.loads(DEMO_JSON.read_text(encoding="utf-8")):
        img = Image.open(DATA_ROOT / d["image_file"])
        r = gradcam(m, img)
        path = out / "demo" / f"{d['case_id']}.png"
        overlay(img, r.cam).save(path)
        rows.append({"case_id": d["case_id"], "true": d["kaggle_label"], "heatmap_for": r.target,
                     "confidence": round(max(r.probs.values()), 4), "heatmap_path": path.as_posix(),
                     **{k: round(v, 3) for k, v in cam_shares(img, r.cam).items()}})
    (out / "demo" / "index.json").write_text(json.dumps(rows, indent=2), encoding="utf-8")
    return rows


def run_check(m: LoadedModel, out: Path, per_class: int | None = None, montage_n: int = 12) -> dict:
    """Artifact check on the clean test split. `per_class` caps images per class (None = all)."""
    with SPLIT_CSV.open(encoding="utf-8") as f:
        test = [r for r in csv.DictReader(f) if r["split"] == "test"]
    by_class: dict[str, list[dict]] = defaultdict(list)
    for r in test:
        if per_class is None or len(by_class[r["label"]]) < per_class:
            by_class[r["label"]].append(r)

    report: dict[str, dict] = {}
    for label in BUCKETS:
        shares, ok, ok_blackout, tiles, names = [], 0, 0, [], []
        for r in by_class[label]:
            img = Image.open(DATA_ROOT / r["path"])
            res = gradcam(m, img)
            shares.append(cam_shares(img, res.cam))
            ok += res.target == label
            pb = m.predict(black_out_border(img))
            ok_blackout += max(pb, key=pb.get) == label
            if len(tiles) < montage_n:
                tiles.append(overlay(img, res.cam))
                names.append(f"pred {res.target} {max(res.probs.values()):.2f}")
        n = len(by_class[label])
        report[label] = {
            "n": n,
            "accuracy": round(ok / n, 4) if n else None,
            "accuracy_border_blacked_out": round(ok_blackout / n, 4) if n else None,
        }
        for k in ("head", "background", "border"):
            report[label][f"mean_{k}_share"] = (
                round(float(np.mean([s[k] for s in shares])), 3) if shares else None)
        if tiles:
            montage(tiles, names).save(out / f"montage_{label}.png")
    out.mkdir(parents=True, exist_ok=True)
    (out / "artifact_check.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Grad-CAM heatmaps and artifact check")
    ap.add_argument("command", choices=["demo", "check"])
    ap.add_argument("--model", type=Path, default=MODEL_PATH)
    ap.add_argument("--out", type=Path, default=OUT_DIR)
    ap.add_argument("--per-class", type=int, default=None, help="check: cap images per class")
    args = ap.parse_args(argv)
    m = LoadedModel(args.model)
    args.out.mkdir(parents=True, exist_ok=True)
    if args.command == "demo":
        for r in run_demo(m, args.out):
            print(f"{r['case_id']} true={r['true']:<10} cam_for={r['heatmap_for']:<10} "
                  f"conf={r['confidence']:.3f} head={r['head']:.2f} border={r['border']:.2f}")
    else:
        print(json.dumps(run_check(m, args.out, args.per_class), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
