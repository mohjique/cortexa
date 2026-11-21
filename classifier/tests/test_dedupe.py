import numpy as np
import pytest

from classifier import dedupe_split as ds
from classifier.dataset import bucket_for_folder, origin_for_path
from pathlib import PurePosixPath as P


@pytest.mark.parametrize("folder, bucket", [
    ("Glioma", "glioma"), ("No-tumor", "no_tumor"), ("notumor", "no_tumor"),
    ("Meningioma", "meningioma"), ("pituitary", "pituitary"), ("Other", None),
])
def test_bucket_for_folder(folder, bucket):
    assert bucket_for_folder(folder) == bucket


@pytest.mark.parametrize("path, origin", [
    ("BT-MRI Dataset/BT-MRI Dataset/Training/Glioma/a.jpg", "training"),
    ("BT-MRI Dataset/BT-MRI Dataset/Testing/Glioma/a.jpg", "testing"),
    ("Challenging Datasets/Challenging Datasets/Blurred Dataset/Glioma/a.jpg", "blurred"),
    ("Challenging Datasets/Challenging Datasets/Noisy Dataset/Glioma/a.jpg", "noisy"),
    ("Challenging Datasets/Challenging Datasets/Patient Motion Artifact Dataset/Glioma/a.jpg", "motion"),
])
def test_origin_for_path(path, origin):
    assert origin_for_path(P(path)) == origin


def test_hamming():
    a = np.array([0, 0xFF, 2**64 - 1], dtype=np.uint64)
    d = ds.hamming(a, a)
    assert d.tolist() == [[0, 8, 64], [8, 0, 56], [64, 56, 0]]


def _row(i, label, ph, dh=None, md5=None):
    return {"path": f"x/{i}.jpg", "label": label, "origin": "training",
            "md5": md5 or f"m{i}", "phash": f"{ph:016x}", "dhash": f"{dh if dh is not None else ph:016x}"}


def _thumbs(n, same=()):
    """Random unit vectors; pairs in `same` get (nearly) identical thumbnails."""
    rng = np.random.default_rng(1)
    t = rng.normal(size=(n, 16)).astype(np.float32)
    for a, b in same:
        t[b] = t[a] + 0.01 * rng.normal(size=16)
    return t / np.linalg.norm(t, axis=1, keepdims=True)


def test_clusters_need_close_hash_and_matching_pixels():
    rows = [
        _row(0, "glioma", 0x0),
        _row(1, "glioma", 0b11),               # 2 bits from row 0, same pixels: duplicate
        _row(2, "glioma", 0b1100),             # 2 bits from row 0, different pixels: not a duplicate
        _row(3, "glioma", 2**64 - 1),          # far hash
        _row(4, "glioma", 0x00FF00FF00FF00FF, md5="m3"),  # same bytes as row 3
    ]
    roots = ds.cluster_clean(rows, _thumbs(5, same=[(0, 1)]))
    assert roots[0] == roots[1]
    assert roots[2] != roots[0]
    assert roots[3] == roots[4]


def test_split_never_straddles_and_drops_label_conflicts(monkeypatch):
    monkeypatch.setattr(ds, "DEMO_POOL", {"glioma": 2, "meningioma": 1, "no_tumor": 1, "pituitary": 1})
    rows, k = [], 0
    rng = np.random.default_rng(0)
    for label in ("glioma", "meningioma", "no_tumor", "pituitary"):
        for _ in range(60):
            rows.append(_row(k, label, int(rng.integers(0, 2**63))))
            k += 1
    # a duplicate pair inside glioma, and one cross-class duplicate
    rows.append(_row(k, "glioma", int(rows[0]["phash"], 16))); k += 1
    rows.append(_row(k, "meningioma", int(rows[5]["phash"], 16)))
    n = len(rows)
    roots = ds.cluster_clean(rows, _thumbs(n, same=[(0, n - 2), (5, n - 1)]))
    split_of = ds.assign_splits(rows, roots)
    by_root = {}
    for r in roots:
        by_root.setdefault(r, set()).add(split_of[r])
    assert all(len(s) == 1 for s in by_root.values())
    assert split_of[roots[5]] == "dropped_label_conflict"
    splits = [split_of[r] for r in roots]
    assert splits.count("demo_pool") == 5
    assert {"train", "test", "calib"} <= set(splits)
