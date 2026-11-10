"""IPF(반복비례조정) — 문서 06 §1.1.

다차원 셀 가중치 배열을 여러 주변분포(일부 축의 합)에 동시에 맞춘다.

    w = ipf(seed, [Marginal(axes=(0, 1), target=sido_by_size), Marginal(axes=(2, 1), target=age_by_size)])

- target 은 axes 순서대로의 배열. 합계가 서로 다르면 첫 주변분포의 합계로 맞춘다(비율 보존).
- 수렴 판정: 모든 주변분포의 최대 상대오차 ≤ tol.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass
class Marginal:
    axes: tuple[int, ...]
    target: np.ndarray
    name: str = ""


@dataclass
class IPFResult:
    weights: np.ndarray
    iterations: int
    max_rel_error: float
    errors: dict[str, float]
    converged: bool


def _project(w: np.ndarray, axes: tuple[int, ...]) -> np.ndarray:
    """w 를 axes 이외 축으로 합산하고, 결과 축 순서를 axes 순서로 맞춘다."""
    other = tuple(i for i in range(w.ndim) if i not in axes)
    s = w.sum(axis=other)
    kept = sorted(axes)
    return np.transpose(s, [kept.index(a) for a in axes])


def _expand(f: np.ndarray, axes: tuple[int, ...], ndim: int) -> np.ndarray:
    """axes 순서의 배열 f 를 w 와 브로드캐스트 가능한 모양으로."""
    kept = sorted(axes)
    f = np.transpose(f, [axes.index(a) for a in kept])
    shape = [1] * ndim
    for a, n in zip(kept, f.shape):
        shape[a] = n
    return f.reshape(shape)


def ipf(seed: np.ndarray, marginals: list[Marginal], tol: float = 1e-6, max_iter: int = 1000) -> IPFResult:
    w = np.asarray(seed, dtype=float).copy()
    if (w < 0).any():
        raise ValueError("seed 에 음수가 있음")
    total = float(np.asarray(marginals[0].target, dtype=float).sum())
    targets = []
    for m in marginals:
        t = np.asarray(m.target, dtype=float)
        if t.shape != tuple(w.shape[a] for a in m.axes):
            raise ValueError(f"주변분포 {m.name or m.axes} 모양 {t.shape} 이 축 크기와 다름")
        if (t < 0).any():
            raise ValueError(f"주변분포 {m.name or m.axes} 에 음수")
        targets.append(t * (total / t.sum()) if t.sum() > 0 else t)

    def errors() -> dict[str, float]:
        out = {}
        for m, t in zip(marginals, targets):
            cur = _project(w, m.axes)
            denom = np.where(t > 0, t, 1.0)
            out[m.name or str(m.axes)] = float(np.max(np.abs(cur - t) / denom))
        return out

    it = 0
    for it in range(1, max_iter + 1):
        for m, t in zip(marginals, targets):
            cur = _project(w, m.axes)
            with np.errstate(divide="ignore", invalid="ignore"):
                f = np.where(cur > 0, t / cur, 0.0)
            w *= _expand(f, m.axes, w.ndim)
        errs = errors()
        if max(errs.values()) <= tol:
            return IPFResult(w, it, max(errs.values()), errs, True)
    errs = errors()
    return IPFResult(w, it, max(errs.values()), errs, False)


def integerize(weights: np.ndarray, n: int, rng: np.random.Generator) -> np.ndarray:
    """셀 가중치 → 정수 표본 수(합 n). 내림 후 나머지를 확률적으로 배분(TRS 방식)."""
    p = weights / weights.sum() * n
    base = np.floor(p).astype(int)
    rem = int(n - base.sum())
    if rem > 0:
        frac = (p - base).ravel()
        idx = rng.choice(frac.size, size=rem, replace=False, p=frac / frac.sum())
        np.add.at(base.ravel(), idx, 1)
    return base
