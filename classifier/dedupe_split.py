"""Dedupe the MRI images, then split them by duplicate cluster (F2.1, issue #22).

    python -m classifier.dedupe_split hash      # hash every image (cached)
    python -m classifier.dedupe_split report    # distance histogram + pair sheets to check by eye
    python -m classifier.dedupe_split split     # write classifier/splits/split.csv

Why: the Kaggle Training and Testing folders share exact and near-duplicate
images, so a model scored on them is partly scored on images it trained on.

1. Every clean image (Training + Testing) gets an MD5, a 64-bit pHash and
   dHash, and a 32x32 grayscale thumbnail. Two images are duplicates when the
   bytes match, or when pHash is close (candidate) AND the thumbnails' pixel
   correlation confirms it. Hashes alone are not enough here: many different
   patients' dark axial T1 post-contrast slices hash within a few bits of each
   other and chained into mixed-class clusters.
2. Duplicates are joined into clusters (union-find). A cluster that holds more
   than one class is dropped: we can't tell which label is right.
3. We ignore the Kaggle folders and split whole clusters, stratified by class:
   demo_pool (issue #22) / test / calib / train. A cluster never straddles two
   splits, so no image in demo_pool, test or calib has a copy in train.
4. Each blurred / noisy / motion copy is matched back to its clean source and
   follows its split: copies of train / calib images join train / calib,
   copies of test images become the held-out robust_<kind> check, copies of
   demo_pool images are excluded.

The CSV holds file names, labels and hashes only, never pixels, so it is
committed and Colab trains on exactly the same split.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import random
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np
from PIL import Image

from classifier.dataset import BUCKETS, CLEAN_ORIGINS, DATA_ROOT, SPLIT_CSV, VARIANT_ORIGINS, find_images

HASH_CACHE = DATA_ROOT / "hashes.csv"  # gitignored with data/
SEED = 2026_11_21

THUMB_CACHE = DATA_ROOT / "thumbs32.npz"

# Near-duplicate rule, set by eye from the `report` pair sheets:
# - candidates: pHash distance <= 10 bits (of 64; always even, pHash sets 32 bits)
# - confirmed: 32x32 thumbnail correlation >= 0.975. True duplicates sit at
#   0.99-1.0; the highest different-patient pair seen (checked by eye) was 0.967.
CANDIDATE_PHASH_MAX = 10
PIXEL_CORR_MIN = 0.975
THUMB = 32
# A blurred / noisy / motion copy matches its source at this correlation, and
# only when no other cluster comes within VARIANT_MARGIN of it.
VARIANT_CORR_MIN = 0.95
VARIANT_MARGIN = 0.01
# A copy follows its source's split, so it never leaks across splits:
# - train / calib: extra degraded examples (calib gets harder, so the fitted
#   temperature stops over-trusting the model on clean, easy scans)
# - test: held-out robustness check, never trained on
# - demo_pool: kept out entirely
VARIANT_SPLIT = {
    "train": "train",
    "calib": "calib",
    "test": "robust_{kind}",
    "demo_pool": "excluded_demo_pool",
    "dropped_label_conflict": "excluded_label_conflict",
}

# Demo pool sizes (issue #22 needs 12 / 5 / 3 / 3; extra glioma so demo-023,
# a glioma the model gets wrong or is unsure about, can be found after training).
DEMO_POOL = {"glioma": 40, "meningioma": 8, "no_tumor": 6, "pituitary": 6}
TEST_FRAC = 0.15
CALIB_FRAC = 0.10

FIELDS = ["path", "label", "origin", "split", "cluster", "md5", "phash", "dhash", "source", "match_corr"]


# ---------------------------------------------------------------------------
# Hashing
# ---------------------------------------------------------------------------


def _hashes(path: Path) -> tuple[str, int, int]:
    import imagehash  # only needed here

    raw = path.read_bytes()
    with Image.open(io.BytesIO(raw)) as im:  # decode the bytes already read
        g = im.convert("L")
        ph = int(str(imagehash.phash(g)), 16)
        dh = int(str(imagehash.dhash(g)), 16)
    return hashlib.md5(raw).hexdigest(), ph, dh


def load_hashes(rebuild: bool = False) -> list[dict]:
    refs = find_images()
    cached: dict[str, dict] = {}
    if HASH_CACHE.exists() and not rebuild:
        with HASH_CACHE.open(newline="", encoding="utf-8") as f:
            cached = {r["path"]: r for r in csv.DictReader(f)}
    rows = []
    for i, ref in enumerate(refs):
        hit = cached.get(ref.path)
        if hit is None:
            md5, ph, dh = _hashes(DATA_ROOT / ref.path)
            hit = {"path": ref.path, "md5": md5, "phash": f"{ph:016x}", "dhash": f"{dh:016x}"}
        rows.append({"path": ref.path, "label": ref.label, "origin": ref.origin,
                     "md5": hit["md5"], "phash": hit["phash"], "dhash": hit["dhash"]})
        if (i + 1) % 2000 == 0:
            print(f"  hashed {i + 1}/{len(refs)}")
    with HASH_CACHE.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["path", "md5", "phash", "dhash"])
        w.writeheader()
        w.writerows({k: r[k] for k in ("path", "md5", "phash", "dhash")} for r in rows)
    return rows


def load_thumbs(rows: list[dict]) -> np.ndarray:
    """Mean-centered, unit-norm 32x32 thumbnails, one row per image (cached)."""
    paths = [r["path"] for r in rows]
    cache: dict[str, np.ndarray] = {}
    if THUMB_CACHE.exists():
        z = np.load(THUMB_CACHE, allow_pickle=False)
        cache = dict(zip(z["paths"].tolist(), z["thumbs"]))
    out = np.empty((len(paths), THUMB * THUMB), dtype=np.float32)
    added = False
    for k, path in enumerate(paths):
        v = cache.get(path)
        if v is None:
            added = True
            with Image.open(DATA_ROOT / path) as im:
                v = np.asarray(im.convert("L").resize((THUMB, THUMB), Image.BILINEAR), dtype=np.float32).ravel()
            v = v - v.mean()
            v = v / (np.linalg.norm(v) + 1e-8)
            cache[path] = v
        out[k] = v
    if added:
        np.savez(THUMB_CACHE, paths=np.array(list(cache)), thumbs=np.stack(list(cache.values())))
    return out


def _as_u64(rows: list[dict], key: str) -> np.ndarray:
    return np.array([int(r[key], 16) for r in rows], dtype=np.uint64)


_POP8 = np.array([bin(i).count("1") for i in range(256)], dtype=np.uint8)


def _popcount(x: np.ndarray) -> np.ndarray:
    """Set bits per uint64 element -> uint8 array of the same shape."""
    return _POP8[x.view(np.uint8).reshape(*x.shape, 8)].sum(axis=-1, dtype=np.uint8)


def hamming(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """Pairwise Hamming distance between two uint64 arrays -> (len(a), len(b)) uint8."""
    return _popcount(a[:, None] ^ b[None, :])


def near_pairs(ph: np.ndarray, p_max: int, dh: np.ndarray | None = None, chunk: int = 512):
    """(i, j, pdist, ddist) for i < j with pHash <= p_max. ddist is the dHash
    distance when `dh` is given (for display only), else None."""
    n = len(ph)
    for s in range(0, n, chunk):
        pd = hamming(ph[s:s + chunk], ph)
        ii, jj = np.nonzero(pd <= p_max)
        keep = s + ii < jj
        ii, jj = ii[keep], jj[keep]
        dd = None if dh is None else _popcount(dh[s + ii] ^ dh[jj])
        for k, (i, j) in enumerate(zip(ii, jj)):
            yield s + int(i), int(j), int(pd[i, j]), None if dd is None else int(dd[k])


# ---------------------------------------------------------------------------
# Clustering + split
# ---------------------------------------------------------------------------


class UnionFind:
    def __init__(self, n: int) -> None:
        self.parent = list(range(n))

    def find(self, x: int) -> int:
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[max(ra, rb)] = min(ra, rb)


def cluster_clean(clean: list[dict], thumbs: np.ndarray) -> list[int]:
    uf = UnionFind(len(clean))
    by_md5: dict[str, int] = {}
    for i, r in enumerate(clean):
        if r["md5"] in by_md5:
            uf.union(i, by_md5[r["md5"]])
        else:
            by_md5[r["md5"]] = i
    for i, j, _, _ in near_pairs(_as_u64(clean, "phash"), CANDIDATE_PHASH_MAX):
        if float(thumbs[i] @ thumbs[j]) >= PIXEL_CORR_MIN:
            uf.union(i, j)
    return [uf.find(i) for i in range(len(clean))]


def assign_splits(clean: list[dict], roots: list[int]) -> dict[int, str]:
    """Cluster root -> split. Stratified by class, seeded, whole clusters only."""
    members: dict[int, list[int]] = defaultdict(list)
    for i, root in enumerate(roots):
        members[root].append(i)
    split_of: dict[int, str] = {}
    by_class: dict[str, list[int]] = defaultdict(list)
    for root, idx in members.items():
        labels = {clean[i]["label"] for i in idx}
        if len(labels) > 1:
            split_of[root] = "dropped_label_conflict"
        else:
            by_class[labels.pop()].append(root)

    rng = random.Random(SEED)
    for label in BUCKETS:
        roots_c = sorted(by_class[label])
        rng.shuffle(roots_c)
        # Demo images come from singleton clusters: one clean, unduplicated scan each.
        singles = [r for r in roots_c if len(members[r]) == 1]
        demo = set(singles[: DEMO_POOL[label]])
        if len(demo) < DEMO_POOL[label]:
            raise SystemExit(f"not enough singleton {label} images for the demo pool")
        rest = [r for r in roots_c if r not in demo]
        total = sum(len(members[r]) for r in rest)
        test_n, calib_n, seen = TEST_FRAC * total, CALIB_FRAC * total, 0
        for r in demo:
            split_of[r] = "demo_pool"
        for r in rest:
            if seen < test_n:
                split_of[r] = "test"
            elif seen < test_n + calib_n:
                split_of[r] = "calib"
            else:
                split_of[r] = "train"
            seen += len(members[r])
    return split_of


def match_variants(variants: list[dict], vthumbs: np.ndarray, clean: list[dict],
                   cthumbs: np.ndarray, roots: list[int]) -> list[tuple[int | None, float]]:
    """For each variant, (index of its clean source or None, pixel correlation).

    A copy keeps its class folder, so only same-class sources count. The best
    match must clear VARIANT_CORR_MIN, and no image from a *different* cluster
    may come within VARIANT_MARGIN (duplicates of the source are fine).
    """
    labels = np.array([c["label"] for c in clean])
    roots_a = np.array(roots)
    out: list[tuple[int | None, float]] = []
    for s in range(0, len(variants), 512):
        corr = vthumbs[s:s + 512] @ cthumbs.T
        for k in range(corr.shape[0]):
            row = np.where(labels == variants[s + k]["label"], corr[k], -1.0)
            best = int(row.argmax())
            other = np.where(roots_a != roots_a[best], row, -1.0).max()
            ok = row[best] >= VARIANT_CORR_MIN and row[best] - other >= VARIANT_MARGIN
            out.append((best if ok else None, round(float(row[best]), 4)))
    return out


def build_split() -> list[dict]:
    rows = load_hashes()
    clean = [r for r in rows if r["origin"] in CLEAN_ORIGINS]
    variants = [r for r in rows if r["origin"] in VARIANT_ORIGINS]
    cthumbs, vthumbs = load_thumbs(clean), load_thumbs(variants)
    roots = cluster_clean(clean, cthumbs)
    split_of = assign_splits(clean, roots)
    out = []
    for r, root in zip(clean, roots):
        out.append({**r, "split": split_of[root], "cluster": root, "source": "", "match_corr": ""})
    for v, (src, corr) in zip(variants, match_variants(variants, vthumbs, clean, cthumbs, roots)):
        if src is None:
            split, source = "excluded_unmatched", ""
        else:
            source = clean[src]["path"]
            split = VARIANT_SPLIT[split_of[roots[src]]].format(kind=v["origin"])
        out.append({**v, "split": split, "cluster": "", "source": source, "match_corr": corr})
    return out


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def cmd_report(sheet_dir: Path) -> None:
    """Distance histogram among clean images + contact sheets of pairs per distance band."""
    rows = [r for r in load_hashes() if r["origin"] in CLEAN_ORIGINS]
    pairs = list(near_pairs(_as_u64(rows, "phash"), 16, _as_u64(rows, "dhash")))
    hist = Counter(p for _, _, p, _ in pairs)
    print("pHash distance -> pairs (any dHash):")
    for k in range(17):
        print(f"  {k:2d}: {hist.get(k, 0)}")
    md5_dupes = len(rows) - len({r["md5"] for r in rows})
    print(f"exact byte duplicates beyond first copy: {md5_dupes}")
    th = load_thumbs(rows)
    dup = [(i, j) for i, j, p, _ in pairs if p <= CANDIDATE_PHASH_MAX and th[i] @ th[j] >= PIXEL_CORR_MIN]
    cross = sum(1 for i, j in dup if rows[i]["origin"] != rows[j]["origin"])
    print(f"confirmed near-dup pairs: {len(dup)}, of which cross Training/Testing: {cross}")

    sheet_dir.mkdir(parents=True, exist_ok=True)
    rng = random.Random(0)
    for lo, hi in [(0, 2), (3, 4), (5, 6), (7, 8), (9, 10), (11, 12)]:
        band = [(i, j, p, d) for i, j, p, d in pairs if lo <= p <= hi]
        pick = rng.sample(band, min(8, len(band)))
        if not pick:
            continue
        tile = 160
        sheet = Image.new("L", (tile * 2, tile * len(pick)), 0)
        for k, (i, j, _, _) in enumerate(pick):
            for c, idx in enumerate((i, j)):
                with Image.open(DATA_ROOT / rows[idx]["path"]) as im:
                    sheet.paste(im.convert("L").resize((tile, tile)), (c * tile, k * tile))
        out = sheet_dir / f"pairs_phash_{lo:02d}-{hi:02d}.png"
        sheet.save(out)
        print(f"  {out}  ({len(band)} pairs in band, d-hash shown per pair: {[d for *_, d in pick]})")


def cmd_split() -> None:
    out = build_split()
    SPLIT_CSV.parent.mkdir(parents=True, exist_ok=True)
    with SPLIT_CSV.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(out)
    table = Counter((r["split"], r["label"]) for r in out)
    splits = sorted({r["split"] for r in out})
    print(f"{'split':24s}" + "".join(f"{b:>12s}" for b in BUCKETS) + f"{'total':>8s}")
    for s in splits:
        counts = [table.get((s, b), 0) for b in BUCKETS]
        print(f"{s:24s}" + "".join(f"{c:>12d}" for c in counts) + f"{sum(counts):>8d}")
    print(f"wrote {SPLIT_CSV}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["hash", "report", "split"])
    ap.add_argument("--rebuild", action="store_true", help="re-hash every image")
    ap.add_argument("--sheets", type=Path, default=Path(__file__).resolve().parents[1] / "artifacts" / "dedupe")
    a = ap.parse_args()
    if a.cmd == "hash":
        print(f"{len(load_hashes(a.rebuild))} images hashed -> {HASH_CACHE}")
    elif a.cmd == "report":
        cmd_report(a.sheets)
    else:
        cmd_split()


if __name__ == "__main__":
    main()
