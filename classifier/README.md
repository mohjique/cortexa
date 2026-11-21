# classifier/

Owner: computer engineer.

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
