"""Pydantic models for every JSON interface in docs/interfaces.md.

One model per JSON block. All models forbid unknown fields so drift between
components fails loudly. Safety rules from CLAUDE.md that can be checked on a
single record are enforced here, in code, not in the LLM.
"""

from __future__ import annotations

import hashlib
import json
from typing import Annotated, Any, Literal, TypedDict, Union

from pydantic import (
    AfterValidator,
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    TypeAdapter,
    ValidationInfo,
    field_validator,
    model_validator,
)

# ---------------------------------------------------------------------------
# Shared types
# ---------------------------------------------------------------------------

Bucket = Literal["glioma", "meningioma", "pituitary", "no_tumor"]
BUCKETS: tuple[Bucket, ...] = ("glioma", "meningioma", "pituitary", "no_tumor")

Stage = Literal[
    "intake",
    "confirm_bucket",
    "watch_or_treat",
    "preop_screen",
    "hormone_labs",
    "surgery_or_meds",
    "awaiting_pathology",
    "enter_results",
    "mutation_check",
    "matching",
    "review_shortlist",
    "no_match",
    "rescan",
    "stopped",
]


class PolicyContext(TypedDict):
    """Runtime policy passed as `context=` to model_validate (safety rule 8).

    Built from policies/policy.yaml by the policy loader. Without it, statuses
    are only format-checked; with it, they must be in the policy.
    """

    statuses: list[str]  # trial_search.statuses
    status_labels: dict[str, str]  # trial_search.status_labels


def _status_in_policy(value: str, info: ValidationInfo) -> str:
    allowed = (info.context or {}).get("statuses")
    if allowed is not None and value not in allowed:
        raise ValueError(f"recruitment status {value!r} is not in policy statuses {allowed}")
    return value


# ClinicalTrials.gov overallStatus enum style. Allowed values come from policy.
RecruitmentStatus = Annotated[
    str,
    StringConstraints(pattern=r"^[A-Z][A-Z_]*$"),
    AfterValidator(_status_in_policy),
]

EligibilityStatus = Literal["met", "not_met", "unknown"]
Sequencing = Literal["exome", "genome", "panel"]
Base = Literal["A", "C", "G", "T"]

NonEmptyStr = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
# Privacy rule 6: only a 5-digit ZIP leaves the app, never an address.
Zip = Annotated[str, StringConstraints(pattern=r"^\d{5}$")]
TrialId = Annotated[str, StringConstraints(pattern=r"^NCT\d{8}$")]
Unit = Annotated[float, Field(ge=0.0, le=1.0)]
Miles = Annotated[float, Field(ge=0.0)]
Timestamp = AwareDatetime  # timezone required; examples use UTC "Z"


class Schema(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ---------------------------------------------------------------------------
# Case state
# ---------------------------------------------------------------------------


class CaseState(Schema):
    case_id: NonEmptyStr
    stage: Stage
    suspected_bucket: Bucket | None = None
    confirmed_bucket: Bucket | None = None
    treatment_path: Literal["surgery", "medication_only", "watch"] | None = None
    waiting_on: Literal["doctor", "agent", "pathology"]
    updated_at: Timestamp


# ---------------------------------------------------------------------------
# Classifier -> agent
# ---------------------------------------------------------------------------


class ClassifierOutput(Schema):
    case_id: NonEmptyStr
    suspected_bucket: Bucket
    probabilities: dict[Bucket, Unit]
    # Always None: MedGemma does not vote (its solo eval was below 0.5). Kept so
    # the interface shape is stable; see _medgemma_never_votes.
    medgemma_bucket: Bucket | None = None
    confidence: Unit
    # Warning only: low confidence, or the classifier's own flag. Every case
    # still goes through the confirm_bucket doctor gate, flagged or not.
    flagged_uncertain: bool
    heatmap_path: str | None = None
    # Draft findings text from MedGemma. Checked against rule 1 below.
    findings_summary: NonEmptyStr | None = None
    # Draft reasoning from MedGemma, written for `suspected_bucket` (the ResNet
    # call) and its decision-tree pathway. Checked against rule 1 below.
    reasoning: NonEmptyStr | None = None

    @field_validator("medgemma_bucket", mode="before")
    @classmethod
    def _medgemma_never_votes(cls, _: Any) -> None:
        # The ResNet call is the only suspected bucket. Any second-model bucket
        # is dropped here, so no code path can route or flag a case on it.
        # Dropped rather than rejected, so case files saved earlier still load.
        return None

    @model_validator(mode="after")
    def _summary_rule_1(self) -> ClassifierOutput:
        if self.findings_summary is not None:
            from agent.findings import check_summary

            problem = check_summary(self.findings_summary)
            if problem:
                raise ValueError(f"rule 1: findings_summary rejected ({problem})")
        if self.reasoning is not None:
            from agent.reasoning import check_reasoning

            problem = check_reasoning(self.reasoning, self.suspected_bucket)
            if problem:
                raise ValueError(f"rule 1: reasoning rejected ({problem})")
        return self

    @model_validator(mode="after")
    def _check_probabilities(self) -> ClassifierOutput:
        if set(self.probabilities) != set(BUCKETS):
            raise ValueError(f"probabilities must have exactly these keys: {BUCKETS}")
        total = sum(self.probabilities.values())
        if abs(total - 1.0) > 0.01:
            raise ValueError(f"probabilities must sum to 1 (got {total:.3f})")
        return self


# ---------------------------------------------------------------------------
# Doctor-entered results
# ---------------------------------------------------------------------------


class PituitaryResults(Schema):
    treatment_path: Literal["surgery", "medication_only"]
    secreting_hormone: Literal[
        "prolactin", "growth_hormone", "acth", "tsh", "none", "pending", "unknown"
    ]
    size: Literal["micro", "macro", "unknown"]
    cavernous_sinus_invasion: Literal["yes", "no", "unknown"]
    surgical_candidate: bool | None = None  # None = not assessed
    hormone_levels: dict[NonEmptyStr, float] = Field(default_factory=dict)  # name_unit -> value


TreatmentType = Literal[
    "surgery",
    "radiation",
    "temozolomide",
    "bevacizumab",
    "idh_inhibitor",
    "checkpoint_inhibitor",
    "pituitary_radiation",
    "other",
]


class PriorTreatment(Schema):
    """One past treatment, per clinical/PATIENT_PROFILES_GUIDE.md section 5."""

    type: TreatmentType
    detail: str | None = None
    weeks_ago: Annotated[int, Field(ge=0)] | None = None
    extent: Literal["gross_total", "subtotal", "biopsy_only"] | None = None  # surgery only
    cycles: Annotated[int, Field(ge=0)] | None = None  # temozolomide only

    @model_validator(mode="after")
    def _type_specific_fields(self) -> PriorTreatment:
        if self.extent is not None and self.type != "surgery":
            raise ValueError("extent is only allowed for surgery")
        if self.cycles is not None and self.type != "temozolomide":
            raise ValueError("cycles is only allowed for temozolomide")
        return self


Grade = Annotated[int, Field(ge=1, le=4)] | Literal["pending", "unknown"]


class DoctorResults(Schema):
    """`unknown` = not tested. `pending` = ordered, not back. Never guessed.

    Optional fields left as None mean "not recorded"; the eligibility checker
    treats them as unknown, never as "no".
    """

    case_id: NonEmptyStr
    confirmed_bucket: Bucket
    diagnosis: NonEmptyStr | None = None
    grade: Grade | None = None  # None = not applicable (e.g. pituitary)
    idh: Literal["mutant", "wildtype", "pending", "unknown"]
    codeletion_1p19q: Literal["codeleted", "intact", "pending", "unknown"]
    mgmt: Literal["methylated", "unmethylated", "pending", "unknown"]
    tmb_clia: Annotated[float, Field(ge=0)] | None = None  # mut/Mb, CLIA lab only
    recurrent: bool
    recurrence_number: Annotated[int, Field(ge=0)] | None = None
    prior_treatments: list[PriorTreatment] = Field(default_factory=list)
    comorbidities: list[NonEmptyStr] | None = None
    can_use_phone_or_tablet: bool | None = None
    age: Annotated[int, Field(ge=0, le=120)]
    zip: Zip
    broad_sequencing_available: bool
    sequencing: Literal["exome", "genome", "panel", "none"] | None = None
    pituitary: PituitaryResults | None = None

    @model_validator(mode="after")
    def _check_consistency(self) -> DoctorResults:
        if self.recurrence_number is not None:
            if not self.recurrent and self.recurrence_number != 0:
                raise ValueError("recurrence_number must be 0 when recurrent is false")
            if self.recurrent and self.recurrence_number == 0:
                raise ValueError("recurrence_number must be 1 or more when recurrent is true")

        is_pit = self.confirmed_bucket == "pituitary"
        if is_pit and self.pituitary is None:
            raise ValueError("pituitary results required when confirmed_bucket is pituitary")
        if not is_pit and self.pituitary is not None:
            raise ValueError("pituitary results only allowed when confirmed_bucket is pituitary")

        if self.pituitary and self.pituitary.treatment_path == "medication_only":
            markers = {
                "idh": self.idh,
                "codeletion_1p19q": self.codeletion_1p19q,
                "mgmt": self.mgmt,
            }
            bad = [k for k, v in markers.items() if v != "unknown"]
            if bad:
                raise ValueError(
                    f"medication-only pituitary has no pathology; {bad} must be 'unknown'"
                )

        if self.sequencing is not None:
            broad = self.sequencing in ("exome", "genome")
            if broad != self.broad_sequencing_available:
                raise ValueError(
                    f"broad_sequencing_available={self.broad_sequencing_available} "
                    f"conflicts with sequencing={self.sequencing!r}"
                )
        return self


# ---------------------------------------------------------------------------
# Doctor decisions (approval gates). One record per gate.
# ---------------------------------------------------------------------------


class ConfirmBucketDecision(Schema):
    bucket: Bucket
    overrode_model: bool


class WatchOrTreatDecision(Schema):
    choice: Literal["watch", "treat"]
    rescan_in_months: Annotated[int, Field(ge=1, le=60)] | None = None

    @model_validator(mode="after")
    def _rescan_only_for_watch(self) -> WatchOrTreatDecision:
        if self.choice == "watch" and self.rescan_in_months is None:
            raise ValueError("rescan_in_months is required when choice is watch")
        if self.choice == "treat" and self.rescan_in_months is not None:
            raise ValueError("rescan_in_months is only allowed when choice is watch")
        return self


class HormoneLabsDecision(Schema):
    """Pituitary: doctor has reviewed hormone labs and endocrinology."""

    labs_reviewed: Literal[True]


class SurgeryOrMedsDecision(Schema):
    choice: Literal["surgery", "medication_only"]


PreSurgeryAction = Literal["pursue_before_surgery", "not_pursuing"]


class PreSurgeryTrialAck(Schema):
    """Doctor's call on one trial that needs enrollment before surgery."""

    trial_id: TrialId
    action: PreSurgeryAction
    reason: NonEmptyStr | None = None

    @model_validator(mode="after")
    def _reason_when_declined(self) -> PreSurgeryTrialAck:
        # Declining closes the window for good, so the tumor board must see why.
        if self.action == "not_pursuing" and self.reason is None:
            raise ValueError("reason is required when action is not_pursuing")
        return self


class PreOpReviewDecision(Schema):
    """Glioma: doctor has reviewed the tests the biopsy must answer and
    acknowledged every pre-surgery trial. The window closes at surgery."""

    # The screen the doctor reviewed. Rejected if the case's screen has changed since.
    screen_id: NonEmptyStr
    tests_reviewed: Literal[True]
    pre_surgery_trials: list[PreSurgeryTrialAck] = Field(default_factory=list)

    @model_validator(mode="after")
    def _one_ack_per_trial(self) -> PreOpReviewDecision:
        ids = [a.trial_id for a in self.pre_surgery_trials]
        if len(ids) != len(set(ids)):
            raise ValueError("a pre-surgery trial may be acknowledged only once")
        return self


class TrialRejection(Schema):
    trial_id: TrialId
    reason: NonEmptyStr


class ReviewShortlistDecision(Schema):
    keep: list[TrialId] = Field(default_factory=list)
    reject: list[TrialRejection] = Field(default_factory=list)

    @model_validator(mode="after")
    def _no_overlap(self) -> ReviewShortlistDecision:
        rejected = [r.trial_id for r in self.reject]
        ids = self.keep + rejected
        if len(ids) != len(set(ids)):
            raise ValueError("a trial may appear only once across keep and reject")
        return self


class _DecisionRecord(Schema):
    case_id: NonEmptyStr
    doctor_id: NonEmptyStr
    ts: Timestamp
    note: str | None = None


class ConfirmBucketRecord(_DecisionRecord):
    gate: Literal["confirm_bucket"]
    decision: ConfirmBucketDecision


class WatchOrTreatRecord(_DecisionRecord):
    gate: Literal["watch_or_treat"]
    decision: WatchOrTreatDecision


class PreOpReviewRecord(_DecisionRecord):
    gate: Literal["preop_review"]
    decision: PreOpReviewDecision


class HormoneLabsRecord(_DecisionRecord):
    gate: Literal["hormone_labs"]
    decision: HormoneLabsDecision


class SurgeryOrMedsRecord(_DecisionRecord):
    gate: Literal["surgery_or_meds"]
    decision: SurgeryOrMedsDecision


class EnterResultsRecord(_DecisionRecord):
    gate: Literal["enter_results"]
    decision: DoctorResults

    @model_validator(mode="after")
    def _same_case(self) -> EnterResultsRecord:
        if self.decision.case_id != self.case_id:
            raise ValueError("results case_id must match the decision record case_id")
        return self


class ReviewShortlistRecord(_DecisionRecord):
    gate: Literal["review_shortlist"]
    decision: ReviewShortlistDecision


DoctorDecision = Annotated[
    Union[
        ConfirmBucketRecord,
        WatchOrTreatRecord,
        PreOpReviewRecord,
        HormoneLabsRecord,
        SurgeryOrMedsRecord,
        EnterResultsRecord,
        ReviewShortlistRecord,
    ],
    Field(discriminator="gate"),
]
DoctorDecisionAdapter: TypeAdapter[DoctorDecision] = TypeAdapter(DoctorDecision)
Gate = Literal[
    "confirm_bucket",
    "watch_or_treat",
    "preop_review",
    "hormone_labs",
    "surgery_or_meds",
    "enter_results",
    "review_shortlist",
]


# ---------------------------------------------------------------------------
# Sites and distances
# ---------------------------------------------------------------------------


class NearestSite(Schema):
    facility: str | None = None
    city: NonEmptyStr
    state: str | None = None
    lat: Annotated[float, Field(ge=-90, le=90)] | None = None
    lon: Annotated[float, Field(ge=-180, le=180)] | None = None
    distance_miles: Miles  # straight line from ZIP centroid
    drive_minutes: Annotated[int, Field(ge=0)] | None = None  # OSRM; None if unavailable


class TrialSite(Schema):
    facility: NonEmptyStr
    city: NonEmptyStr
    state: str | None = None
    zip: str | None = None  # site postal code; may be non-US
    lat: Annotated[float, Field(ge=-90, le=90)] | None = None
    lon: Annotated[float, Field(ge=-180, le=180)] | None = None


# ---------------------------------------------------------------------------
# Trial record (agent internal)
# ---------------------------------------------------------------------------


class TrialRecord(Schema):
    trial_id: TrialId
    title: NonEmptyStr
    source: Literal["ctgov", "nci", "ctgov+nci"]
    recruitment_status: RecruitmentStatus
    phases: list[str] = Field(default_factory=list)
    conditions: list[str] = Field(default_factory=list)
    criteria_text: str
    min_age_years: Annotated[float, Field(ge=0)] | None = None
    max_age_years: Annotated[float, Field(ge=0)] | None = None
    remote_participation: bool
    sites: list[TrialSite] = Field(default_factory=list)
    fetched_at: Timestamp
    from_cache: bool

    @model_validator(mode="after")
    def _age_range(self) -> TrialRecord:
        lo, hi = self.min_age_years, self.max_age_years
        if lo is not None and hi is not None and lo > hi:
            raise ValueError("min_age_years must not exceed max_age_years")
        return self


# ---------------------------------------------------------------------------
# Eligibility output
# ---------------------------------------------------------------------------


# Mutation check output fields. Research context only (safety rule 10): they
# may never be the evidence that passes or fails a criterion. A CLIA lab TMB
# lives in DoctorResults.tmb_clia instead.
MUTATION_CHECK_FIELDS: frozenset[str] = frozenset(
    {
        "mutation_check",
        "mutation_context",
        "sbs11_call",
        "exposures",
        "total_mutations",
        "counts_96",
        "sbs11_fraction_ci95",
        "detection_limit_95",
        "critical_fraction",
    }
)


def _is_mutation_check_field(field: str) -> bool:
    root = field.split(".", 1)[0]
    return root in MUTATION_CHECK_FIELDS


class EligibilityResult(Schema):
    """One per criterion. Safety rule 4: always cites field + criterion text.

    `status` answers "does the patient pass this criterion?". For an exclusion,
    `met` means the exclusion does not apply.
    """

    trial_id: TrialId
    criterion_type: Literal["inclusion", "exclusion"] | None = None
    criterion_text: NonEmptyStr
    status: EligibilityStatus
    evidence_field: NonEmptyStr
    reason: NonEmptyStr

    @model_validator(mode="after")
    def _mutation_check_never_decides(self) -> EligibilityResult:
        if self.status != "unknown" and _is_mutation_check_field(self.evidence_field):
            raise ValueError(
                f"safety rule 10: mutation check field {self.evidence_field!r} cannot "
                f"make a criterion {self.status}; use a CLIA result (tmb_clia) or mark unknown"
            )
        return self


# ---------------------------------------------------------------------------
# Pre-op screen (glioma)
# ---------------------------------------------------------------------------


class TestNeeded(Schema):
    __test__ = False  # not a pytest class

    field: Literal["idh", "codeletion_1p19q", "mgmt", "grade"]
    label: NonEmptyStr
    current_value: NonEmptyStr
    decides_trials: Annotated[int, Field(ge=0)]
    trial_ids: list[TrialId]

    @model_validator(mode="after")
    def _count_matches(self) -> TestNeeded:
        if self.decides_trials != len(set(self.trial_ids)):
            raise ValueError("decides_trials must equal the number of unique trial_ids")
        return self


class PreSurgeryTrial(Schema):
    trial_id: TrialId
    title: NonEmptyStr
    recruitment_status: RecruitmentStatus
    criterion_text: NonEmptyStr  # the matched criterion line, or "Title: ..." for a title hint
    # criteria = a criterion line matched. title_hint = only the title suggests it;
    # shown as "possible, check criteria" and still needs the doctor's answer.
    matched_on: Literal["criteria", "title_hint"] = "criteria"
    nearest_site: NearestSite | None = None
    # Set from the preop_review gate; "pending" until the doctor decides.
    doctor_action: Literal["pending", PreSurgeryAction] = "pending"
    doctor_reason: str | None = None


_DOCTOR_FIELDS = {"doctor_action", "doctor_reason"}


class PreOpScreen(Schema):
    case_id: NonEmptyStr
    # Content hash, set on creation. Changes when any flag, status, criterion or
    # test changes, so a review of an older screen is rejected.
    screen_id: str = ""
    trials_considered: Annotated[int, Field(ge=0)]
    tests_needed: list[TestNeeded]
    pre_surgery_trials: list[PreSurgeryTrial] = Field(default_factory=list)
    label: NonEmptyStr

    @model_validator(mode="after")
    def _set_screen_id(self) -> PreOpScreen:
        if not self.screen_id:
            content = {
                "tests_needed": [t.model_dump(mode="json") for t in self.tests_needed],
                "pre_surgery_trials": [
                    t.model_dump(mode="json", exclude=_DOCTOR_FIELDS) for t in self.pre_surgery_trials
                ],
                "label": self.label,
            }
            digest = hashlib.sha256(json.dumps(content, sort_keys=True).encode()).hexdigest()
            self.screen_id = f"preop-{digest[:12]}"
        return self


# ---------------------------------------------------------------------------
# Mutation check
# ---------------------------------------------------------------------------


class Snv(Schema):
    chrom: NonEmptyStr
    pos: Annotated[int, Field(ge=1)]
    ref: Base
    alt: Base
    context: Annotated[str, StringConstraints(pattern=r"^[ACGT]{3}$")]

    @model_validator(mode="after")
    def _context_matches(self) -> Snv:
        if self.ref == self.alt:
            raise ValueError("ref and alt must differ")
        if self.context[1] != self.ref:
            raise ValueError("context middle base must equal ref")
        return self


Count = Annotated[int, Field(ge=0, strict=True)]


class MutationCheckInput(Schema):
    """Exactly one of `snvs` (real use) or `counts_96` (stored demo catalogs, issue #6).

    Serialize with `exclude_none=True`: the mutation check rejects a request
    that carries both keys, even if one is null.
    """

    case_id: NonEmptyStr
    sequencing: Sequencing
    reference: NonEmptyStr
    snvs: list[Snv] | None = None
    # 96 SBS bins in COSMIC row order (5' base, substitution, 3' base).
    counts_96: Annotated[list[Count], Field(min_length=96, max_length=96)] | None = None

    @model_validator(mode="after")
    def _exactly_one_source(self) -> MutationCheckInput:
        if (self.snvs is None) == (self.counts_96 is None):
            raise ValueError("send exactly one of snvs or counts_96")
        return self


class MutationCheckRan(Schema):
    """Context only. Never decides eligibility (safety rule 10).

    `total_mutations` counts SNVs in a research catalog. It is not TMB, never
    fills `tmb_clia`, and never satisfies a TMB criterion.

    The four optional fields (issue #6) say how much a call can be trusted:
    `not_detected` alone can't tell "no SBS11" from "too few mutations to see it".
    """

    case_id: NonEmptyStr
    ran: Literal[True]
    total_mutations: Annotated[int, Field(ge=0)]
    sbs11_call: Literal["detected", "not_detected", "inconclusive"]
    false_alarm_rate: Unit
    exposures: dict[str, Unit]
    note: str = "Context only. Does not decide eligibility."
    # 95% interval on the fraction of mutations from SBS11, [low, high].
    sbs11_fraction_ci95: tuple[Unit, Unit] | None = None
    # SBS11 fraction detected 95% of the time at this count. null = not established: 95% is never
    # reached even at 100% SBS11, or the count is below the calibrated range (sigdetect: N < 10).
    detection_limit_95: Unit | None = None
    # SBS11 fraction detected 50% of the time at this count. Above 0.5 the call is inconclusive.
    critical_fraction: Unit | None = None
    # Plain-English sentence from a fixed template; every number comes from the fit.
    summary: NonEmptyStr | None = None

    @model_validator(mode="after")
    def _limits_consistent(self) -> MutationCheckRan:
        ci = self.sbs11_fraction_ci95
        if ci is not None and ci[0] > ci[1]:
            raise ValueError("sbs11_fraction_ci95 low must not exceed high")
        crit, lim = self.critical_fraction, self.detection_limit_95
        if crit is not None and lim is not None and crit > lim:
            raise ValueError("critical_fraction (50% detection) must not exceed detection_limit_95")
        # Too few mutations to see SBS11 is not evidence of absence.
        if crit is not None and crit > 0.5 and self.sbs11_call == "not_detected":
            raise ValueError("critical_fraction above 0.5 means the call must be inconclusive, not not_detected")
        return self


class MutationCheckSkipped(Schema):
    case_id: NonEmptyStr
    ran: Literal[False]
    skip_reason: Literal["no_broad_sequencing", "not_recurrent_glioma"]


MutationCheckOutput = Annotated[
    Union[MutationCheckRan, MutationCheckSkipped], Field(discriminator="ran")
]
MutationCheckOutputAdapter: TypeAdapter[MutationCheckOutput] = TypeAdapter(MutationCheckOutput)


# ---------------------------------------------------------------------------
# Shortlist -> frontend
# ---------------------------------------------------------------------------


class EligibilitySummary(Schema):
    met: Annotated[int, Field(ge=0)]
    not_met: Annotated[int, Field(ge=0)]
    unknown: Annotated[int, Field(ge=0)]


ShortlistGroup = Literal["eligible", "needs_test", "needs_review"]
SHORTLIST_GROUP_ORDER: dict[str, int] = {"eligible": 0, "needs_test": 1, "needs_review": 2}


class ShortlistTrial(Schema):
    trial_id: TrialId
    title: NonEmptyStr
    recruitment_status: RecruitmentStatus
    status_label: NonEmptyStr
    remote_participation: bool
    nearest_site: NearestSite | None = None  # None: remote-only, or no site coordinates listed
    summary: EligibilitySummary
    eligibility: list[EligibilityResult]
    mutation_context: MutationCheckRan | None = None
    doctor_decision: Literal["pending", "keep", "reject"] = "pending"
    # eligible: open questions are routine screening-visit items.
    # needs_test: an unknown on a key field (trial_search.key_unknown_fields);
    # needs_test_fields names the result(s) that would decide it.
    # needs_review: no clinical criterion confirmed automatically (only
    # demographics such as age); the doctor must read the criteria.
    group: ShortlistGroup = "eligible"
    needs_test_fields: list[NonEmptyStr] = []

    @model_validator(mode="after")
    def _consistent(self, info: ValidationInfo) -> ShortlistTrial:
        labels = (info.context or {}).get("status_labels")
        if labels is not None:
            expected = labels.get(self.recruitment_status)
            if expected is None:
                raise ValueError(f"policy has no status_label for {self.recruitment_status}")
            if self.status_label != expected:
                raise ValueError(
                    f"status_label for {self.recruitment_status} must be {expected!r}"
                )
        if any(e.trial_id != self.trial_id for e in self.eligibility):
            raise ValueError("every eligibility row must belong to this trial")
        counts = {s: 0 for s in ("met", "not_met", "unknown")}
        for e in self.eligibility:
            counts[e.status] += 1
        if counts != self.summary.model_dump():
            raise ValueError(f"summary {self.summary.model_dump()} does not match rows {counts}")
        if counts["not_met"]:
            raise ValueError("a shortlist trial cannot have any not_met criterion")
        if (self.group == "needs_test") != bool(self.needs_test_fields):
            raise ValueError("needs_test_fields must be set exactly when group is needs_test")
        unknown_fields = {e.evidence_field for e in self.eligibility if e.status == "unknown"}
        missing = [f for f in self.needs_test_fields if f not in unknown_fields]
        if missing:
            raise ValueError(f"needs_test_fields {missing} have no unknown eligibility row")
        return self


class Origin(Schema):
    zip: Zip
    lat: Annotated[float, Field(ge=-90, le=90)]
    lon: Annotated[float, Field(ge=-180, le=180)]


def distance_sort_key(site: NearestSite | None) -> tuple[bool, float]:
    """Nearest first; trials with no known distance (e.g. remote-only) last."""
    return (site is None, site.distance_miles if site else 0.0)


class Shortlist(Schema):
    """Every eligible trial, no distance cutoff: "eligible" group first, then
    "needs_test", then "needs_review", each sorted nearest first."""

    case_id: NonEmptyStr
    origin: Origin
    statuses_searched: list[RecruitmentStatus]
    trials: list[ShortlistTrial]

    @model_validator(mode="after")
    def _nearest_first(self) -> Shortlist:
        keys = [(SHORTLIST_GROUP_ORDER[t.group], distance_sort_key(t.nearest_site))
                for t in self.trials]
        if keys != sorted(keys):
            raise ValueError("shortlist trials must be grouped (eligible, needs_test, needs_review) and "
                             "sorted nearest first within each group, unknown distance last")
        return self


# ---------------------------------------------------------------------------
# No-match plan -> frontend
# ---------------------------------------------------------------------------


class SearchSummary(Schema):
    statuses: list[RecruitmentStatus]
    match_count: Literal[0]


class NearMiss(Schema):
    trial_id: TrialId
    title: NonEmptyStr
    near_miss_type: Literal["fails_one_rule", "pending_result"]
    blocking: Annotated[list[EligibilityResult], Field(min_length=1)]
    nearest_site: NearestSite | None = None

    @model_validator(mode="after")
    def _blocking_matches_type(self) -> NearMiss:
        statuses = [b.status for b in self.blocking]
        if any(b.trial_id != self.trial_id for b in self.blocking):
            raise ValueError("every blocking row must belong to this trial")
        if "met" in statuses:
            raise ValueError("blocking rows cannot be met")
        if self.near_miss_type == "fails_one_rule" and statuses.count("not_met") != 1:
            raise ValueError("fails_one_rule needs exactly one not_met blocking row")
        if self.near_miss_type == "pending_result" and "not_met" in statuses:
            raise ValueError("pending_result cannot have a not_met blocking row")
        return self


class AlertQuery(Schema):
    bucket: Bucket
    markers: dict[str, str | int] = Field(default_factory=dict)
    zip: Zip


class TrialAlert(Schema):
    alert_id: NonEmptyStr
    query: AlertQuery
    notify: Literal["care_team_in_app"]  # safety rule 5: never the patient
    created_at: Timestamp


class TumorBoardEntry(Schema):
    ts: Timestamp
    case_id: NonEmptyStr
    entry: NonEmptyStr
    searched_at: Timestamp


class NoMatchPlan(Schema):
    case_id: NonEmptyStr
    searched: SearchSummary
    near_misses: list[NearMiss] = Field(default_factory=list)
    alert: TrialAlert | None = None
    tumor_board_log: TumorBoardEntry | None = None

    @model_validator(mode="after")
    def _consistent(self) -> NoMatchPlan:
        if self.tumor_board_log and self.tumor_board_log.case_id != self.case_id:
            raise ValueError("tumor_board_log case_id must match")
        return self


# ---------------------------------------------------------------------------
# Audit log
# ---------------------------------------------------------------------------


class AuditEntry(Schema):
    ts: Timestamp
    case_id: str | None = None  # None for system events (e.g. policy reload)
    actor: Literal["agent", "doctor", "system"]  # system = startup, policy reload
    action: NonEmptyStr
    detail: dict[str, Any] = Field(default_factory=dict)
