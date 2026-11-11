"""실시간 계산 API — 문서 08 §1. 정적 웹(welfaretwin.vercel.app)이 부르는 계산 서버.

    uv run uvicorn welfaretwin.api.main:app --port 7860

판정·검증·시나리오는 대시보드와 같은 엔진(rules.engine · verify.queries · sim.scenario)을 그대로 부른다.
공개 서버이므로 LLM 은 부르지 않는다(레드팀은 캐시된 사업안만 — 웹의 사전 계산본).
무거운 계산(Z3·시나리오)은 한 번에 하나씩, IP 당 분당 횟수를 제한한다.
"""

from __future__ import annotations

import os
import threading
import time
from collections import OrderedDict, defaultdict, deque
from contextlib import asynccontextmanager, contextmanager
from functools import lru_cache
from typing import Literal

import pandas as pd
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from welfaretwin.model import Household
from welfaretwin.rules.dsl import RULES_DB
from welfaretwin.rules.engine import RuleSet, _Ctx, evaluate, load_ruleset
from welfaretwin.verify.explain import describe_household, failed_reasons

ROOT = RULES_DB.parent
CENTRAL_GROUPS = ["NBLS_LIVELIHOOD", "NBLS_HOUSING", "NBLS_EDUCATION", "BASIC_PENSION", "EITC", "ENERGY_VOUCHER"]
INCOME_SUPPORT = ["NBLS_LIVELIHOOD", "BASIC_PENSION", "EITC"]
ON_VERCEL = bool(os.environ.get("VERCEL"))   # Vercel 함수(1 vCPU): 프로세스 풀·시작 예열 없이
SCENARIO_SAMPLE = int(os.environ.get("WT_SCENARIO_SAMPLE", "10000"))
WORKERS = int(os.environ.get("WT_WORKERS", "1" if ON_VERCEL else str(min(4, os.cpu_count() or 2))))
HEAVY_PER_MIN = int(os.environ.get("WT_HEAVY_PER_MIN", "6"))
# Z3 절벽 탐색은 벽시계 대신 Z3 계산량(rlimit)을 예산으로 쓴다(verify.queries.cliff 결정적 모드) —
# 서버 CPU 속도·부하와 무관하게 같은 입력이면 항상 같은 결과. 1 가상초 = 1,040만 rlimit(개발 PC 1초 분량, 2026-11 측정).
# 예산 7 가상초 ≈ 개발 PC 7초 · Vercel 1 vCPU 약 20초.
CLIFF_BUDGET = float(os.environ.get("WT_CLIFF_BUDGET", "7"))
CLIFF_RLIMIT_PER_SEC = int(os.environ.get("WT_CLIFF_RLIMIT", "10400000"))

BASELINE_FILE = ROOT / "artifacts" / "api" / "scenario_base.pkl"   # scripts/deploy_api.py 가 배포 묶음에만 만든다
_baseline_tried = False


def _preload_baseline() -> bool:
    """배포 묶음의 기준선(현행 규칙, 같은 표본) 가구 표를 캐시에 넣는다 — 지금 서버의 표본과 가구가 같을 때만."""
    from welfaretwin.sim.scenario import PolicyChange, sample_population, table_key
    from welfaretwin.sim.static import load_population
    global _baseline_tried
    key = table_key(PolicyChange("현행"), SCENARIO_SAMPLE)
    if key in _scenario_cache:
        return True
    if _baseline_tried or not BASELINE_FILE.exists():
        return False
    _baseline_tried = True
    saved = pd.read_pickle(BASELINE_FILE)
    hh, persons = load_population(ROOT / "data" / "processed")
    ids = set(sample_population(hh, persons, SCENARIO_SAMPLE)[0]["hh_id"])
    if saved.get("key") != key or set(saved["table"].index) != ids:
        return False                          # 표본·키가 다르면 쓰지 않고 새로 계산
    _scenario_cache[key] = saved["table"]
    _scenario_cache.move_to_end(key, last=False)   # 기준선은 캐시 맨 앞(비울 때 남김)
    return True


def _warmup() -> None:
    """서버가 뜨면 규칙·지역 목록을 읽고, 시나리오 기준선(현행·S0, 같은 표본)을 미리 계산해 첫 요청을 빠르게."""
    ruleset()
    _regions()
    if (ROOT / "data" / "processed" / "households.parquet").exists():
        from welfaretwin.sim.scenario import PolicyChange, compare
        with _heavy:
            _preload_baseline()
            compare([(PolicyChange("현행"), "S0")], sample=SCENARIO_SAMPLE, workers=WORKERS, cache=_scenario_cache)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    if os.environ.get("WT_WARMUP", "0" if ON_VERCEL else "1") == "1":
        threading.Thread(target=_warmup, daemon=True).start()
    yield


app = FastAPI(title="WelfareTwin API", version="0.1.0", docs_url="/docs", redoc_url=None, lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[o for o in os.environ.get("WT_CORS_ORIGINS", "").split(",") if o]
    + ["https://welfaretwin.vercel.app", "http://localhost:8790", "http://127.0.0.1:8790"],
    allow_origin_regex=r"https://welfaretwin(-[a-z0-9-]+)?\.vercel\.app",
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type"],
    max_age=600,
)

_heavy = threading.Semaphore(1)
_hits: dict[str, deque] = defaultdict(deque)
_scenario_cache: OrderedDict = OrderedDict()


@lru_cache(maxsize=1)
def ruleset() -> RuleSet:
    return load_ruleset()


def _client(req: Request) -> str:
    fwd = req.headers.get("x-forwarded-for", "")
    return fwd.split(",")[0].strip() or (req.client.host if req.client else "?")


class _Heavy:
    """무거운 계산: IP 당 분당 HEAVY_PER_MIN 회, 동시에 하나(대기 최대 60초)."""

    def __init__(self, req: Request):
        self.ip = _client(req)

    def __enter__(self):
        now, q = time.monotonic(), _hits[self.ip]
        while q and now - q[0] > 60:
            q.popleft()
        if len(q) >= HEAVY_PER_MIN:
            raise HTTPException(429, f"요청이 많습니다. 1분에 {HEAVY_PER_MIN}회까지 실행할 수 있어요.")
        q.append(now)
        if not _heavy.acquire(timeout=60):
            raise HTTPException(503, "다른 계산이 진행 중입니다. 잠시 뒤 다시 시도해 주세요.")
        return self

    def __exit__(self, *exc):
        _heavy.release()


def _visible(pid: str, hh: Household, rs: RuleSet) -> bool:
    """중앙 제도는 모두, 지자체 사업은 그 가구의 지역 사업만 보여 준다."""
    p = rs.programs[pid]
    if p.level == "central" or not p.region:
        return True
    return p.region.sido == hh.region_sido and (not p.region.sigungu or p.region.sigungu == hh.region_sigungu)


# ------------------------------------------------------------------ 가벼운 계산

@app.get("/health")
def health() -> dict:
    rs = ruleset()
    pop = (ROOT / "data" / "processed" / "households.parquet").exists()
    return {"ok": True, "programs": len(rs.programs), "population": pop, "scenario_sample": SCENARIO_SAMPLE,
            "selection_ratio_livelihood": float(rs.params.params["selection_ratio_livelihood"].value),
            "llm": False, "version": os.environ.get("WT_VERSION", "dev")}


@app.get("/programs")
def programs() -> list[dict]:
    return [{"id": pid, "name": p.name, "level": p.level,
             "region": p.region.model_dump() if p.region else None} for pid, p in ruleset().programs.items()]


@lru_cache(maxsize=1)
def _regions() -> dict[str, list[str]]:
    path = ROOT / "data" / "processed" / "households.parquet"
    out: dict[str, set[str]] = defaultdict(set)
    if path.exists():
        hh = pd.read_parquet(path, columns=["region_sido", "region_sigungu"]).drop_duplicates()
        for s, g in hh.itertuples(index=False):
            if isinstance(g, str) and g:
                out[s].add(g)
    for p in ruleset().programs.values():
        if p.region:
            out[p.region.sido]
            if p.region.sigungu:
                out[p.region.sido].add(p.region.sigungu)
    return {s: sorted(v) for s, v in sorted(out.items())}


@app.get("/regions")
def regions() -> dict:
    return _regions()


@app.post("/evaluate")
def evaluate_household(hh: Household) -> dict:
    t0 = time.perf_counter()
    rs = ruleset()
    decisions = [d for d in evaluate(hh, rs) if _visible(d.program_id, hh, rs)]
    ctx = _Ctx(hh, rs)
    own = sum(m.earned_income + m.business_income + m.other_income for m in hh.members)
    return {
        "household_text": describe_household(hh),
        "income_recognized": round(float(ctx.derived("nbls_income_recognized", None))),
        "median_income": int(ctx.median_income(hh.size)),
        "own_income": own,
        "disposable": own + sum(d.benefit_monthly for d in decisions),
        "decisions": [{"program_id": d.program_id, "program_name": d.program_name, "eligible": d.eligible,
                       "benefit_monthly": d.benefit_monthly, "level": rs.programs[d.program_id].level,
                       "reasons": [] if d.eligible else failed_reasons(hh, rs, d.program_id)} for d in decisions],
        "elapsed_sec": round(time.perf_counter() - t0, 3),
    }


class SweepRequest(BaseModel):
    household: Household
    member: int = Field(0, ge=0)
    max_income: int = Field(3_500_000, ge=100_000, le=6_000_000)
    step: int = Field(10_000, ge=10_000, le=500_000)


@app.post("/sweep")
def sweep(req: SweepRequest) -> dict:
    """한 가구원의 월 근로소득을 0 → max_income 으로 올리며 전 제도 평가 — 웹 sweeps.json 과 같은 형식."""
    if req.member >= len(req.household.members):
        raise HTTPException(422, "member 가 가구원 수보다 큽니다.")
    if req.max_income // req.step > 600:
        raise HTTPException(422, "구간이 너무 많습니다(최대 600개).")
    t0 = time.perf_counter()
    rs = ruleset()
    base = req.household.model_dump()
    xs, own, series = [], [], {g: [] for g in [*CENTRAL_GROUPS, "LOCAL"]}
    local_names: set[str] = set()
    for inc in range(0, req.max_income + 1, req.step):
        members = [dict(m) for m in base["members"]]
        members[req.member]["earned_income"] = inc
        members[req.member]["employed"] = inc > 0          # web_export.sweeps 와 같은 규칙
        hh = Household(**{**base, "members": members})
        vals = dict.fromkeys(series, 0)
        for d in evaluate(hh, rs):
            g = d.program_id if d.program_id in CENTRAL_GROUPS else "LOCAL"
            if g == "LOCAL" and d.benefit_monthly:
                local_names.add(rs.programs[d.program_id].name)
            if g in vals:
                vals[g] += d.benefit_monthly
        xs.append(inc)
        own.append(sum(m.earned_income + m.business_income + m.other_income for m in hh.members))
        for g in series:
            series[g].append(vals[g])
    return {"label": describe_household(req.household), "earned": xs, "own_income": own,
            "series": {g: v for g, v in series.items() if any(v)}, "local_programs": sorted(local_names),
            "elapsed_sec": round(time.perf_counter() - t0, 2)}


# ------------------------------------------------------------------ 무거운 계산(Z3 · 시나리오)

class BlindspotRequest(BaseModel):
    poverty_ratio: float = Field(0.40, ge=0.20, le=0.60)
    programs: list[Literal["NBLS_LIVELIHOOD", "BASIC_PENSION", "EITC"]] = Field(default_factory=lambda: list(INCOME_SUPPORT),
                                                                            min_length=1)
    n: int = Field(8, ge=1, le=8)


@app.post("/verify/blindspot")
def verify_blindspot(req: BlindspotRequest, request: Request) -> dict:
    from welfaretwin.verify import queries as q
    from welfaretwin.verify.explain import explain
    with _Heavy(request):
        rs = ruleset()
        r = q.blindspot(rs, programs=list(dict.fromkeys(req.programs)), poverty_ratio=req.poverty_ratio, n=req.n,
                        timeout_ms=10_000)
        items = explain(r, rs)
    return {"query": "blindspot", "status": r.status, "elapsed_sec": round(r.elapsed_sec, 2),
            "options": r.options, "items": items}


@contextmanager
def _fresh_z3_context():
    """이 블록 안에서만 Z3 기본 컨텍스트를 새것으로 — 앞선 계산의 내부 상태가 탐색 경로를 바꾸지 않게, 끝나면 원래대로.

    자원 한도만으로는 부족했다(같은 프로세스 두 번째 호출부터 결과가 달라짐, 새 컨텍스트면 매번 같음).
    원래 컨텍스트로 되돌리므로 다른 곳에서 만든 Z3 객체와 섞이지 않는다. 무거운 계산은 한 번에 하나(_heavy)라 안전.
    z3 모듈의 내부 변수라 버전이 바뀌어 없으면 그냥 넘어간다(결과는 맞고 재현성만 약해짐).
    """
    import z3
    mod = z3.z3
    if not hasattr(mod, "_main_ctx"):
        yield
        return
    old, mod._main_ctx = mod._main_ctx, None
    try:
        yield
    finally:
        mod._main_ctx = old


class CliffRequest(BaseModel):
    delta: int = Field(10_000, ge=10_000, le=500_000)
    n: int = Field(4, ge=1, le=5)


@app.post("/verify/cliff")
def verify_cliff(req: CliffRequest, request: Request) -> dict:
    from welfaretwin.verify import queries as q
    from welfaretwin.verify.explain import explain
    with _Heavy(request):
        rs = ruleset()
        with _fresh_z3_context():
            r = q.cliff(rs, delta=req.delta, n=req.n, budget_sec=CLIFF_BUDGET, rlimit_per_sec=CLIFF_RLIMIT_PER_SEC)
        items = explain(r, rs)
    return {"query": "cliff", "status": r.status, "elapsed_sec": round(r.elapsed_sec, 2),
            "options": r.options, "items": items}


class ScenarioRequest(BaseModel):
    selection_ratio_livelihood: float = Field(0.32, ge=0.25, le=0.40)
    behavior: Literal["S0", "S1"] = "S0"


@app.post("/scenario")
def scenario(req: ScenarioRequest, request: Request) -> dict:
    """현행(S0) 대비 생계급여 선정기준·신청 방식 변경 — 표본 SCENARIO_SAMPLE 가구(가중치 보정), 같은 표본으로 쌍 비교."""
    if not (ROOT / "data" / "processed" / "households.parquet").exists():
        raise HTTPException(503, "합성 인구 파일이 없습니다.")
    from welfaretwin.sim.scenario import PolicyChange, compare
    ratio = round(req.selection_ratio_livelihood, 3)
    change = PolicyChange(f"생계급여 선정기준 {ratio:.0%}", {"selection_ratio_livelihood": ratio})
    with _Heavy(request):
        _preload_baseline()
        out = compare([(PolicyChange("현행"), "S0"), (change, req.behavior)], sample=SCENARIO_SAMPLE,
                      workers=WORKERS, cache=_scenario_cache)
        while len(_scenario_cache) > 12:       # 기준선(처음 넣은 것)은 남기고 오래된 변경안부터 비움
            keys = list(_scenario_cache)
            _scenario_cache.pop(keys[1])
    out["note"] = (f"합성 인구 {SCENARIO_SAMPLE:,}가구 표본(가중치 보정)으로 계산 — 현행과 변경안은 같은 표본으로 비교. "
                   "발표 수치(전체 10만 가구)와 표본 오차만큼 다를 수 있음")
    return out
