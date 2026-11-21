"""Runtime policy loader (safety rule 8).

Reads policies/policy.yaml at startup and again whenever the file changes, so
staff edits take effect with no code change. A bad edit is rejected and the
last good policy stays in force. Settings that back a non-negotiable safety
rule are locked: no edit can loosen them.
"""

from __future__ import annotations

import hashlib
import os
import threading
from pathlib import Path
from typing import Annotated, Literal

import yaml
from pydantic import Field, ValidationError, model_validator

from agent.audit import audit
from agent.schemas import Gate, NonEmptyStr, PolicyContext, Schema

POLICY_PATH = Path(
    os.environ.get(
        "CORTEXA_POLICY", Path(__file__).resolve().parents[1] / "policies" / "policy.yaml"
    )
)

# Gates rule 2 names explicitly. Staff may add gates, never remove these.
LOCKED_GATES: frozenset[str] = frozenset({"confirm_bucket", "enter_results", "review_shortlist"})
GEOCODE_FIELDS_SAFE: frozenset[str] = frozenset({"zip", "city"})



class PolicyError(Exception):
    """Policy file is missing, unreadable, or breaks a rule."""


# ---------------------------------------------------------------------------
# Policy model: mirrors policy.yaml. Literal[True] / Literal["never"] = locked.
# ---------------------------------------------------------------------------


class ClassifierPolicy(Schema):
    min_confidence_for_auto_suggest: Annotated[float, Field(ge=0, le=1)]
    flag_on_model_disagreement: bool
    # Rule 1: model output is always "suspected". A blocklist would let
    # "Confirmed" or "Definitive" through, so only the one safe value is allowed.
    label_prefix: Literal["Suspected"]


class ApprovalsPolicy(Schema):
    required: list[Gate]

    @model_validator(mode="after")
    def _keep_locked_gates(self) -> ApprovalsPolicy:
        missing = LOCKED_GATES - set(self.required)
        if missing:
            raise ValueError(f"locked by safety rule 2: approvals.required must keep {sorted(missing)}")
        return self


class EligibilityPolicy(Schema):
    missing_field_status: Literal["unknown"]  # rule 3: never guessed
    missing_field_reason_template: NonEmptyStr
    require_evidence_citation: Literal[True]  # rule 4

    @model_validator(mode="after")
    def _template_names_field(self) -> EligibilityPolicy:
        if "{field}" not in self.missing_field_reason_template:
            raise ValueError("missing_field_reason_template must contain {field}")
        return self


class NoMatchPolicy(Schema):
    show_near_misses: bool
    alert_on_new_trials: bool
    log_for_tumor_board: bool


class TrialSearchPolicy(Schema):
    statuses: Annotated[list[NonEmptyStr], Field(min_length=1)]
    status_labels: dict[str, NonEmptyStr]
    # No distance cutoff: every eligible trial is shown, nearest first.
    # Distance sorts and labels trials; it never filters them out.
    # An unknown on one of these puts a trial in the "needs_test" group.
    key_unknown_fields: list[NonEmptyStr]
    no_match_plan: NoMatchPolicy

    @model_validator(mode="after")
    def _consistent(self) -> TrialSearchPolicy:
        unlabeled = [s for s in self.statuses if s not in self.status_labels]
        if unlabeled:
            raise ValueError(f"status_labels missing for {unlabeled}")
        return self


class MutationCheckPolicy(Schema):
    run_only_for: Literal["recurrent_glioma"]
    # Locked: false-alarm calibration only holds for exome/genome, not small panels.
    require_broad_sequencing: Literal[True]
    false_alarm_rate: Annotated[float, Field(gt=0, lt=1)]
    never_route_to_immunotherapy_on_mutation_count: Literal[True]  # rule 10


PhraseList = Annotated[list[NonEmptyStr], Field(min_length=1)]


class PreSurgeryPolicy(Schema):
    """Phrases that mark a trial as needing enrollment before surgery (pre-op screen).

    Inclusion and exclusion lists are separate on purpose: "prior resection" in an
    inclusion list means the trial is for recurrent tumors, the opposite case.
    """

    inclusion_phrases: PhraseList
    exclusion_phrases: PhraseList
    title_hints: list[NonEmptyStr] = []
    match: Literal["case_insensitive_substring"]
    # Rule 4: a title alone cites no criterion, so it can never flag a trial by itself.
    llm_confirm_on_title_hint_only: Literal[True]


class PrivacyPolicy(Schema):
    geocode_fields_allowed: Annotated[list[str], Field(min_length=1)]
    patient_contact: Literal["never"]  # rule 5

    @model_validator(mode="after")
    def _zip_or_city_only(self) -> PrivacyPolicy:
        extra = set(self.geocode_fields_allowed) - GEOCODE_FIELDS_SAFE
        if extra:
            raise ValueError(f"locked by safety rule 6: only zip/city may be geocoded, not {sorted(extra)}")
        return self


class AuditPolicy(Schema):
    log_every_tool_call: Literal[True]  # rule 7
    log_every_approval: Literal[True]


class Policy(Schema):
    classifier: ClassifierPolicy
    approvals: ApprovalsPolicy
    eligibility: EligibilityPolicy
    trial_search: TrialSearchPolicy
    mutation_check: MutationCheckPolicy
    pre_surgery_detection: PreSurgeryPolicy
    privacy: PrivacyPolicy
    audit: AuditPolicy

    def status_context(self) -> PolicyContext:
        """Validation context for agent.schemas (allowed statuses + UI labels)."""
        return {
            "statuses": list(self.trial_search.statuses),
            "status_labels": dict(self.trial_search.status_labels),
        }


def parse_policy(text: str) -> Policy:
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise PolicyError(f"policy.yaml is not valid YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise PolicyError("policy.yaml must be a mapping")
    try:
        return Policy.model_validate(data)
    except ValidationError as exc:
        raise PolicyError(str(exc)) from exc


# ---------------------------------------------------------------------------
# Loader: reload on change, keep last good policy on a bad edit.
# ---------------------------------------------------------------------------


class PolicyStore:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()
        self._policy: Policy | None = None
        self._stamp: tuple[int, int] | None = None  # (mtime_ns, size)
        self.last_error: str | None = None

    def _file_stamp(self) -> tuple[int, int]:
        st = self.path.stat()
        return st.st_mtime_ns, st.st_size

    def get(self) -> Policy:
        """Current policy. Reloads first if the file changed since last read."""
        with self._lock:
            try:
                stamp = self._file_stamp()
            except OSError as exc:
                if self._policy is None:
                    raise PolicyError(f"cannot read {self.path}: {exc}") from exc
                return self._policy  # file briefly gone mid-save: keep last good
            if stamp != self._stamp:
                self._reload(stamp)
            assert self._policy is not None
            return self._policy

    def _reload(self, stamp: tuple[int, int]) -> None:
        raw = self.path.read_bytes()
        digest = hashlib.sha256(raw).hexdigest()[:12]
        first = self._policy is None
        try:
            policy = parse_policy(raw.decode("utf-8"))
        except (PolicyError, UnicodeDecodeError) as exc:
            self._stamp = stamp  # don't retry the same bad file on every read
            self.last_error = str(exc)
            audit("system", "policy_reload_failed", {"sha": digest, "error": str(exc)[:1000]})
            if first:
                raise PolicyError(f"no valid policy at startup: {exc}") from exc
            return
        self._policy, self._stamp, self.last_error = policy, stamp, None
        audit("system", "policy_loaded" if first else "policy_reloaded", {"sha": digest})


_default = PolicyStore(POLICY_PATH)


def get_policy() -> Policy:
    return _default.get()


def status_context() -> PolicyContext:
    return _default.get().status_context()


def set_policy_store(store: PolicyStore) -> None:
    """Swap the process-wide store (tests)."""
    global _default
    _default = store
