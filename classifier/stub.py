"""Stub MRI classifier (F2.6).

Returns canned, schema-valid `ClassifierOutput` so the agent and frontend can
run end to end before the real ResNet + MedGemma model exists. Swap it for the
real classifier later; both implement the `Classifier` protocol.

Profile choice is deterministic:
1. A profile keyword in the upload filename (see stub_profiles.json), e.g.
   `demo_glioma.png`, `case3_notumor.jpg`, `glioma_lowconf.png`.
2. Otherwise a bucket picked from the SHA-256 of the image bytes. That pick is
   arbitrary, so it is always `flagged_uncertain`: the confirm screen must never
   pre-fill a bucket the stub made up.

The output is always "suspected". The agent recomputes `flagged_uncertain` from
policies/policy.yaml (F2.4); the value set here is only the classifier's view.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Protocol

from agent.schemas import BUCKETS, ClassifierOutput

PROFILES_FILE = Path(__file__).with_name("stub_profiles.json")

# Profiles are keyed by bucket name (plus two special profiles). Filenames are
# matched with punctuation stripped, so "no_tumor", "no-tumor" and the Kaggle
# folder name "notumor" all hit the `no_tumor` profile.
_ALIASES = {"notumour": "notumor", "lowconfidence": "lowconf"}


class Classifier(Protocol):
    def classify(
        self, case_id: str, image: bytes, filename: str | None = None
    ) -> ClassifierOutput: ...


def _norm(text: str) -> str:
    return re.sub(r"[^a-z]", "", text.lower())


class StubClassifier:
    def __init__(
        self,
        profiles_file: Path = PROFILES_FILE,
        min_confidence_for_auto_suggest: float = 0.80,  # names match policy.yaml
        flag_on_model_disagreement: bool = True,
    ) -> None:
        raw = json.loads(profiles_file.read_text(encoding="utf-8"))
        # Validate every profile up front so a bad edit fails at startup.
        self.profiles: dict[str, dict] = {}
        for name, p in raw.items():
            if name.startswith("_"):
                continue
            ClassifierOutput(case_id="validate", flagged_uncertain=False, **p)
            self.profiles[name] = p
        self.min_confidence_for_auto_suggest = min_confidence_for_auto_suggest
        self.flag_on_model_disagreement = flag_on_model_disagreement

    def match_keyword(self, filename: str | None) -> str | None:
        if not filename:
            return None
        stem = _norm(Path(filename).stem)
        for alias, name in _ALIASES.items():
            stem = stem.replace(alias, name)
        for name in self.profiles:  # file order: special profiles first
            if _norm(name) in stem:
                return name
        return None

    def pick_profile(self, image: bytes, filename: str | None) -> str:
        name = self.match_keyword(filename)
        if name is not None:
            return name
        return BUCKETS[int(hashlib.sha256(image).hexdigest(), 16) % len(BUCKETS)]

    def classify(
        self, case_id: str, image: bytes, filename: str | None = None
    ) -> ClassifierOutput:
        from_fallback = self.match_keyword(filename) is None
        p = self.profiles[self.pick_profile(image, filename)]
        disagree = (
            p.get("medgemma_bucket") is not None
            and p["medgemma_bucket"] != p["suspected_bucket"]
        )
        flagged_uncertain = (
            from_fallback  # arbitrary hash pick: never present it as confident
            or p["confidence"] < self.min_confidence_for_auto_suggest
            or (self.flag_on_model_disagreement and disagree)
        )
        return ClassifierOutput(
            case_id=case_id,
            flagged_uncertain=flagged_uncertain,
            heatmap_path=None,  # stub has no Grad-CAM
            **p,
        )
