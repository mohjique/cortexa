"""Fine-tune ResNet-18 on the deduplicated split, calibrate, evaluate (F2.1, F2.3).

    python -m classifier.train --out artifacts/classifier [--epochs 10]

Reads classifier/splits/split.csv (made by dedupe_split.py), so every machine
trains on the same split. Uses the GPU when there is one.

- train:  fit the weights
- calib:  pick the best epoch and fit the temperature (confidence calibration)
- test:   per-class accuracy, reported once at the end. Excludes demo_pool.
- demo_pool / robust_*: predictions only, for picking demo images (issue #22)
  and the robustness check on blurred / noisy / motion copies of test images.

Writes to --out: model.pt, metrics.json, predictions.csv.
"""

from __future__ import annotations

import argparse
import csv
import json
import random
import time
from collections import Counter
from pathlib import Path

import numpy as np
import torch
from PIL import Image
from torch import nn
from torch.utils.data import DataLoader, Dataset

from classifier.dataset import BUCKETS, DATA_ROOT, SPLIT_CSV
from classifier.model import (
    IMG_SIZE,
    build_model,
    eval_transform,
    expected_calibration_error,
    fit_temperature,
    save_checkpoint,
    train_transform,
)

SEED = 2026_11_21


class MriDataset(Dataset):
    def __init__(self, rows: list[dict], transform) -> None:
        self.rows, self.transform = rows, transform

    def __len__(self) -> int:
        return len(self.rows)

    def __getitem__(self, i: int):
        r = self.rows[i]
        with Image.open(DATA_ROOT / r["path"]) as im:
            x = self.transform(im.convert("RGB"))
        return x, BUCKETS.index(r["label"]), i


def load_split(path: Path = SPLIT_CSV) -> dict[str, list[dict]]:
    with path.open(newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    out: dict[str, list[dict]] = {}
    for r in rows:
        out.setdefault(r["split"], []).append(r)
    return out


@torch.no_grad()
def run_logits(model: nn.Module, loader: DataLoader, device: str) -> tuple[torch.Tensor, torch.Tensor]:
    model.eval()
    logits, labels = [], []
    for x, y, _ in loader:
        with torch.autocast(device_type=device, enabled=device == "cuda"):
            logits.append(model(x.to(device, non_blocking=True)).float().cpu())
        labels.append(y)
    return torch.cat(logits), torch.cat(labels)


def per_class(probs: torch.Tensor, labels: torch.Tensor) -> dict:
    pred = probs.argmax(1)
    conf = [[int(((labels == t) & (pred == p)).sum()) for p in range(len(BUCKETS))] for t in range(len(BUCKETS))]
    acc = {b: (conf[i][i] / max(1, sum(conf[i]))) for i, b in enumerate(BUCKETS)}
    return {
        "n": int(len(labels)),
        "accuracy": float((pred == labels).float().mean()) if len(labels) else None,
        "per_class_accuracy": acc,
        "per_class_n": {b: sum(conf[i]) for i, b in enumerate(BUCKETS)},
        "confusion_matrix": {"rows_true_cols_pred": list(BUCKETS), "matrix": conf},
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, default=Path("artifacts/classifier"))
    ap.add_argument("--epochs", type=int, default=10)
    ap.add_argument("--batch", type=int, default=64)
    ap.add_argument("--lr", type=float, default=3e-4)
    ap.add_argument("--workers", type=int, default=2)
    ap.add_argument("--limit", type=int, default=0, help="debug: cap rows per split")
    a = ap.parse_args()

    random.seed(SEED); np.random.seed(SEED); torch.manual_seed(SEED)
    device = "cuda" if torch.cuda.is_available() else "cpu"
    print(f"device: {device}" + (f" ({torch.cuda.get_device_name(0)})" if device == "cuda" else ""))

    split = load_split()
    if a.limit:
        rng = random.Random(SEED)  # random, not the first rows (those are one class)
        split = {k: rng.sample(v, min(a.limit, len(v))) for k, v in split.items()}
    for k in ("train", "calib", "test", "demo_pool"):
        print(f"  {k:10s} {len(split.get(k, [])):5d}  {dict(Counter(r['label'] for r in split.get(k, [])))}")
    overlap = {r["path"] for r in split["train"]} & {r["path"] for k in ("calib", "test", "demo_pool") for r in split[k]}
    assert not overlap, f"split leak: {sorted(overlap)[:3]}"

    pin = device == "cuda"
    mk = lambda rows, tf, shuffle: DataLoader(  # noqa: E731
        MriDataset(rows, tf), batch_size=a.batch, shuffle=shuffle, num_workers=a.workers, pin_memory=pin)
    train_dl = mk(split["train"], train_transform(), True)
    eval_sets = [k for k in split if k in ("calib", "test", "demo_pool") or k.startswith("robust_")]
    eval_dl = {k: mk(split[k], eval_transform(), False) for k in eval_sets}

    model = build_model(pretrained=True).to(device)
    # Class-balanced loss: no_tumor has ~20% more images than glioma.
    counts = Counter(r["label"] for r in split["train"])
    weight = torch.tensor([len(split["train"]) / (len(BUCKETS) * max(1, counts[b])) for b in BUCKETS], dtype=torch.float32)
    loss_fn = nn.CrossEntropyLoss(weight=weight.to(device), label_smoothing=0.05)
    opt = torch.optim.AdamW(model.parameters(), lr=a.lr, weight_decay=1e-4)
    sched = torch.optim.lr_scheduler.OneCycleLR(opt, max_lr=a.lr, total_steps=a.epochs * len(train_dl))
    scaler = torch.amp.GradScaler(enabled=device == "cuda")

    best_acc, best_state, history = -1.0, None, []
    for epoch in range(1, a.epochs + 1):
        model.train()
        t0, total, n = time.time(), 0.0, 0
        for x, y, _ in train_dl:
            x, y = x.to(device, non_blocking=True), y.to(device, non_blocking=True)
            opt.zero_grad(set_to_none=True)
            with torch.autocast(device_type=device, enabled=device == "cuda"):
                loss = loss_fn(model(x), y)
            scaler.scale(loss).backward()
            scaler.step(opt)
            scaler.update()
            sched.step()
            total, n = total + float(loss) * len(y), n + len(y)
        lg, lb = run_logits(model, eval_dl["calib"], device)
        acc = float((lg.argmax(1) == lb).float().mean())
        history.append({"epoch": epoch, "train_loss": total / n, "calib_acc": acc, "sec": round(time.time() - t0, 1)})
        print(f"epoch {epoch:2d}  loss {total / n:.4f}  calib acc {acc:.4f}  ({time.time() - t0:.0f}s)")
        if acc > best_acc:
            best_acc, best_state = acc, {k: v.detach().cpu().clone() for k, v in model.state_dict().items()}

    model.load_state_dict(best_state)
    calib_logits, calib_labels = run_logits(model, eval_dl["calib"], device)
    temperature = fit_temperature(calib_logits, calib_labels)
    print(f"temperature: {temperature:.3f}")

    metrics: dict = {"device": device, "epochs": a.epochs, "img_size": IMG_SIZE, "history": history,
                     "temperature": temperature,
                     "note": "test excludes the demo_pool images (issue #22). Dedupe by perceptual hash before split."}
    pred_rows = []
    for k, dl in eval_dl.items():
        lg, lb = (calib_logits, calib_labels) if k == "calib" else run_logits(model, dl, device)
        raw, cal = torch.softmax(lg, 1), torch.softmax(lg / temperature, 1)
        m = per_class(cal, lb)
        m["ece_uncalibrated"] = expected_calibration_error(raw, lb)
        m["ece_calibrated"] = expected_calibration_error(cal, lb)
        metrics[k] = m
        print(f"{k:16s} n={m['n']:4d}  acc={m['accuracy']:.4f}  ECE {m['ece_uncalibrated']:.3f} -> {m['ece_calibrated']:.3f}  "
              + "  ".join(f"{b}={v:.3f}" for b, v in m["per_class_accuracy"].items()))
        for r, p in zip(dl.dataset.rows, cal):
            pred_rows.append({"path": r["path"], "split": k, "label": r["label"],
                              "pred": BUCKETS[int(p.argmax())], "confidence": round(float(p.max()), 4),
                              **{f"p_{b}": round(float(p[i]), 4) for i, b in enumerate(BUCKETS)}})

    a.out.mkdir(parents=True, exist_ok=True)
    save_checkpoint(a.out / "model.pt", model, temperature, {"best_calib_acc": best_acc, "split_csv_rows": sum(map(len, split.values()))})
    (a.out / "metrics.json").write_text(json.dumps(metrics, indent=2), encoding="utf-8")
    with (a.out / "predictions.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(pred_rows[0]))
        w.writeheader()
        w.writerows(pred_rows)
    print(f"wrote {a.out}/model.pt, metrics.json, predictions.csv")


if __name__ == "__main__":
    main()
