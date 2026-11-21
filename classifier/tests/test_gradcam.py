"""Grad-CAM mechanics on a random-weight checkpoint (no dataset or trained model needed)."""

from __future__ import annotations

import numpy as np
import pytest
from PIL import Image, ImageDraw

from classifier import gradcam
from classifier.dataset import BUCKETS
from classifier.model import LoadedModel, build_model, save_checkpoint


@pytest.fixture(scope="module")
def model(tmp_path_factory):
    path = tmp_path_factory.mktemp("ck") / "model.pt"
    save_checkpoint(path, build_model(pretrained=False), temperature=1.0, meta={})
    return LoadedModel(path)


@pytest.fixture
def scan():
    """Synthetic 'head': bright ellipse on black, 256x256."""
    img = Image.new("L", (256, 256), 0)
    ImageDraw.Draw(img).ellipse((50, 40, 206, 216), fill=160)
    return img


def test_cam_shape_range_and_probs(model, scan):
    r = gradcam.gradcam(model, scan)
    assert r.cam.shape == (224, 224)
    assert r.cam.min() >= 0 and r.cam.max() <= 1
    assert r.target in BUCKETS
    assert abs(sum(r.probs.values()) - 1) < 1e-4


def test_cam_for_named_target(model, scan):
    assert gradcam.gradcam(model, scan, target="pituitary").target == "pituitary"


def test_shares_and_masks(scan):
    cam = np.zeros((224, 224), dtype=np.float32)
    cam[100:120, 100:120] = 1.0  # centre of the head
    s = gradcam.cam_shares(scan, cam)
    assert s["head"] == pytest.approx(1.0) and s["border"] == 0.0
    cam[:] = 0
    cam[:5, :5] = 1.0  # corner: background and border
    s = gradcam.cam_shares(scan, cam)
    assert s["background"] == pytest.approx(1.0) and s["border"] == pytest.approx(1.0)


def test_black_out_border_and_overlay(scan):
    out = gradcam.black_out_border(Image.new("L", (100, 100), 255))
    a = np.asarray(out.convert("L"))
    assert a[0, 0] == 0 and a[50, 50] == 255 and a[5, 50] == 0
    ov = gradcam.overlay(scan, np.random.rand(224, 224).astype(np.float32))
    assert ov.size == (224, 224) and ov.mode == "RGB"
