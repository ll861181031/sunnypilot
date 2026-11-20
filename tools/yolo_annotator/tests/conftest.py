"""Shared pytest configuration: make the app modules importable."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

APP_DIR = Path(__file__).resolve().parents[1]
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

import demo


@pytest.fixture()
def demo_batch(tmp_path: Path) -> Path:
    """A fresh synthetic batch (4 images, 22 classes) for one test."""
    dest = tmp_path / "batch_demo"
    demo.make_demo_batch(dest, n=4, seed=11)
    return dest


@pytest.fixture()
def dataset(demo_batch: Path):
    import dataset as ds

    return ds.Dataset.open(demo_batch)
