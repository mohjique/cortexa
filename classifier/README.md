# classifier/

- Download the Kaggle dataset `mohamadabouali1/mri-brain-tumor-dataset-4-class-7023-images` into `data/brain-tumor-mri/` (gitignored). Its 7,023 Training/Testing images are the same as `masoudnickparvar/brain-tumor-mri-dataset` (identical per-class counts, renamed folders), plus blurred / noisy / motion-artifact copies.
- Remove exact and near-duplicate images (perceptual hashing) before the train/val/test split.
- Fine-tune a pretrained ResNet; report per-class accuracy on the deduplicated test set.
- Grad-CAM heatmaps for every prediction; sanity-check that they highlight the tumor.
- Evaluate MedGemma alone before using it as a second opinion.
- Output format: see `docs/interfaces.md` (Classifier → agent).

## Pipeline

```
python -m classifier.dedupe_split hash     # MD5 + pHash/dHash, cached in data/
python -m classifier.dedupe_split report   # histogram + pair sheets in artifacts/dedupe/ to check by eye
python -m classifier.dedupe_split split    # writes classifier/splits/split.csv (committed)
python -m classifier.train --out artifacts/classifier   # or Colab: classifier/train_colab.ipynb
```

**Dedupe.** A pair is a duplicate when the bytes match, or pHash is within 10 bits *and* 32x32 thumbnail correlation is >= 0.975. Hashes alone chained different patients (dark axial T1 post-contrast slices hash alike) into mixed-class clusters; every cross-class pair above 0.95 correlation was a different patient when checked by eye, max 0.967. True duplicates sit at 0.99-1.0. The largest cluster is one no-tumor slice repeated 35 times at different contrast, 32 in Kaggle Training and 3 in Testing.

**Split** (by cluster, stratified, seed 20261121; a cluster never spans two splits). Each blurred / noisy / motion copy follows its source image's split.

| split | glioma | meningioma | no_tumor | pituitary | total | used for |
|---|---|---|---|---|---|---|
| train | 1780 | 1913 | 2323 | 1998 | 8014 | weights: 5215 clean + 2799 degraded copies of those same scans |
| calib | 214 | 262 | 411 | 271 | 1158 | best epoch + temperature: 697 clean + 461 copies (harder, so confidence is not fitted on easy scans only) |
| test | 238 | 246 | 303 | 264 | 1051 | reported accuracy, clean only |
| demo_pool | 40 | 8 | 6 | 6 | 60 | demo images (issue #22), never trained on or reported; their 35 copies are excluded |
| robust_blurred / noisy / motion | | | | | 264 / 264 / 87 | copies of **test** images only, never trained on |

The robust sets come from the same generator as the training copies, so they measure "this dataset's noise", not noise in general. `RandomDegrade` in `model.py` adds independently written noise, blur and motion ghosting to training images so the model is not tuned to one generator.

Demo pool images are single, unduplicated scans. Reported test accuracy excludes them.

**Model.** ResNet-18 (ImageNet weights), 224 px, class-weighted loss, temperature scaling on calib. `classifier/model.py` holds preprocessing and checkpoint loading for both training and inference. Outputs (`model.pt`, `metrics.json`, `predictions.csv`) go in `artifacts/classifier/` (gitignored).

## Results (run 2, Colab T4, 12 epochs, temperature 0.514)

Deduplicated test set, 1,051 clean images. Demo pool excluded; demo-023's image **is** in it (without it: 97.0% on 1,050).

| | glioma | meningioma | no_tumor | pituitary | overall |
|---|---|---|---|---|---|
| test accuracy | 98.7% | 91.5% | 97.7% | 99.6% | **97.0%** |
| noisy copies of test images | 100% | 93.3% | 100% | 100% | 98.5% (run 1: 61.4%) |
| blurred copies | 100% | 95.0% | 100% | 100% | 98.9% (run 1: 94.7%) |
| motion copies | 100% | 90.0% | 96.3% | 100% | 96.6% (run 1: 88.5%) |

Run 1 had no degraded training data and called 86 of 264 noisy copies of real tumors "no tumor".

**Known weaknesses (say these out loud):**
- Meningioma is the weakest class; 12 of 246 test meningiomas were called "no tumor".
- Errors are often confident: 19 of 32 test errors are at >= 0.95. Calibration has little to learn from (8 errors in 1,158 calib images).
- `flagged_uncertain` is a warning, not a safety net. At the policy threshold 0.99 (chosen on calib: flags 8/8 calib errors) it flags 20 of 32 test errors and 3.3% of correct calls; at 0.80 it flagged 6 of 32. The doctor confirms every case either way.

**Demo images** (issue #22): `classifier/splits/demo_images.json`. 22 from the demo pool, picked at random (seed 20261121) among correct calls at >= 0.99. demo-023 is a test-split glioma the model calls meningioma at 0.78 (wrong and flagged). Kaggle folder names ("Training") do not matter: none of these images or their duplicates are in our train split.

## Grad-CAM heatmaps and source-artifact check (F2.2)

```
python -m classifier.gradcam demo                    # artifacts/classifier/heatmaps/demo/<case_id>.png + index.json
python -m classifier.gradcam check --per-class 120   # artifact_check.json + montage_<class>.png
```

The "no tumor" class comes from a different source dataset (Br35H), so the model could learn the source instead of anatomy. Check on 120 clean test images per class:

| class | accuracy | border blacked out | heat on head | heat on background | heat in border band |
|---|---|---|---|---|---|
| glioma | 97.5% | 98.3% | 0.79 | 0.21 | 0.13 |
| meningioma | 90.0% | 91.7% | 0.86 | 0.14 | 0.13 |
| no_tumor | 97.5% | 97.5% | 0.74 | 0.26 | 0.28 |
| pituitary | 100% | 99.2% | 0.89 | 0.11 | 0.17 |

The border band is the outer 12% of each side, where frames and burned-in text sit.

**What it shows (montages checked by eye):**
- **No reliance on frame or border artifacts.** Blacking out the border band leaves "no tumor" accuracy unchanged.
- **Meningioma:** the heat sits on the visible tumor in most images. That is the clearest evidence the model looks at lesions.
- **No tumor:** the heat spreads over brain tissue, but many images also show a hot spot in the same lower-right position, partly off the head. This is consistent with the higher border share and may be a learned position cue; we have not proven that it is.
- **Glioma:** the heat is mostly a broad central blob, not clearly on the lesion. Many glioma images in this dataset are coronal or sagittal, while "no tumor" images are axial, so slice orientation may be a shortcut.
- **Misses look like these failure modes:** a frontal meningioma was called "no tumor" at 0.98, with heat on the back of the head.

**Bottom line:** the check rules out the obvious frame and text shortcut. It does not show that every call is driven by the lesion, which is one more reason the doctor confirms every bucket.

**Demo images:** heat on head 0.63–0.96. demo-023 (glioma called meningioma, 0.78) has its heatmap explaining the wrong call, which is useful to show judges.

## Serving the model (F2.5)

`classifier/real.py` `ResNetClassifier` puts `model.pt` behind the same `classify(case_id, image, filename)` interface as the stub. It returns calibrated probabilities and the suspected bucket, and writes a Grad-CAM overlay to `agent/data/heatmaps/<case_id>.png`. `medgemma_bucket` is `None` until MedGemma is wired in.

The API picks the classifier with `CORTEXA_CLASSIFIER=real|stub`. The default is `real` when `artifacts/classifier/model.pt` exists, otherwise `stub`; `CORTEXA_MODEL_PATH` overrides the path.
