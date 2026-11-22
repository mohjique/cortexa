"""MedGemma: evaluated alone, then used for reasoning on the ResNet call (F2.3).

CLAUDE.md: "Evaluate MedGemma alone on the test set first. If it is weak on
brain MRI, use it to write the plain-language findings summary instead of
voting." It was weak (overall accuracy below 0.5 on the deduplicated test
split), so it does not vote. Instead it gets the ResNet classifier's call and
the decision-tree pathway for that bucket (agent/reasoning.py) and explains
them. Runs on a GPU (Colab: classifier/medgemma_colab.ipynb):

    python -m classifier.medgemma eval --per-class 25   # MedGemma alone on the test split
    python -m classifier.medgemma demo                  # reasoning for the 23 demo images

Outputs (JSON, committed, synthetic case ids only):
- classifier/medgemma/eval.json (eval only, not committed): per-class accuracy, confusion, raw answers
- classifier/medgemma/demo.json: per demo case, `reasoning` and the `reasoning_bucket` it was written for

The ResNet call per demo image comes from classifier/splits/demo_images.json
(`model_pred`, `model_confidence`). The agent reads demo.json at upload time
(no GPU in the demo) and attaches the reasoning only when `reasoning_bucket`
matches that upload's suspected bucket.

Rule 1 is enforced in code, not only in the prompt: `agent.reasoning.check_reasoning`
rejects text that diagnoses, recommends, says what anyone should do, refers,
or names a tumor type without hedging. A rejected text is dropped.

Model: google/medgemma-4b-it (gated: accept the terms on Hugging Face and
set HF_TOKEN).
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import time
from collections import Counter, defaultdict
from pathlib import Path

from agent.reasoning import REASONING_SYSTEM, check_reasoning, reasoning_prompt
from classifier.dataset import BUCKETS, DATA_ROOT, SPLIT_CSV

MODEL_ID = "google/medgemma-4b-it"
OUT_DIR = Path(__file__).resolve().parent / "medgemma"
DEMO_JSON = Path(__file__).resolve().parent / "splits" / "demo_images.json"
PROMPT_VERSION = "v1"            # eval (multiple choice)
REASONING_PROMPT_VERSION = "r1"  # demo reasoning

CHOICES = {"A": "glioma", "B": "meningioma", "C": "pituitary", "D": "no_tumor"}

CLASSIFY_PROMPT = (
    "This is a single slice from a brain MRI. Which option best describes it?\n"
    "A) glioma\nB) meningioma\nC) pituitary tumor\nD) no tumor\n"
    "Answer with a single letter."
)


def parse_choice(answer: str) -> str | None:
    """Bucket from a multiple-choice answer, or None if it can't be read."""
    a = answer.strip()
    m = re.match(r"^\(?([ABCD])\b", a, re.IGNORECASE)
    if m:
        return CHOICES[m.group(1).upper()]
    low = a.lower()
    found = [b for b, words in (("no_tumor", ("no tumor", "no tumour", "normal")),
                                ("meningioma", ("meningioma",)),
                                ("pituitary", ("pituitary",)),
                                ("glioma", ("glioma", "glioblastoma", "astrocytoma")))
             if any(w in low for w in words)]
    return found[0] if len(found) == 1 else None


# --- model ----------------------------------------------------------------------


class MedGemma:
    def __init__(self, model_id: str = MODEL_ID) -> None:
        import torch
        from transformers import AutoModelForImageTextToText, AutoProcessor

        self.torch = torch
        self.processor = AutoProcessor.from_pretrained(model_id)
        self.model = AutoModelForImageTextToText.from_pretrained(
            model_id, torch_dtype=torch.bfloat16, device_map="auto")
        self.model_id = model_id

    def ask(self, image, prompt: str, system: str | None = None, max_new_tokens: int = 120) -> str:
        messages = []
        if system:
            messages.append({"role": "system", "content": [{"type": "text", "text": system}]})
        messages.append({"role": "user", "content": [
            {"type": "image", "image": image.convert("RGB")}, {"type": "text", "text": prompt}]})
        inputs = self.processor.apply_chat_template(
            messages, add_generation_prompt=True, tokenize=True, return_dict=True,
            return_tensors="pt").to(self.model.device, dtype=self.torch.bfloat16)
        n = inputs["input_ids"].shape[-1]
        with self.torch.inference_mode():
            out = self.model.generate(**inputs, max_new_tokens=max_new_tokens, do_sample=False)
        return self.processor.decode(out[0][n:], skip_special_tokens=True).strip()


def _open(rel: str):
    from PIL import Image

    return Image.open(DATA_ROOT / rel)


def run_eval(mg: MedGemma, per_class: int) -> dict:
    with SPLIT_CSV.open(encoding="utf-8") as f:
        test = [r for r in csv.DictReader(f) if r["split"] == "test"]
    picked: dict[str, list[dict]] = defaultdict(list)
    for r in test:
        if len(picked[r["label"]]) < per_class:
            picked[r["label"]].append(r)
    rows, confusion = [], defaultdict(Counter)
    t0 = time.time()
    for label in BUCKETS:
        for r in picked[label]:
            ans = mg.ask(_open(r["path"]), CLASSIFY_PROMPT, max_new_tokens=8)
            pred = parse_choice(ans)
            confusion[label][pred or "unreadable"] += 1
            rows.append({"path": r["path"], "true": label, "answer": ans, "pred": pred})
    per_class_acc = {b: round(confusion[b][b] / max(1, len(picked[b])), 3) for b in BUCKETS}
    n = len(rows)
    report = {
        "model": mg.model_id, "prompt_version": PROMPT_VERSION, "n": n,
        "accuracy": round(sum(r["pred"] == r["true"] for r in rows) / max(1, n), 3),
        "per_class_accuracy": per_class_acc,
        "confusion": {k: dict(v) for k, v in confusion.items()},
        "seconds": round(time.time() - t0, 1),
        "rows": rows,
    }
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / "eval.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report


def run_demo(mg: MedGemma) -> list[dict]:
    out = []
    for d in json.loads(DEMO_JSON.read_text(encoding="utf-8")):
        bucket, conf = d["model_pred"], float(d["model_confidence"])
        flagged = bool(d.get("flagged_uncertain_at_0_99"))
        raw = mg.ask(_open(d["image_file"]), reasoning_prompt(bucket, conf, flagged),
                     system=REASONING_SYSTEM, max_new_tokens=220)
        problem = check_reasoning(raw, bucket)  # rule 1 + goes along with the ResNet call
        out.append({
            "case_id": d["case_id"],
            "reasoning_bucket": bucket,  # the ResNet call this was written for
            "resnet_confidence": conf,
            "reasoning": None if problem else " ".join(raw.split()),  # rejected text is not kept
            "reasoning_rejected": problem,
            "model": mg.model_id,
            "prompt_version": REASONING_PROMPT_VERSION,
        })
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / "demo.json").write_text(json.dumps(out, indent=2), encoding="utf-8")
    return out


def cached(case_id: str, path: Path | None = None) -> dict | None:
    """The precomputed demo result for a case, or None. No GPU, no model load."""
    path = path or OUT_DIR / "demo.json"
    if not path.is_file():
        return None
    for r in json.loads(path.read_text(encoding="utf-8")):
        if r.get("case_id") == case_id:
            return r
    return None


def recheck(path: Path | None = None) -> list[dict]:
    """Re-run check_reasoning on each cached reasoning. No GPU, no model load.

    For when check_reasoning changes after a Colab run. Rejected text is not
    kept, so a recheck can only drop more reasoning, never bring any back.
    """
    path = path or OUT_DIR / "demo.json"
    rows = json.loads(path.read_text(encoding="utf-8"))
    for r in rows:
        r.pop("raw_reasoning", None)
        if r.get("reasoning") and (problem := check_reasoning(r["reasoning"], r["reasoning_bucket"])):
            r["reasoning"], r["reasoning_rejected"] = None, problem
        elif r.get("reasoning_rejected"):
            r["reasoning_rejected"] = r["reasoning_rejected"].split(":")[0]  # older reasons quoted the text
    path.write_text(json.dumps(rows, indent=2), encoding="utf-8")
    return rows


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="MedGemma eval and demo summaries")
    ap.add_argument("command", choices=["eval", "demo", "recheck"])
    ap.add_argument("--per-class", type=int, default=25)
    args = ap.parse_args(argv)
    if args.command == "recheck":
        for r in recheck():
            print(r["case_id"], r["reasoning_bucket"], "|", r["reasoning"] or f"REJECTED: {r['reasoning_rejected']}")
        return 0
    mg = MedGemma()
    if args.command == "eval":
        r = run_eval(mg, args.per_class)
        print(json.dumps({k: v for k, v in r.items() if k != "rows"}, indent=2))
    else:
        for r in run_demo(mg):
            print(r["case_id"], r["reasoning_bucket"], "|", r["reasoning"] or f"REJECTED: {r['reasoning_rejected']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
