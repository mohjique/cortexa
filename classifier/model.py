"""ResNet-18 MRI classifier: model, preprocessing, temperature, checkpoint I/O.

Shared by `classifier/train.py` (Colab GPU) and local inference, so both
preprocess images the same way. The checkpoint carries everything inference
needs (class order, image size, temperature), so nothing is hard-coded twice.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import torch
from PIL import Image
from torch import nn
from torchvision import models, transforms

from classifier.dataset import BUCKETS

IMG_SIZE = 224
MEAN = (0.485, 0.456, 0.406)  # ImageNet, matches the pretrained weights
STD = (0.229, 0.224, 0.225)
ARCH = "resnet18"


def build_model(pretrained: bool = True) -> nn.Module:
    weights = models.ResNet18_Weights.IMAGENET1K_V1 if pretrained else None
    m = models.resnet18(weights=weights)
    m.fc = nn.Linear(m.fc.in_features, len(BUCKETS))
    return m


def eval_transform(size: int = IMG_SIZE) -> transforms.Compose:
    return transforms.Compose([
        transforms.Grayscale(num_output_channels=3),  # MRIs are grayscale; some JPEGs are RGB
        transforms.Resize((size, size)),
        transforms.ToTensor(),
        transforms.Normalize(MEAN, STD),
    ])


def train_transform(size: int = IMG_SIZE) -> transforms.Compose:
    return transforms.Compose([
        transforms.Grayscale(num_output_channels=3),
        transforms.RandomResizedCrop(size, scale=(0.85, 1.0), ratio=(0.9, 1.1)),
        transforms.RandomHorizontalFlip(),
        transforms.RandomRotation(10),
        transforms.ColorJitter(brightness=0.15, contrast=0.15),
        transforms.ToTensor(),
        transforms.Normalize(MEAN, STD),
    ])


def fit_temperature(logits: torch.Tensor, labels: torch.Tensor) -> float:
    """Temperature scaling on held-out logits (Guo et al. 2017). Returns T > 0."""
    log_t = torch.zeros(1, requires_grad=True)
    opt = torch.optim.LBFGS([log_t], lr=0.1, max_iter=200)
    nll = nn.CrossEntropyLoss()

    def step() -> torch.Tensor:
        opt.zero_grad()
        loss = nll(logits / log_t.exp(), labels)
        loss.backward()
        return loss

    opt.step(step)
    return float(log_t.exp())


def expected_calibration_error(probs: torch.Tensor, labels: torch.Tensor, bins: int = 15) -> float:
    conf, pred = probs.max(dim=1)
    correct = (pred == labels).float()
    edges = torch.linspace(0, 1, bins + 1)
    ece = torch.zeros(())
    for lo, hi in zip(edges[:-1], edges[1:]):
        m = (conf > lo) & (conf <= hi)
        if m.any():
            ece += m.float().mean() * (conf[m].mean() - correct[m].mean()).abs()
    return float(ece)


def save_checkpoint(path: Path, model: nn.Module, temperature: float, meta: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    torch.save({
        "arch": ARCH,
        "buckets": list(BUCKETS),
        "img_size": IMG_SIZE,
        "mean": MEAN,
        "std": STD,
        "temperature": temperature,
        "state_dict": model.state_dict(),
        "meta": meta,
    }, path)


class LoadedModel:
    """Checkpoint loaded for inference. `predict` returns calibrated probabilities."""

    def __init__(self, path: Path, device: str = "cpu") -> None:
        ck = torch.load(path, map_location=device, weights_only=False)
        if ck["arch"] != ARCH or tuple(ck["buckets"]) != BUCKETS:
            raise ValueError(f"checkpoint {path} does not match this code (arch/buckets)")
        self.model = build_model(pretrained=False)
        self.model.load_state_dict(ck["state_dict"])
        self.model.eval().to(device)
        self.temperature = float(ck["temperature"])
        self.transform = eval_transform(ck["img_size"])
        self.device = device
        self.meta = ck.get("meta", {})

    @torch.no_grad()
    def predict(self, image: Image.Image) -> dict[str, float]:
        x = self.transform(image).unsqueeze(0).to(self.device)
        p = torch.softmax(self.model(x) / self.temperature, dim=1)[0].cpu()
        return {b: float(p[i]) for i, b in enumerate(BUCKETS)}
