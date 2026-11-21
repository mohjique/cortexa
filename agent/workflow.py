"""Case state machine (F1) and doctor decision gates (F3).

The spec is docs/decision-tree.md. All branch rules live in `next_stage`, and
every gate check lives in `submit_decision`, in code, never in the LLM
(safety rule 2). A case leaves a doctor stage only through a recorded
decision for that stage's gate.

Agent stages (pre-op screen, mutation check, matching) run through `AgentHooks`.
The pre-op screen is the one agent stage that also has a doctor gate: the
agent runs it (waiting_on=agent), then the case stays at preop_screen with
waiting_on=doctor until `preop_review` is recorded. Trials that need
enrollment before surgery are lost once the case moves on, so no flagged
trial can be skipped. While waiting, the doctor may ask for a fresh screen
(`rescreen_preop`), for example after a trial's status changes.

Defaults: the mutation check is the real check (`agent.mutation`), the pre-op
screen is the real pre-surgery detector (`agent.preop`), and matching is the
real matcher (`agent.matching`). The stubs stay as test helpers.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal, get_args

from agent.audit import audit
from agent.matching import match_case
from agent.mutation import mutation_check_hook
from agent.policy import Policy, get_policy
from agent.preop import preop_screen_hook
from agent.schemas import (
    ClassifierOutput,
    ConfirmBucketRecord,
    DoctorDecision,
    EnterResultsRecord,
    Gate,
    MutationCheckOutput,
    MutationCheckRan,
    MutationCheckSkipped,
    NoMatchPlan,
    PreOpReviewRecord,
    PreOpScreen,
    ReviewShortlistRecord,
    SearchSummary,
    Shortlist,
    Stage,
    SurgeryOrMedsRecord,
    WatchOrTreatRecord,
)
from agent.store import CaseRecord, get_store

log = logging.getLogger(__name__)
_lock = threading.RLock()  # one case change at a time

WaitingOn = Literal["doctor", "agent", "pathology"]

ALL_GATES: frozenset[str] = frozenset(get_args(Gate))

# Which gate a doctor may submit at each stage. Stages not listed take no gate.
GATE_AT_STAGE: dict[str, Gate] = {
    "confirm_bucket": "confirm_bucket",
    "watch_or_treat": "watch_or_treat",
    "preop_screen": "preop_review",  # after the agent runs the screen
    "hormone_labs": "hormone_labs",
    "surgery_or_meds": "surgery_or_meds",
    "awaiting_pathology": "enter_results",  # results arrive from the lab
    "enter_results": "enter_results",  # medication-only pituitary: no lab
    "review_shortlist": "review_shortlist",
}

WAITING_ON: dict[str, WaitingOn] = {
    "intake": "agent",
    "confirm_bucket": "doctor",
    "watch_or_treat": "doctor",
    "preop_screen": "agent",  # set to doctor once the screen is done
    "hormone_labs": "doctor",
    "surgery_or_meds": "doctor",
    "awaiting_pathology": "pathology",
    "enter_results": "doctor",
    "mutation_check": "agent",
    "matching": "agent",
    "review_shortlist": "doctor",
    "no_match": "agent",  # alerts keep watching for new trials
    "rescan": "doctor",
    "stopped": "doctor",
}
assert set(WAITING_ON) == set(get_args(Stage)), "every stage needs a waiting_on"
assert set(GATE_AT_STAGE.values()) == ALL_GATES, "every gate needs a stage"

AGENT_STAGES: frozenset[str] = frozenset({"preop_screen", "mutation_check", "matching"})
# Agent stages that end in a doctor gate instead of moving on by themselves.
GATED_AGENT_STAGES: frozenset[str] = frozenset({"preop_screen"})
FINAL_STAGES: frozenset[str] = frozenset({"stopped", "rescan", "no_match"})

_BUCKET_NEXT: dict[str, Stage] = {
    "no_tumor": "stopped",
    "meningioma": "watch_or_treat",
    "glioma": "preop_screen",
    "pituitary": "hormone_labs",
}


class WorkflowError(Exception):
    """Base error for the state machine."""


class WrongGate(WorkflowError):
    """The gate does not belong to the case's current stage."""


class InvalidDecision(WorkflowError):
    """The decision fits the stage but conflicts with the case."""


# ---------------------------------------------------------------------------
# Branch rules
# ---------------------------------------------------------------------------


SkipReason = Literal["no_broad_sequencing", "not_recurrent_glioma"]


def needs_mutation_check(rec: CaseRecord, policy: Policy) -> SkipReason | None:
    """None if the mutation check should run, else the skip reason."""
    r = rec.results
    if r is None or r.confirmed_bucket != "glioma" or not r.recurrent:
        return "not_recurrent_glioma"
    if policy.mutation_check.require_broad_sequencing and not r.broad_sequencing_available:
        return "no_broad_sequencing"
    return None


def next_stage(rec: CaseRecord, policy: Policy) -> Stage:
    """The stage after the current one. Pure: reads the record, changes nothing."""
    st = rec.state
    stage = st.stage
    if stage == "intake":
        if rec.classifier is None:
            raise WorkflowError("intake needs a classifier result first")
        return "confirm_bucket"
    if stage == "confirm_bucket":
        if st.confirmed_bucket is None:
            raise WorkflowError("confirm_bucket needs a confirmed bucket")
        return _BUCKET_NEXT[st.confirmed_bucket]
    if stage == "watch_or_treat":
        return "rescan" if st.treatment_path == "watch" else "awaiting_pathology"
    if stage == "preop_screen":
        if rec.preop is None:
            raise WorkflowError("preop_screen needs the screen result first")
        return "awaiting_pathology"
    if stage == "hormone_labs":
        return "surgery_or_meds"
    if stage == "surgery_or_meds":
        return "enter_results" if st.treatment_path == "medication_only" else "awaiting_pathology"
    if stage in ("awaiting_pathology", "enter_results"):
        if rec.results is None:
            raise WorkflowError(f"{stage} needs doctor-entered results")
        return "mutation_check" if needs_mutation_check(rec, policy) is None else "matching"
    if stage == "mutation_check":
        return "matching"
    if stage == "matching":
        return "review_shortlist" if rec.shortlist and rec.shortlist.trials else "no_match"
    if stage == "review_shortlist":
        return "stopped"
    raise WorkflowError(f"{stage} is a final stage")


def _move(rec: CaseRecord, policy: Policy) -> None:
    old = rec.state.stage
    new = next_stage(rec, policy)
    rec.state.stage = new
    rec.state.waiting_on = WAITING_ON[new]
    audit("agent", "stage_changed", {"from": old, "to": new}, case_id=rec.case_id)


# ---------------------------------------------------------------------------
# Agent stage hooks. The stubs stay as test helpers.
# ---------------------------------------------------------------------------


def stub_preop_screen(rec: CaseRecord, policy: Policy) -> PreOpScreen:
    # Test helper: a screen that flags nothing.
    return PreOpScreen(
        case_id=rec.case_id,
        trials_considered=0,
        tests_needed=[],
        label=f"{policy.classifier.label_prefix} glioma. Biopsy results pending.",
    )


def stub_mutation_check(rec: CaseRecord, policy: Policy) -> MutationCheckRan:
    # Test helper. Inconclusive = no information, so matching is unchanged (decision tree rule).
    return MutationCheckRan(
        case_id=rec.case_id,
        ran=True,
        total_mutations=0,
        sbs11_call="inconclusive",
        false_alarm_rate=policy.mutation_check.false_alarm_rate,
        exposures={},
        note="Stub: mutation check not connected yet. Context only. Does not decide eligibility.",
    )


def stub_matching(rec: CaseRecord, policy: Policy) -> Shortlist | NoMatchPlan:
    # Test helper: reports no match without searching.
    return NoMatchPlan(
        case_id=rec.case_id,
        searched=SearchSummary(
            statuses=list(policy.trial_search.statuses),
            match_count=0,
        ),
    )


@dataclass
class AgentHooks:
    preop_screen: Callable[[CaseRecord, Policy], PreOpScreen] = preop_screen_hook
    # None = no trustworthy answer; the hook audits why and matching runs without it.
    mutation_check: Callable[[CaseRecord, Policy], MutationCheckOutput | None] = mutation_check_hook
    matching: Callable[[CaseRecord, Policy], Shortlist | NoMatchPlan] = match_case


_hooks = AgentHooks()


def get_hooks() -> AgentHooks:
    return _hooks


def set_hooks(hooks: AgentHooks) -> None:
    """Swap agent hooks (tests, real agent wiring)."""
    global _hooks
    _hooks = hooks


def _attach_mutation_context(shortlist: Shortlist, mutation: MutationCheckOutput | None) -> None:
    """Copy the case's mutation result onto each trial. Context only (rule 10).

    Set here, not by the matching hook, so every trial shows the same result
    the case actually has, and none when the check didn't run.
    """
    ctx = mutation if isinstance(mutation, MutationCheckRan) else None
    for t in shortlist.trials:
        t.mutation_context = ctx.model_copy() if ctx else None


def _run_agent_stage(rec: CaseRecord, policy: Policy) -> None:
    stage = rec.state.stage
    hooks = _hooks
    try:
        if stage == "preop_screen":
            rec.preop = hooks.preop_screen(rec, policy)
            detail = {"tests_needed": len(rec.preop.tests_needed)}
        elif stage == "mutation_check":
            out = hooks.mutation_check(rec, policy)
            if isinstance(out, MutationCheckRan):
                detail = {"sbs11_call": out.sbs11_call, "total_mutations": out.total_mutations}
            elif isinstance(out, MutationCheckSkipped):
                detail = {"skip_reason": out.skip_reason}
            elif out is None:
                detail = {"result": "unavailable"}  # treated as no information
            else:
                raise WorkflowError(
                    "mutation_check hook must return MutationCheckRan, MutationCheckSkipped or None"
                )
            rec.mutation = out
        elif stage == "matching":
            out = hooks.matching(rec, policy)
            if isinstance(out, NoMatchPlan):
                rec.shortlist, rec.no_match = None, out
                detail = {"trials": 0}
            elif not out.trials:
                # No trials means the no-match plan must run; an empty shortlist would skip it.
                raise WorkflowError("matching hook returned an empty Shortlist; return a NoMatchPlan")
            else:
                _attach_mutation_context(out, rec.mutation)
                rec.shortlist, rec.no_match = out, None
                detail = {"trials": len(out.trials)}
        else:
            raise WorkflowError(f"{stage} is not an agent stage")
    except Exception as exc:
        audit(
            "agent",
            f"stage_failed:{stage}",
            {"error": f"{type(exc).__name__}: {exc}"},
            case_id=rec.case_id,
        )
        raise
    audit("agent", f"stage_done:{stage}", detail, case_id=rec.case_id)


def _run_agent_after_save(case_id: str) -> CaseRecord:
    """Run agent stages after a doctor action that is already saved.

    A failed hook must not look like a failed decision: the decision stands,
    the case stays at the agent stage (waiting_on=agent, `stage_failed:*`
    audited), and the client retries with POST /cases/{id}/run.
    """
    try:
        return run_agent(case_id)
    except Exception:
        log.exception("agent stage failed after a saved doctor action; case %s waits on agent", case_id)
        return get_store().get(case_id)


def run_agent(case_id: str) -> CaseRecord:
    """Run agent stages until the case needs a doctor, the lab, or is final.

    Saves after every stage, so a failed hook leaves the case at that stage
    (waiting_on=agent) and a later call retries it.
    """
    store = get_store()
    with _lock:
        rec = store.get(case_id)
        while rec.state.stage in AGENT_STAGES and rec.state.waiting_on == "agent":
            policy = get_policy()
            _run_agent_stage(rec, policy)
            if rec.state.stage in GATED_AGENT_STAGES:
                rec.state.waiting_on = "doctor"  # stay here until the doctor's gate
                audit(
                    "agent",
                    "waiting_on_doctor",
                    {"stage": rec.state.stage, "gate": GATE_AT_STAGE[rec.state.stage]},
                    case_id=rec.case_id,
                )
            else:
                _move(rec, policy)
            rec = store.save(rec)
        return rec


# ---------------------------------------------------------------------------
# Intake
# ---------------------------------------------------------------------------


def attach_classification(case_id: str, output: ClassifierOutput) -> CaseRecord:
    """Store the classifier result and move intake -> confirm_bucket."""
    store = get_store()
    with _lock:
        rec = store.get(case_id)
        if rec.state.stage != "intake":
            raise WrongGate(f"case is at {rec.state.stage}, not intake")
        if output.case_id != case_id:
            raise InvalidDecision("classifier case_id does not match the case")
        policy = get_policy()
        flagged, reasons = uncertainty_flag(output, policy)
        output = output.model_copy(update={"flagged_uncertain": flagged})
        rec.classifier = output
        rec.state.suspected_bucket = output.suspected_bucket
        audit(
            "agent",
            "classification_attached",
            {"suspected_bucket": output.suspected_bucket, "confidence": output.confidence,
             "flagged_uncertain": flagged, "flag_reasons": reasons},
            case_id=case_id,
        )
        _move(rec, policy)
        return store.save(rec)


def uncertainty_flag(output: ClassifierOutput, policy: Policy) -> tuple[bool, list[str]]:
    """F2.4: `flagged_uncertain` from policy.yaml, plus any flag the classifier set.

    The agent can add a flag but never clears one (the stub flags its
    arbitrary hash picks). The flag only warns the doctor; every case still
    goes through confirm_bucket (rule 2).
    """
    c = policy.classifier
    reasons = []
    if output.flagged_uncertain:
        reasons.append("classifier_flag")
    if output.confidence < c.min_confidence_for_auto_suggest:
        reasons.append(f"confidence {output.confidence:.3f} < {c.min_confidence_for_auto_suggest}")
    if (c.flag_on_model_disagreement and output.medgemma_bucket is not None
            and output.medgemma_bucket != output.suspected_bucket):
        reasons.append("model_disagreement")
    return bool(reasons), reasons


# ---------------------------------------------------------------------------
# Doctor decision gates
# ---------------------------------------------------------------------------

def _check(rec: CaseRecord, d: DoctorDecision) -> DoctorDecision:
    """Raise if the decision can't apply now. Returns it, normalized."""
    expected = GATE_AT_STAGE.get(rec.state.stage)
    if expected != d.gate:
        want = expected or "no doctor gate"
        raise WrongGate(f"case is at {rec.state.stage} (expects {want}), got {d.gate}")

    if rec.state.waiting_on == "agent":
        # e.g. preop_screen before the agent has finished the screen
        raise WrongGate(f"case is at {rec.state.stage} and the agent has not finished it")

    if isinstance(d, ConfirmBucketRecord):
        # Server decides overrode_model; the client's value is not trusted.
        overrode = (
            rec.state.suspected_bucket is not None
            and d.decision.bucket != rec.state.suspected_bucket
        )
        d = d.model_copy(
            update={"decision": d.decision.model_copy(update={"overrode_model": overrode})}
        )

    elif isinstance(d, EnterResultsRecord):
        r = d.decision
        if r.confirmed_bucket != rec.state.confirmed_bucket:
            raise InvalidDecision(
                f"results say {r.confirmed_bucket}, case is confirmed {rec.state.confirmed_bucket}"
            )
        if r.pituitary and r.pituitary.treatment_path != rec.state.treatment_path:
            raise InvalidDecision(
                f"results treatment_path {r.pituitary.treatment_path} does not match "
                f"case treatment_path {rec.state.treatment_path}"
            )

    elif isinstance(d, PreOpReviewRecord):
        if rec.preop is None:
            raise InvalidDecision("no pre-op screen to review")
        if d.decision.screen_id != rec.preop.screen_id:
            raise InvalidDecision(
                f"the pre-op screen changed since it was reviewed ({d.decision.screen_id} -> "
                f"{rec.preop.screen_id}); reload it and review again"
            )
        flagged = {t.trial_id for t in rec.preop.pre_surgery_trials}
        acked = {a.trial_id for a in d.decision.pre_surgery_trials}
        if acked - flagged:
            raise InvalidDecision(f"not a pre-surgery trial on the screen: {sorted(acked - flagged)}")
        if flagged - acked:
            raise InvalidDecision(
                f"every pre-surgery trial needs pursue_before_surgery or not_pursuing; "
                f"missing {sorted(flagged - acked)}"
            )

    elif isinstance(d, ReviewShortlistRecord):
        if rec.shortlist is None:
            raise InvalidDecision("no shortlist to review")
        on_list = {t.trial_id for t in rec.shortlist.trials}
        decided = set(d.decision.keep) | {r.trial_id for r in d.decision.reject}
        if decided - on_list:
            raise InvalidDecision(f"not on the shortlist: {sorted(decided - on_list)}")
        if on_list - decided:
            raise InvalidDecision(f"every trial needs keep or reject; missing {sorted(on_list - decided)}")

    return d


def _apply(rec: CaseRecord, d: DoctorDecision) -> None:
    if isinstance(d, ConfirmBucketRecord):
        rec.state.confirmed_bucket = d.decision.bucket
    elif isinstance(d, WatchOrTreatRecord):
        rec.state.treatment_path = "watch" if d.decision.choice == "watch" else "surgery"
    elif isinstance(d, SurgeryOrMedsRecord):
        rec.state.treatment_path = d.decision.choice
    elif isinstance(d, EnterResultsRecord):
        rec.results = d.decision
        skip = needs_mutation_check(rec, get_policy())
        if skip is not None:
            rec.mutation = MutationCheckSkipped(case_id=rec.case_id, ran=False, skip_reason=skip)
            audit("agent", "mutation_check_skipped", {"skip_reason": skip}, case_id=rec.case_id)
    elif isinstance(d, PreOpReviewRecord):
        assert rec.preop is not None
        acks = {a.trial_id: a for a in d.decision.pre_surgery_trials}
        for t in rec.preop.pre_surgery_trials:
            t.doctor_action = acks[t.trial_id].action
            t.doctor_reason = acks[t.trial_id].reason
        # tests_reviewed=true means "reviewed what the screen listed"; record how much that was.
        audit(
            "doctor",
            "preop_review_recorded",
            {
                "screen_id": rec.preop.screen_id,
                "tests_needed_listed": len(rec.preop.tests_needed),
                "pre_surgery_trials": {t.trial_id: t.doctor_action for t in rec.preop.pre_surgery_trials},
            },
            case_id=rec.case_id,
        )
    elif isinstance(d, ReviewShortlistRecord):
        assert rec.shortlist is not None
        keep = set(d.decision.keep)
        for t in rec.shortlist.trials:
            t.doctor_decision = "keep" if t.trial_id in keep else "reject"


def submit_decision(decision: DoctorDecision) -> CaseRecord:
    """Record a doctor decision, advance the case, then run any agent stages.

    If an agent stage fails afterwards, the saved state is returned with
    waiting_on=agent (retry via run_agent); the decision is not undone.
    Raises WrongGate if the gate doesn't match the stage, InvalidDecision if
    it conflicts with the case. Both are audited; the case is unchanged.
    """
    store = get_store()
    with _lock:
        rec = store.get(decision.case_id)
        try:
            decision = _check(rec, decision)
        except WorkflowError as exc:
            audit(
                "doctor",
                f"decision_rejected:{decision.gate}",
                {"doctor_id": decision.doctor_id, "stage": rec.state.stage, "error": str(exc)},
                case_id=decision.case_id,
            )
            raise
        rec = store.append_decision(decision)  # saves + audits (rule 7)
        _apply(rec, decision)
        _move(rec, get_policy())
        store.save(rec)
        return _run_agent_after_save(decision.case_id)


def rescreen_preop(case_id: str, doctor_id: str, reason: str | None = None) -> CaseRecord:
    """Doctor asks for a fresh pre-op screen before reviewing it.

    Only while the case waits on the preop_review gate. The old screen is
    dropped (its flags stay in the audit log) and the agent runs again, so a
    trial whose status changed, or a screen made during an API outage, is
    not stuck as the only option.
    """
    store = get_store()
    with _lock:
        rec = store.get(case_id)
        if rec.state.stage != "preop_screen" or rec.state.waiting_on != "doctor":
            raise WrongGate(
                f"rescreen only while preop_screen waits on a doctor; case is at "
                f"{rec.state.stage} waiting on {rec.state.waiting_on}"
            )
        old = [t.trial_id for t in rec.preop.pre_surgery_trials] if rec.preop else []
        audit(
            "doctor",
            "preop_rescreen_requested",
            {"doctor_id": doctor_id, "reason": reason, "previous_flags": old},
            case_id=case_id,
        )
        rec.preop = None
        rec.state.waiting_on = "agent"
        store.save(rec)
        return _run_agent_after_save(case_id)

