"""가상 국민(에이전트) — 페르소나 클러스터마다 NVIDIA Nemotron-Personas-Korea 의 가상 개인 3명을 붙인다. 문서 06 §2.2.

왜: 지금까지 LLM 에게 '가구주 20대, 1인 가구, 소득 1분위' 같은 *집단 설명*을 주고 3번 물었다. 이제는 그 집단 조건에 맞는
*통계 기반 가상 개인*(나이·성별·가구 형태·시도·시군구·직업·인물 묘사)이 각자 1인칭으로 답한다 — '1,000명 생성형 에이전트' 방식.
인물은 Nemotron-Personas-Korea(NVIDIA, CC BY 4.0, KOSIS·대법원·건보공단 통계 기반, 실제 개인 아님)에서 고른다.

경제 상황(소득·재산·주거 점유·장애)은 Nemotron 에 없다 → 우리 합성 인구(클러스터) 값이 정답이고, 인물은 '사람'만 맡는다.

매칭 규칙(결정적 — 해시 순서로 고르므로 다시 돌려도 같은 사람):
  나이     : 클러스터 가구주 연령대 안
  가구 형태 : 가구원 수에 맞는 Nemotron family_type(가구주·배우자일 가능성이 큰 유형만, 아래 FAMILY)
  취업     : 3명 중 취업자 수 = round(3 × 클러스터 가구주 취업 비율), 미취업 = occupation '무직'
  지역     : 클러스터 시도 분포(가중치)를 3자리에 최대 잔여 방식으로 배정
  소득     : [가정] 소득 1~2분위 클러스터에는 고소득 전문·관리직(HIGH_STATUS)을 고르지 않는다
조건을 다 만족하는 사람이 없으면 지역 → 소득 → 취업 순으로 풀고, 몇 단계 풀었는지 match_relaxed 에 남긴다.

    uv run python -m welfaretwin.population.residents            # → artifacts/sim/residents.parquet
"""

from __future__ import annotations

import argparse
import hashlib
import re
import sys
from pathlib import Path

import pandas as pd

from welfaretwin.population.personas import AGE_GROUPS, Persona, build
from welfaretwin.rules.dsl import RULES_DB

ROOT = RULES_DB.parent
NEMOTRON = ROOT / "data" / "raw" / "nemotron" / "data" / "train-00000-of-00009.parquet"
OUT = ROOT / "artifacts" / "sim" / "residents.parquet"
SOURCE = "NVIDIA Nemotron-Personas-Korea (CC BY 4.0) https://huggingface.co/datasets/nvidia/Nemotron-Personas-Korea"
COLS = ["uuid", "sex", "age", "marital_status", "family_type", "housing_type", "education_level", "occupation",
        "district", "province", "persona", "family_persona"]
PER_PERSONA = 3

# 가구원 수 → 그 가구의 가구주·배우자일 가능성이 큰 Nemotron 가구 형태('부모와 동거' 처럼 자녀 입장인 유형은 제외)
FAMILY = {
    1: {"혼자 거주", "혼자 거주 (배우자 별거)"},
    2: {"배우자와 거주", "자녀와 거주 (한부모)", "자녀와 거주 (배우자 별거)", "손자녀와 거주", "형제자매와 동거 (가구주)",
        "비친족 동거", "기타1세대", "가구주+기타친인척"},
    3: {"배우자·자녀와 거주", "자녀와 거주 (한부모)", "자녀와 거주 (배우자 별거)", "배우자·편부모와 거주",
        "배우자·손자녀와 거주", "자녀·어머니와 거주", "기타2세대"},
    4: {"배우자·자녀와 거주", "배우자·자녀·어머니와 거주", "배우자·자녀·부모와 거주", "배우자·자녀·아버지와 거주",
        "배우자·부모와 거주", "기타3세대", "기타2세대"},
}
# [가정] 소득 1~2분위 가구주로 보기 어려운 고소득 전문·관리직(직업명 일부)
HIGH_STATUS = re.compile(r"의사|변호사|판사|검사|회계사|세무사|변리사|교수|임원|고위|관리자|약사|한의사|치과|감정평가사|조종사|펀드|투자")
# 합성 인구 시도명 → Nemotron province
SIDO = {"서울": "서울", "부산": "부산", "대구": "대구", "인천": "인천", "광주": "광주", "대전": "대전", "울산": "울산",
        "세종": "세종", "경기": "경기", "강원": "강원", "충청북": "충청북", "충청남": "충청남", "전북": "전북", "전라북": "전북",
        "전라남": "전라남", "경상북": "경상북", "경상남": "경상남", "제주": "제주"}


def province_of(sido: str) -> str:
    for k, v in SIDO.items():
        if sido.startswith(k):
            return v
    raise KeyError(f"시도 이름을 Nemotron 지역으로 바꿀 수 없음: {sido}")


def load_pool(path: Path = NEMOTRON) -> pd.DataFrame:
    if not path.exists():
        raise SystemExit(f"없음: {path}\n  uv run --with huggingface_hub python -c \"from huggingface_hub import hf_hub_download; "
                         "hf_hub_download('nvidia/Nemotron-Personas-Korea', 'data/train-00000-of-00009.parquet', "
                         "repo_type='dataset', local_dir='data/raw/nemotron')\"")
    return pd.read_parquet(path, columns=COLS).reset_index(drop=True)


def _slots(weights: pd.Series, k: int) -> list[str]:
    """가중치 분포를 k 자리에 최대 잔여 방식으로 배정(결정적)."""
    w = weights[weights > 0].sort_values(ascending=False)
    share = w / w.sum() * k
    base = share.astype(int)
    rest = (share - base).sort_values(ascending=False)
    out = [name for name, n in base.items() for _ in range(n)]
    out += list(rest.index[: k - len(out)])
    return out[:k]


def _rank(pid: str, uuids: pd.Series) -> pd.Series:
    return uuids.map(lambda u: hashlib.sha1(f"{pid}|{u}".encode()).hexdigest())


def match(personas: list[Persona], pid: pd.Series, hh: pd.DataFrame, pool: pd.DataFrame,
          k: int = PER_PERSONA) -> pd.DataFrame:
    """클러스터마다 가상 개인 k명. 한 사람은 한 클러스터에만 쓴다."""
    by_sido = hh.set_index("hh_id").join(pid).assign(prov=lambda d: d["region_sido"].map(province_of)) \
        .groupby(["persona_id", "prov"])["weight"].sum()
    used: set[str] = set()
    rows = []
    for p in sorted(personas, key=lambda p: -p.weight):   # 큰 클러스터부터(겹칠 때 우선권)
        lo, hi = next((a, b) for a, b, name in AGE_GROUPS if name == p.age_group)
        size = {"1인": 1, "2인": 2, "3인": 3, "4인 이상": 4}[p.size_group]
        n_emp = round(k * p.profile["employed_share"])
        provs = _slots(by_sido.loc[p.persona_id], k)
        base = pool[(pool["age"] >= max(lo, 19)) & (pool["age"] <= hi) & pool["family_type"].isin(FAMILY[size])
                    & ~pool["uuid"].isin(used)]
        for slot in range(k):
            employed = slot < n_emp
            want_prov = provs[slot % len(provs)]
            tries = [("region", "income", "employment"), ("income", "employment"), ("employment",), ()]
            for level, keep in enumerate(tries):
                c = base
                if "employment" in keep:
                    c = c[(c["occupation"] != "무직") == employed]
                if "income" in keep and p.income_quintile <= 2:
                    c = c[~c["occupation"].str.contains(HIGH_STATUS)]
                if "region" in keep:
                    c = c[c["province"] == want_prov]
                c = c[~c["uuid"].isin(used)]
                if len(c):
                    break
            else:
                raise RuntimeError(f"{p.persona_id}: 조건을 모두 풀어도 맞는 인물이 없음")
            pick = c.loc[_rank(p.persona_id, c["uuid"]).sort_values().index[0]]
            used.add(pick["uuid"])
            rows.append({"persona_id": p.persona_id, "slot": slot, "match_relaxed": level,
                         "target_province": want_prov, "target_employed": employed, **pick[COLS].to_dict()})
    return pd.DataFrame(rows).sort_values(["persona_id", "slot"]).reset_index(drop=True)


def run() -> pd.DataFrame:
    hh = pd.read_parquet(ROOT / "data" / "processed" / "households.parquet")
    persons = pd.read_parquet(ROOT / "data" / "processed" / "persons.parquet")
    personas, pid = build(hh, persons)
    return match(personas, pid, hh, load_pool())


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    argparse.ArgumentParser(description="페르소나 클러스터 × Nemotron 가상 개인 매칭").parse_args(argv)
    df = run()
    OUT.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(OUT, index=False)
    print(f"{df['persona_id'].nunique()}개 클러스터 × {PER_PERSONA}명 = {len(df)}명, 중복 인물 {df['uuid'].duplicated().sum()}명")
    print("조건 완화 단계별 인원:", df["match_relaxed"].value_counts().sort_index().to_dict(), "(0 = 모든 조건 충족)")
    print(f"출처: {SOURCE}\n→ {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
