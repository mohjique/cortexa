"""MedGemma: evaluate it alone, then use it for findings summaries (F2.3).

CLAUDE.md: "Evaluate MedGemma alone on the test set first. If it is weak on
brain MRI, use it to write the plain-language findings summary instead of
voting." This script does both, on a GPU (Colab: classifier/medgemma_colab.ipynb):

    python -m classifier.medgemma eval --per-class 25   # MedGemma alone on the test split
    python -m classifier.medgemma demo                  # summary + call for the 23 demo images
    python -m classifier.medgemma finetune --minutes 14 # LoRA on the train split, then eval + demo

Fine-tuning (LoRA, train split only, so the test and demo images stay unseen)
oversamples the classes zero-shot MedGemma got wrong (glioma, meningioma) and
mixes in blurred / noisy / motion copies as harder scans. The adapter is used
for the A-D call only; summaries come from the base model, so training on
single-letter answers can't change how the summary is written.

Outputs (JSON, committed, synthetic case ids only):
- classifier/medgemma/eval.json: per-class accuracy, confusion, raw answers
- classifier/medgemma/demo.json: per demo case, `findings_summary` and `medgemma_bucket`
- classifier/medgemma/train.json: fine-tune settings, per-class counts, loss curve
The LoRA adapter is a model weight: it is saved outside the repo and never committed.

The agent reads demo.json at upload time (no GPU in the demo). Whether
MedGemma's call counts as a second opinion is `classifier.medgemma_vote` in
policy.yaml; the summary is shown either way.

Rule 1 is enforced in code, not only in the prompt: `agent.findings.check_summary`
rejects text that diagnoses, names a tumor type without hedging, recommends
treatment or refers to a specialist. A rejected summary is dropped.

Model: google/medgemma-4b-it (gated: accept the terms on Hugging Face and
set HF_TOKEN).
"""

from __future__ import annotations

import argparse
import csv
import json
import random
import re
import time
from collections import Counter, defaultdict
from pathlib import Path

from agent.findings import check_summary
from classifier.dataset import BUCKETS, DATA_ROOT, SPLIT_CSV

MODEL_ID = "google/medgemma-4b-it"
OUT_DIR = Path(__file__).resolve().parent / "medgemma"
DEMO_JSON = Path(__file__).resolve().parent / "splits" / "demo_images.json"
PROMPT_VERSION = "v1"

# Training images per class for the LoRA run. Weighted toward the classes the
# zero-shot eval got wrong; no_tumor was already accurate, so it gets the fewest
# (enough that the model doesn't forget it).
TRAIN_COUNTS = {"glioma": 340, "meningioma": 340, "pituitary": 170, "no_tumor": 110}
VARIANT_FRACTION = 0.3  # share drawn from blurred / noisy / motion copies (harder scans)
CLEAN_ORIGINS = ("training", "testing")
LETTER = {"glioma": "A", "meningioma": "B", "pituitary": "C", "no_tumor": "D"}

CHOICES = {"A": "glioma", "B": "meningioma", "C": "pituitary", "D": "no_tumor"}

CLASSIFY_PROMPT = (
    "This is a single slice from a brain MRI. Which option best describes it?\n"
    "A) glioma\nB) meningioma\nC) pituitary tumor\nD) no tumor\n"
    "Answer with a single letter."
)

SUMMARY_SYSTEM = (
    "You help a neuro-oncology care team by describing imaging findings. "
    "You never give a diagnosis, never recommend treatment, and never suggest a referral. "
    "Any tumor type you mention must be written as suspected."
)
SUMMARY_PROMPT = (
    "Describe the visible findings in this brain MRI slice in 2 or 3 short sentences: "
    "the plane and sequence if you can tell, and the location, size and appearance of any mass. "
    "If no abnormality is visible, say so. Write 'suspected' before any tumor type."
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
        self.finetune_info: dict | None = None  # set by finetune()

    def ask(self, image, prompt: str, system: str | None = None, max_new_tokens: int = 120,
            adapter: bool = True) -> str:
        """Generate an answer. adapter=False uses the base model even after finetune()."""
        if not adapter and self.finetune_info is not None:
            with self.model.disable_adapter():
                return self._ask(image, prompt, system, max_new_tokens)
        return self._ask(image, prompt, system, max_new_tokens)

    @staticmethod
    def messages(image, prompt: str, system: str | None = None) -> list[dict]:
        messages = []
        if system:
            messages.append({"role": "system", "content": [{"type": "text", "text": system}]})
        messages.append({"role": "user", "content": [
            {"type": "image", "image": image.convert("RGB")}, {"type": "text", "text": prompt}]})
        return messages

    def _ask(self, image, prompt: str, system: str | None, max_new_tokens: int) -> str:
        inputs = self.processor.apply_chat_template(
            self.messages(image, prompt, system), add_generation_prompt=True, tokenize=True, return_dict=True,
            return_tensors="pt").to(self.model.device, dtype=self.torch.bfloat16)
        n = inputs["input_ids"].shape[-1]
        with self.torch.inference_mode():
            out = self.model.generate(**inputs, max_new_tokens=max_new_tokens, do_sample=False)
        return self.processor.decode(out[0][n:], skip_special_tokens=True).strip()


def _open(rel: str):
    from PIL import Image

    return Image.open(DATA_ROOT / rel)


# --- fine-tune ------------------------------------------------------------------


def pick_train(counts: dict[str, int] = TRAIN_COUNTS, variant_fraction: float = VARIANT_FRACTION,
               seed: int = 0, split_csv: Path = SPLIT_CSV) -> list[dict]:
    """Shuffled rows from the train split only, so test and demo images stay unseen."""
    with split_csv.open(encoding="utf-8") as f:
        train = [r for r in csv.DictReader(f) if r["split"] == "train"]
    rng = random.Random(seed)
    picked: list[dict] = []
    for label, n in counts.items():
        clean = [r for r in train if r["label"] == label and r["origin"] in CLEAN_ORIGINS]
        variant = [r for r in train if r["label"] == label and r["origin"] not in CLEAN_ORIGINS]
        n_var = min(len(variant), round(n * variant_fraction))
        picked += rng.sample(variant, n_var) + rng.sample(clean, min(len(clean), n - n_var))
    rng.shuffle(picked)
    return picked


def finetune(mg: MedGemma, rows: list[dict], minutes: float = 14.0, lr: float = 2e-4,
             grad_accum: int = 8, rank: int = 16, adapter_dir: str | Path | None = None) -> dict:
    """LoRA-tune the language model to answer the A-D question. Stops after `minutes`.

    Loss is on the answer tokens only; the vision tower stays frozen. Rows are
    pre-shuffled, so an early stop still sees every class in proportion.
    """
    from peft import LoraConfig, get_peft_model

    torch = mg.torch
    model = mg.model
    model.gradient_checkpointing_enable(gradient_checkpointing_kwargs={"use_reentrant": False})
    model.enable_input_require_grads()
    model.config.use_cache = False
    model = get_peft_model(model, LoraConfig(
        r=rank, lora_alpha=2 * rank, lora_dropout=0.05, bias="none",
        target_modules=r".*language_model.*\.(q_proj|k_proj|v_proj|o_proj|gate_proj|up_proj|down_proj)"))
    mg.model = model
    params = [p for p in model.parameters() if p.requires_grad]
    opt = torch.optim.AdamW(params, lr=lr, weight_decay=0.0)
    total_steps = max(1, len(rows) // grad_accum)
    warmup = max(1, total_steps // 10)
    sched = torch.optim.lr_scheduler.LambdaLR(
        opt, lambda s: min(1.0, (s + 1) / warmup) * max(0.1, 1 - s / total_steps))

    model.train()
    t0, losses, seen, running, stopped_early = time.time(), [], 0, 0.0, False
    for r in rows:
        if time.time() - t0 > minutes * 60:
            stopped_early = True
            break
        msgs = MedGemma.messages(_open(r["path"]), CLASSIFY_PROMPT)
        n_prompt = mg.processor.apply_chat_template(
            msgs, add_generation_prompt=True, tokenize=True, return_dict=True,
            return_tensors="pt")["input_ids"].shape[-1]
        full = mg.processor.apply_chat_template(
            msgs + [{"role": "assistant", "content": [{"type": "text", "text": LETTER[r["label"]]}]}],
            tokenize=True, return_dict=True, return_tensors="pt",
        ).to(model.device, dtype=torch.bfloat16)
        labels = full["input_ids"].clone()
        labels[:, :n_prompt] = -100  # learn the answer, not the prompt
        loss = model(**full, labels=labels).loss
        (loss / grad_accum).backward()
        running += loss.item()
        seen += 1
        if seen % grad_accum == 0:
            torch.nn.utils.clip_grad_norm_(params, 1.0)
            opt.step()
            sched.step()
            opt.zero_grad(set_to_none=True)
            losses.append(round(running / grad_accum, 4))
            running = 0.0
            if len(losses) % 10 == 0:
                print(f"step {len(losses)}/{total_steps}  images {seen}  loss {losses[-1]}  "
                      f"{(time.time() - t0) / 60:.1f} min", flush=True)
    if seen % grad_accum:
        opt.step()
        opt.zero_grad(set_to_none=True)
    model.eval()
    model.config.use_cache = True
    if adapter_dir:
        model.save_pretrained(str(adapter_dir))

    used = rows[:seen]
    elapsed = time.time() - t0
    info = {
        "method": "LoRA", "rank": rank, "lr": lr, "grad_accum": grad_accum, "split": "train",
        "images_planned": len(rows), "images_seen": seen, "stopped_early": stopped_early,
        "per_class_seen": dict(Counter(r["label"] for r in used)),
        "variant_share_seen": round(sum(r["origin"] not in CLEAN_ORIGINS for r in used) / max(1, seen), 3),
        "minutes": round(elapsed / 60, 1),
        "seconds_per_image": round(elapsed / max(1, seen), 2),
        "loss_per_step": losses,
    }
    mg.finetune_info = info
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / "train.json").write_text(json.dumps(info, indent=2), encoding="utf-8")
    return info


def run_eval(mg: MedGemma, per_class: int, out_name: str = "eval.json",
             adapter: bool = True) -> dict:
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
            ans = mg.ask(_open(r["path"]), CLASSIFY_PROMPT, max_new_tokens=8, adapter=adapter)
            pred = parse_choice(ans)
            confusion[label][pred or "unreadable"] += 1
            rows.append({"path": r["path"], "true": label, "answer": ans, "pred": pred})
    per_class_acc = {b: round(confusion[b][b] / max(1, len(picked[b])), 3) for b in BUCKETS}
    n = len(rows)
    report = {
        "model": mg.model_id, "prompt_version": PROMPT_VERSION, "n": n,
        "finetuned": bool(adapter and mg.finetune_info),
        "accuracy": round(sum(r["pred"] == r["true"] for r in rows) / max(1, n), 3),
        "per_class_accuracy": per_class_acc,
        "confusion": {k: dict(v) for k, v in confusion.items()},
        "seconds": round(time.time() - t0, 1),
        "rows": rows,
    }
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / out_name).write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report


def run_demo(mg: MedGemma) -> list[dict]:
    out = []
    for d in json.loads(DEMO_JSON.read_text(encoding="utf-8")):
        img = _open(d["image_file"])
        raw = mg.ask(img, SUMMARY_PROMPT, system=SUMMARY_SYSTEM, adapter=False)
        problem = check_summary(raw)
        ans = mg.ask(img, CLASSIFY_PROMPT, max_new_tokens=8)
        out.append({
            "case_id": d["case_id"],
            "findings_summary": None if problem else " ".join(raw.split()),
            "summary_rejected": problem,
            "raw_summary": raw,
            "medgemma_bucket": parse_choice(ans),
            "medgemma_answer": ans,
            "model": mg.model_id,
            "finetuned": bool(mg.finetune_info),
            "prompt_version": PROMPT_VERSION,
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


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="MedGemma eval and demo summaries")
    ap.add_argument("command", choices=["eval", "demo", "finetune"])
    ap.add_argument("--per-class", type=int, default=25)
    ap.add_argument("--minutes", type=float, default=14.0, help="training time cap (finetune)")
    ap.add_argument("--adapter-dir", default=None, help="save the LoRA adapter here (outside the repo)")
    args = ap.parse_args(argv)
    mg = MedGemma()
    if args.command == "finetune":
        info = finetune(mg, pick_train(), minutes=args.minutes, adapter_dir=args.adapter_dir)
        print(json.dumps({k: v for k, v in info.items() if k != "loss_per_step"}, indent=2))
        r = run_eval(mg, args.per_class)
        print(json.dumps({k: v for k, v in r.items() if k != "rows"}, indent=2))
        run_demo(mg)
    elif args.command == "eval":
        r = run_eval(mg, args.per_class)
        print(json.dumps({k: v for k, v in r.items() if k != "rows"}, indent=2))
    else:
        for r in run_demo(mg):
            print(r["case_id"], r["medgemma_bucket"], "|", r["findings_summary"] or f"REJECTED: {r['summary_rejected']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
