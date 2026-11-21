# classifier/

Owner: computer engineer.

- Download the Kaggle dataset `masoudnickparvar/brain-tumor-mri-dataset` into `data/` (gitignored).
- Remove exact and near-duplicate images (perceptual hashing) before the train/val/test split.
- Fine-tune a pretrained ResNet; report per-class accuracy on the deduplicated test set.
- Grad-CAM heatmaps for every prediction; sanity-check that they highlight the tumor.
- Evaluate MedGemma alone before using it as a second opinion.
- Output format: see `docs/interfaces.md` (Classifier → agent).
