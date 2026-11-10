"""형식검증 실행 CLI.

    uv run python -m welfaretwin.verify.run --query blindspot
    uv run python -m welfaretwin.verify.run --query cliff --n 3
    uv run python -m welfaretwin.verify.run --query overlap
    uv run python -m welfaretwin.verify.run --query all      # 데모용 사전 계산(artifacts/demo/)

결과는 artifacts/demo/verify_<query>.json 에 메타데이터(규칙·params 해시, 시각)와 함께 저장한다.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from welfaretwin.rules.dsl import RULES_DB
from welfaretwin.rules.engine import load_ruleset
from welfaretwin.verify import queries as q
from welfaretwin.verify.explain import explain

ARTIFACTS = RULES_DB.parent / "artifacts" / "demo"


def _hash_files(paths) -> str:
    h = hashlib.sha256()
    for p in sorted(paths):
        h.update(p.name.encode())
        h.update(p.read_bytes())
    return h.hexdigest()[:12]


def _git_rev() -> str:
    try:
        out = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=RULES_DB.parent,
                             capture_output=True, text=True, timeout=5)
        return out.stdout.strip() or "uncommitted"
    except Exception:
        return "unknown"


def metadata(year: int) -> dict:
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "git_rev": _git_rev(),
        "rules_hash": _hash_files((RULES_DB / "approved").glob("*.yaml")),
        "params_hash": _hash_files([RULES_DB / "params" / f"{year}.yaml"]),
        "params_year": year,
    }


def run_query(name: str, args) -> dict:
    rs = load_ruleset(year=args.year)
    if name == "blindspot":
        r = q.blindspot(rs, n=args.n or 8, poverty_ratio=args.ratio, basis=args.basis)
    elif name == "cliff":
        r = q.cliff(rs, n=args.n or 3, delta=args.delta, budget_sec=args.budget)
    elif name == "overlap":
        r = q.overlap(rs)
    else:
        raise ValueError(name)
    items = explain(r, rs)
    return {"query": name, "status": r.status, "elapsed_sec": round(r.elapsed_sec, 2),
            "options": r.options, "meta": metadata(args.year), "items": items}


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description="복지트윈 형식검증(Z3)")
    ap.add_argument("--query", choices=["blindspot", "cliff", "overlap", "all"], required=True)
    ap.add_argument("--n", type=int)
    ap.add_argument("--ratio", type=float, default=0.40, help="사각지대 빈곤선(기준중위소득 비율)")
    ap.add_argument("--basis", choices=["income_recognized", "actual_income"], default="income_recognized")
    ap.add_argument("--delta", type=int, default=10_000, help="급여 절벽: 근로소득 증가분(원/월)")
    ap.add_argument("--budget", type=float, default=8.0, help="급여 절벽: 탐색 시간 예산(초)")
    ap.add_argument("--year", type=int, default=2026)
    ap.add_argument("--out", type=Path, default=ARTIFACTS)
    ap.add_argument("--no-save", action="store_true")
    args = ap.parse_args(argv)

    names = ["blindspot", "cliff", "overlap"] if args.query == "all" else [args.query]
    for name in names:
        res = run_query(name, args)
        print(f"== {name}: {res['status']} ({res['elapsed_sec']}초, {len(res['items'])}건)")
        for it in res["items"]:
            print(it["text"])
            print()
        if not args.no_save:
            args.out.mkdir(parents=True, exist_ok=True)
            path = args.out / f"verify_{name}.json"
            path.write_text(json.dumps(res, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
            print(f"→ {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
