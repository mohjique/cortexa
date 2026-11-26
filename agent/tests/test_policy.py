import os
from pathlib import Path

import pytest
import yaml

from agent.policy import POLICY_PATH, PolicyError, PolicyStore, parse_policy

REAL = POLICY_PATH.read_text(encoding="utf-8")


def edited(**changes) -> str:
    """Real policy.yaml with dotted-path edits, e.g. edited(**{"privacy.patient_contact": "sms"})."""
    data = yaml.safe_load(REAL)
    for path, value in changes.items():
        *head, last = path.split(".")
        node = data
        for key in head:
            node = node[key]
        if value is ...:
            del node[last]
        else:
            node[last] = value
    return yaml.safe_dump(data)


def write(path: Path, text: str) -> None:
    """Write and bump mtime so the change is seen even on coarse clocks."""
    path.write_text(text, encoding="utf-8")
    st = path.stat()
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))


def test_real_policy_is_valid():
    policy = parse_policy(REAL)
    assert not hasattr(policy.trial_search, "default_radius_miles")  # no distance cutoff
    assert policy.status_context()["statuses"] == ["RECRUITING", "NOT_YET_RECRUITING"]


@pytest.mark.parametrize(
    "change,match",
    [
        ({"privacy.patient_contact": "sms"}, "patient_contact"),
        ({"privacy.geocode_fields_allowed": ["zip", "street_address"]}, "safety rule 6"),
        ({"audit.log_every_tool_call": False}, "log_every_tool_call"),
        ({"audit.log_every_approval": False}, "log_every_approval"),
        ({"classifier.label_prefix": "Diagnosis"}, "label_prefix"),
        ({"classifier.label_prefix": "Confirmed"}, "label_prefix"),
        ({"classifier.label_prefix": "Definitive"}, "label_prefix"),
        ({"classifier.label_prefix": "suspected"}, "label_prefix"),
        ({"mutation_check.require_broad_sequencing": False}, "require_broad_sequencing"),
        ({"eligibility.missing_field_status": "not_met"}, "missing_field_status"),
        ({"eligibility.require_evidence_citation": False}, "require_evidence_citation"),
        ({"mutation_check.never_route_to_immunotherapy_on_mutation_count": False}, "immunotherapy"),
        ({"pre_surgery_detection.llm_confirm_on_title_hint_only": False}, "llm_confirm_on_title_hint_only"),
    ],
)
def test_locked_safety_settings_cannot_be_loosened(change, match):
    with pytest.raises(PolicyError, match=match):
        parse_policy(edited(**change))


@pytest.mark.parametrize(
    "change,match",
    [
        ({"trial_search.statuses": ["RECRUITING", "ENROLLING_BY_INVITATION"]}, "status_labels missing"),
        ({"eligibility.missing_field_reason_template": "needs pathology"}, "{field}"),
        ({"trial_search.status_label": {}}, "Extra inputs"),  # staff typo
        # No distance cutoff: a radius setting is rejected, not silently ignored.
        ({"trial_search.default_radius_miles": 100}, "Extra inputs"),
        ({"trial_search.no_match_plan.widened_radius_miles": 500}, "Extra inputs"),
        ({"privacy": ...}, "privacy"),
        ({"pre_surgery_detection.inclusion_phrases": []}, "inclusion_phrases"),
        ({"pre_surgery_detection.exclusion_phrases": [""]}, "exclusion_phrases"),
        # Rule 8: no hidden default list in code; policy.yaml is the only source.
        ({"pre_surgery_detection.inclusion_context_words": ...}, "inclusion_context_words"),
        ({"pre_surgery_detection.inclusion_context_window_words": ...}, "inclusion_context_window_words"),
        ({"pre_surgery_detection.inclusion_context_window_words": 0}, "inclusion_context_window_words"),
    ],
)
def test_bad_edits_rejected(change, match):
    with pytest.raises(PolicyError, match=match):
        parse_policy(edited(**change))


def test_not_yaml_rejected():
    with pytest.raises(PolicyError, match="not valid YAML"):
        parse_policy("trial_search: [unclosed")


def test_store_reloads_on_change(tmp_path, audit_log):
    path = tmp_path / "policy.yaml"
    write(path, REAL)
    store = PolicyStore(path)
    assert store.get() == parse_policy(REAL)

    write(path, edited(**{"classifier.min_confidence_for_auto_suggest": 0.5}))
    assert store.get().classifier.min_confidence_for_auto_suggest == 0.5
    assert [e.action for e in audit_log.read()] == ["policy_loaded", "policy_reloaded"]
    assert all(e.actor == "system" for e in audit_log.read())


def test_store_does_not_reload_unchanged_file(tmp_path, audit_log):
    path = tmp_path / "policy.yaml"
    write(path, REAL)
    store = PolicyStore(path)
    assert store.get() is store.get()
    assert len(audit_log.read()) == 1


def test_bad_edit_keeps_last_good_policy(tmp_path, audit_log):
    path = tmp_path / "policy.yaml"
    write(path, REAL)
    store = PolicyStore(path)
    good = store.get()

    write(path, edited(**{"privacy.patient_contact": "email"}))
    assert store.get() is good
    assert "patient_contact" in (store.last_error or "")
    assert audit_log.read()[-1].action == "policy_reload_failed"

    write(path, edited(**{"classifier.min_confidence_for_auto_suggest": 0.85}))
    assert store.get().classifier.min_confidence_for_auto_suggest == 0.85
    assert store.last_error is None


def test_bad_policy_at_startup_raises(tmp_path):
    path = tmp_path / "policy.yaml"
    write(path, edited(**{"audit.log_every_tool_call": False}))
    with pytest.raises(PolicyError, match="no valid policy at startup"):
        PolicyStore(path).get()


def test_missing_file_at_startup_raises(tmp_path):
    with pytest.raises(PolicyError, match="cannot read"):
        PolicyStore(tmp_path / "missing.yaml").get()


def test_file_gone_after_start_keeps_last_good(tmp_path):
    path = tmp_path / "policy.yaml"
    write(path, REAL)
    store = PolicyStore(path)
    good = store.get()
    path.unlink()
    assert store.get() is good
