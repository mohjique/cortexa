"""Stub classifier: deterministic, schema-valid, flags review correctly."""

import pytest

from agent.schemas import BUCKETS, ClassifierOutput
from classifier.stub import StubClassifier

IMG = b"synthetic-not-an-mri"


@pytest.fixture
def stub() -> StubClassifier:
    return StubClassifier()


@pytest.mark.parametrize(
    "filename, bucket, review",
    [
        ("demo_glioma.png", "glioma", False),
        ("Meningioma_02.jpg", "meningioma", False),
        ("case4-pituitary.png", "pituitary", False),
        ("notumor_01.jpg", "no_tumor", False),
        ("case5_no_tumor.png", "no_tumor", False),
        ("glioma_lowconf.png", "glioma", True),
        ("disagree.png", "meningioma", True),
    ],
)
def test_filename_keyword(stub, filename, bucket, review):
    out = stub.classify("demo-001", IMG, filename)
    assert isinstance(out, ClassifierOutput)
    assert out.case_id == "demo-001"
    assert out.suspected_bucket == bucket
    assert out.flagged_uncertain is review


def test_hash_fallback_is_deterministic_and_always_flagged(stub):
    seen = set()
    for i in range(20):
        img = f"img-{i}".encode()
        a = stub.classify("c", img, "upload.png")
        assert a == stub.classify("c", img, None)
        assert a.suspected_bucket in BUCKETS
        # Arbitrary pick: must never look confident, even with lax policy.
        assert a.flagged_uncertain is True
        seen.add(a.suspected_bucket)
    assert len(seen) > 1  # fallback really spreads across buckets
    lax = StubClassifier(min_confidence_for_auto_suggest=0.0, flag_on_model_disagreement=False)
    assert lax.classify("c", b"x", "scan.png").flagged_uncertain is True


def test_threshold_is_configurable():
    strict = StubClassifier(min_confidence_for_auto_suggest=0.95)
    assert strict.classify("c", IMG, "glioma.png").flagged_uncertain is True
    lax = StubClassifier(flag_on_model_disagreement=False)
    assert lax.classify("c", IMG, "disagree.png").flagged_uncertain is False
