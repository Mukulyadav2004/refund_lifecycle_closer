"""Shared fixtures. The dataset is built once per session; a build is the slow part."""

from __future__ import annotations

import pytest

from rlc.config import REPO_ROOT, Config
from rlc.generate import SyntheticDataGenerator
from rlc.loader import Sources

_CACHE: dict[str, object] = {}


def _dataset():
    if "ds" not in _CACHE:
        cfg = Config.load(REPO_ROOT / "config.yaml")
        _CACHE["cfg"] = cfg
        _CACHE["ds"] = SyntheticDataGenerator(cfg).build()
    return _CACHE["cfg"], _CACHE["ds"]


def sources_from(ds) -> Sources:
    return Sources.build(
        payments=ds.payments,
        refunds=ds.refunds,
        disputes=ds.disputes,
        settlements=ds.settlements,
        recon=ds.recon,
        returns_ledger=ds.returns_ledger,
    )


@pytest.fixture(scope="session")
def cfg():
    return _dataset()[0]


@pytest.fixture(scope="session")
def dataset():
    return _dataset()[1]


@pytest.fixture(scope="session")
def sources(dataset):
    return sources_from(dataset)


@pytest.fixture(scope="session")
def build_sources():
    """Factory: turn a Dataset (or a trimmed copy of one) into indexed Sources."""
    return sources_from
