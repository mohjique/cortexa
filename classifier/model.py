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


class RandomDegrade:
    """Randomly degrade a [0,1] image tensor the way real scans degrade.

    Written from scratch (never trained on the dataset's blurred / noisy /
    motion files, which stay a held-out check). Covers the same families:
    Gaussian, speckle and salt-and-pepper noise; blur; motion ghosting.
    The first model, trained without this, called 86 of 264 noisy copies of
    real tumors "no tumor", mostly with high confidence.
    """

    def __init__(self, p: float = 0.5) -> None:
        self.p = p
        self.blur = transforms.GaussianBlur(kernel_size=7, sigma=(0.5, 2.0))

    def __call__(self, x: torch.Tensor) -> torch.Tensor:
        if torch.rand(()) >= self.p:
            return x
        kind = int(torch.randint(0, 5, ()))
        if kind == 0:  # gaussian noise
            x = x + torch.randn_like(x[:1]) * float(torch.empty(()).uniform_(0.02, 0.15))
        elif kind == 1:  # speckle (multiplicative)
            x = x * (1 + torch.randn_like(x[:1]) * float(torch.empty(()).uniform_(0.05, 0.4)))
        elif kind == 2:  # salt and pepper
            amount = float(torch.empty(()).uniform_(0.005, 0.06))
            r = torch.rand_like(x[:1])
            x = torch.where(r < amount / 2, torch.zeros_like(x), x)
            x = torch.where(r > 1 - amount / 2, torch.ones_like(x), x)
        elif kind == 3:  # blur
            x = self.blur(x)
        else:  # motion: faint shifted copies along the phase-encode axis (ghosting)
            dim = 1 + int(torch.randint(0, 2, ()))
            ghost = torch.zeros_like(x)
            for _ in range(int(torch.randint(1, 4, ()))):
                ghost += torch.roll(x, int(torch.randint(8, 40, ())), dims=dim)
            w = float(torch.empty(()).uniform_(0.08, 0.3))
            x = (1 - w) * x + w * ghost / ghost.amax().clamp(min=1e-6) * x.amax()
        return x.clamp(0, 1)


def train_transform(size: int = IMG_SIZE) -> transforms.Compose:
    return transforms.Compose([
        transforms.Grayscale(num_output_channels=3),
        transforms.RandomResizedCrop(size, scale=(0.85, 1.0), ratio=(0.9, 1.1)),
        transforms.RandomHorizontalFlip(),
        transforms.RandomRotation(10),
        transforms.ColorJitter(brightness=0.15, contrast=0.15),
        transforms.ToTensor(),
        RandomDegrade(p=0.5),
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
