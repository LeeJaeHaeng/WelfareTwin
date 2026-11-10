import numpy as np
import pytest

from welfaretwin.population.ipf import Marginal, integerize, ipf


def test_two_way_matches_both_margins():
    rows = np.array([30.0, 70.0])
    cols = np.array([20.0, 50.0, 30.0])
    r = ipf(np.ones((2, 3)), [Marginal((0,), rows, "rows"), Marginal((1,), cols, "cols")])
    assert r.converged
    np.testing.assert_allclose(r.weights.sum(axis=1), rows, rtol=1e-6)
    np.testing.assert_allclose(r.weights.sum(axis=0), cols, rtol=1e-6)


def test_three_way_with_two_dimensional_margins():
    rng = np.random.default_rng(0)
    truth = rng.random((4, 3, 5)) * 100
    a = truth.sum(axis=2)            # 축 (0,1)
    b = truth.sum(axis=0).T          # 축 (2,1) 순서
    r = ipf(np.ones_like(truth), [Marginal((0, 1), a, "a"), Marginal((2, 1), b, "b")])
    assert r.converged and r.max_rel_error < 1e-6
    np.testing.assert_allclose(r.weights.sum(axis=2), a, rtol=1e-5)
    np.testing.assert_allclose(r.weights.sum(axis=0).T, b, rtol=1e-5)


def test_structural_zeros_kept():
    seed = np.ones((2, 2))
    seed[0, 1] = 0           # 불가능한 셀
    r = ipf(seed, [Marginal((0,), np.array([10.0, 10.0])), Marginal((1,), np.array([15.0, 5.0]))])
    assert r.weights[0, 1] == 0


def test_shape_mismatch_rejected():
    with pytest.raises(ValueError):
        ipf(np.ones((2, 3)), [Marginal((0,), np.array([1.0, 2.0, 3.0]))])


def test_integerize_sums_to_n_and_tracks_weights():
    w = np.array([[1.0, 2.0], [3.0, 4.0]])
    counts = integerize(w, 1000, np.random.default_rng(1))
    assert counts.sum() == 1000
    np.testing.assert_allclose(counts / 1000, w / w.sum(), atol=0.002)
