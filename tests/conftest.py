"""Shared fixtures."""

from __future__ import annotations

import warnings
from collections.abc import Iterator

import pytest

from quickjs_rs import Context, Runtime


@pytest.fixture
def rt() -> Iterator[Runtime]:
    with Runtime() as runtime:
        yield runtime


@pytest.fixture
def ctx(rt: Runtime) -> Iterator[Context]:
    with rt.new_context() as context:
        yield context


@pytest.fixture(autouse=True)
def strict_warnings() -> Iterator[None]:
    with warnings.catch_warnings():
        warnings.filterwarnings("error", category=ResourceWarning)
        yield
