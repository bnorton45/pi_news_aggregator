"""Feature-hashing tests (libs/classify/featurize.py) — the train/serve shared contract."""

from __future__ import annotations

import numpy as np

from libs.classify.featurize import DIM, featurize


def test_shape_and_dtype() -> None:
    v = featurize("breaking earthquake near the coast")
    assert v.shape == (1, DIM)
    assert v.dtype == np.float32


def test_deterministic_across_calls() -> None:
    # The whole point: hashlib, not builtin hash() — identical across calls/processes.
    a = featurize("major flooding downtown tonight")
    b = featurize("major flooding downtown tonight")
    assert np.array_equal(a, b)


def test_l2_normalized() -> None:
    v = featurize("some newsworthy text with several tokens here")
    assert abs(float(np.linalg.norm(v)) - 1.0) < 1e-5


def test_empty_text_is_zero_vector() -> None:
    v = featurize("")
    assert v.shape == (1, DIM)
    assert not v.any()


def test_different_text_differs() -> None:
    assert not np.array_equal(featurize("earthquake"), featurize("lunch"))


def test_case_insensitive() -> None:
    assert np.array_equal(featurize("Breaking NEWS"), featurize("breaking news"))
