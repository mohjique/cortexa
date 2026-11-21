"""Real MRI classifier: the trained ResNet-18 behind the stub's `Classifier` protocol.

Calibrated probabilities from `model.pt` (temperature-scaled on held-out data),
a Grad-CAM heatmap for the suspected bucket, saved under the data folder, and
`ClassifierOutput` out. The output is always "suspected" (rule 1): the doctor
confirms every bucket at the confirm_bucket gate.

MedGemma is not wired in, so `medgemma_bucket` is None and model
disagreement can't flag a case. The agent recomputes `flagged_uncertain`
from policy.yaml (F2.4); the value set here is only the classifier's view.

Images are decoded in memory and never written to disk; only the heatmap is.
"""

from __future__ import annotations

import io
import os
from pathlib import Path

from PIL import Image, UnidentifiedImageError

from agent.schemas import ClassifierOutput

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MODEL = Path(os.environ.get("CORTEXA_MODEL_PATH", REPO_ROOT / "artifacts" / "classifier" / "model.pt"))


class BadImage(ValueError):
    """The upload is not an image PIL can read."""


class ResNetClassifier:
    def __init__(
        self,
        model_path: Path = DEFAULT_MODEL,
        heatmap_dir: Path | None = None,
        min_confidence_for_auto_suggest: float = 0.99,  # names match policy.yaml
    ) -> None:
        from classifier.model import LoadedModel  # torch import only when used

        self.model = LoadedModel(Path(model_path))
        if heatmap_dir is None:
            from agent.audit import DATA_DIR

            heatmap_dir = DATA_DIR / "heatmaps"
        self.heatmap_dir = Path(heatmap_dir)
        self.min_confidence_for_auto_suggest = min_confidence_for_auto_suggest

    def classify(self, case_id: str, image: bytes, filename: str | None = None) -> ClassifierOutput:
        from classifier import gradcam

        try:
            img = Image.open(io.BytesIO(image))
            img.load()
        except (UnidentifiedImageError, OSError) as exc:
            raise BadImage(f"not a readable image: {exc}") from None
        r = gradcam.gradcam(self.model, img)
        self.heatmap_dir.mkdir(parents=True, exist_ok=True)
        heatmap = self.heatmap_dir / f"{case_id}.png"
        gradcam.overlay(img, r.cam).save(heatmap)
        # 4 decimals keeps the sum well inside the schema's 0.01 tolerance.
        probs = {b: round(p, 4) for b, p in r.probs.items()}
        confidence = probs[r.target]
        return ClassifierOutput(
            case_id=case_id,
            suspected_bucket=r.target,
            probabilities=probs,
            medgemma_bucket=None,
            confidence=confidence,
            flagged_uncertain=confidence < self.min_confidence_for_auto_suggest,
            heatmap_path=heatmap.as_posix(),
        )
