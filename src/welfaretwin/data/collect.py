"""복지서비스 원문 수집기: 목록 → 상세 전체 수집.

    uv run python -m welfaretwin.data.collect --source central --source local
    uv run python -m welfaretwin.data.collect --source central --smoke   # 키·엔드포인트 확인

저장 규칙(문서 03 §2)
- data/raw/{source}/{servId}.json : 상세 응답 원문(raw_xml, 가공 없음) + 수집 메타
- data/raw/{source}/_list/page_NNNN.xml : 목록 응답 원문
- data/raw/{source}/_summary.json : 목록 건수 vs 상세 건수, 실패 목록
- data/raw/_manifest.csv : servId, 수집일시, 소스, 해시
"""

from __future__ import annotations

import argparse
import csv
import functools
import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

from welfaretwin.data.bokjiro_api import ENDPOINTS_VERIFIED, ApiError, BokjiroClient, Source

MANIFEST_FIELDS = ["serv_id", "fetched_at", "source", "sha256"]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def read_manifest(path: Path) -> dict[tuple[str, str], dict[str, str]]:
    if not path.exists():
        return {}
    with path.open(encoding="utf-8", newline="") as f:
        return {(r["source"], r["serv_id"]): r for r in csv.DictReader(f)}


def write_manifest(path: Path, rows: dict[tuple[str, str], dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=MANIFEST_FIELDS)
        w.writeheader()
        for key in sorted(rows):
            w.writerow(rows[key])
    tmp.replace(path)


def scan_manifest(out_dir: Path) -> dict[tuple[str, str], dict[str, str]]:
    """디스크의 상세 JSON 에서 매니페스트를 재구성한다(중단돼도 매니페스트가 어긋나지 않게)."""
    rows = {}
    for source in ("central", "local"):
        for f in sorted((out_dir / source).glob("WLF*.json")):
            rec = json.loads(f.read_text(encoding="utf-8"))
            rows[(rec["source"], rec["serv_id"])] = {k: rec[k] for k in MANIFEST_FIELDS}
    return rows


MAX_CONSECUTIVE_FAILURES = 5


def collect_source(
    client: BokjiroClient,
    source: Source,
    out_dir: Path,
    manifest: dict[tuple[str, str], dict[str, str]],
    *,
    limit: int | None = None,
    force: bool = False,
    list_extra: dict[str, str] | None = None,
    num_rows: int | None = None,
    log=print,
) -> dict:
    src_dir = out_dir / source
    list_dir = src_dir / "_list"
    list_dir.mkdir(parents=True, exist_ok=True)

    serv_ids: list[str] = []
    total_count = 0
    for i, page in enumerate(client.iter_list_pages(source, num_rows, list_extra), start=1):
        (list_dir / f"page_{i:04d}.xml").write_bytes(page.raw)
        total_count = page.total_count
        serv_ids.extend(page.serv_ids)
        log(f"[{source}] 목록 {i}쪽: {len(page.serv_ids)}건 (누적 {len(serv_ids)}/{total_count})")
        if limit and len(serv_ids) >= limit:
            break
    serv_ids = list(dict.fromkeys(serv_ids))  # 순서 유지 중복 제거
    if limit:
        serv_ids = serv_ids[:limit]

    fetched, skipped, failed, streak = 0, 0, [], 0
    for n, sid in enumerate(serv_ids, start=1):
        path = src_dir / f"{sid}.json"
        if path.exists() and not force:
            skipped += 1
            continue
        try:
            url, params, raw = client.detail(source, sid)
        except ApiError as e:
            if not e.retryable and e.code in {"20", "22", "30", "31", "32"}:
                raise  # 키·권한·한도 문제 → 계속해도 전부 실패
            failed.append({"serv_id": sid, "error": str(e)})
            log(f"[{source}] 상세 실패 {sid}: {e}")
            streak += 1
            if streak >= MAX_CONSECUTIVE_FAILURES:
                raise ApiError(e.code, f"연속 {streak}건 실패로 중단 (마지막: {e})", retryable=False) from e
            continue
        streak = 0
        fetched_at = _now()
        sha = hashlib.sha256(raw).hexdigest()
        record = {
            "serv_id": sid,
            "source": source,
            "fetched_at": fetched_at,
            "url": url,
            "params": params,  # serviceKey 는 저장하지 않는다
            "sha256": sha,
            "raw_xml": raw.decode("utf-8"),
        }
        path.write_text(json.dumps(record, ensure_ascii=False, indent=1), encoding="utf-8")
        manifest[(source, sid)] = {
            "serv_id": sid,
            "fetched_at": fetched_at,
            "source": source,
            "sha256": sha,
        }
        fetched += 1
        if n % 50 == 0:
            log(f"[{source}] 상세 {n}/{len(serv_ids)}")

    on_disk = sum(1 for p in src_dir.glob("*.json") if not p.name.startswith("_"))
    summary = {
        "source": source,
        "finished_at": _now(),
        "list_total_count": total_count,
        "list_serv_ids": len(serv_ids),
        "detail_fetched": fetched,
        "detail_skipped_existing": skipped,
        "detail_failed": failed,
        "detail_files_on_disk": on_disk,
        "limit": limit,
        "list_filter": list_extra or {},
        "endpoints_verified": ENDPOINTS_VERIFIED,
    }
    (src_dir / "_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    log(
        f"[{source}] 완료: 목록 {total_count}건 / 신규 {fetched} / 기존 {skipped} / 실패 {len(failed)}"
    )
    return summary


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="복지서비스 원문 수집기")
    p.add_argument("--source", action="append", choices=["central", "local"], required=True)
    p.add_argument("--out", type=Path, default=Path("data/raw"))
    p.add_argument("--limit", type=int, help="소스별 최대 상세 수집 건수")
    p.add_argument("--smoke", action="store_true", help="목록 1쪽 + 상세 1건만 (키 확인용)")
    p.add_argument("--force", action="store_true", help="이미 받은 상세도 다시 수집")
    p.add_argument("--ctpv", help="지자체 목록 시도 필터 (예: 대구광역시)")
    p.add_argument("--sgg", help="지자체 목록 시군구 필터")
    p.add_argument("--interval", type=float, default=0.2, help="요청 간 최소 간격(초)")
    args = p.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")  # 파이프·로그 파일에서 한글 깨짐 방지

    load_dotenv()
    key = os.environ.get("DATA_GO_KR_API_KEY", "").strip()
    if not key:
        print("DATA_GO_KR_API_KEY 가 없습니다. .env.example 을 복사해 .env 에 키를 넣으세요.")
        return 2

    client = BokjiroClient(key, min_interval=args.interval)
    manifest_path = args.out / "_manifest.csv"
    manifest = scan_manifest(args.out)
    log = functools.partial(print, flush=True)
    limit = 1 if args.smoke else args.limit
    num_rows = 5 if args.smoke else None

    try:
        for source in dict.fromkeys(args.source):
            extra = {}
            if source == "local":
                if args.ctpv:
                    extra["ctpvNm"] = args.ctpv
                if args.sgg:
                    extra["sggNm"] = args.sgg
            try:
                collect_source(
                    client, source, args.out, manifest,
                    limit=limit, force=args.force or args.smoke,
                    list_extra=extra, num_rows=num_rows, log=log,
                )
            finally:
                write_manifest(manifest_path, scan_manifest(args.out))
    except ApiError as e:
        print(f"API 오류로 중단: {e}")
        if e.code == "22":
            print("  → 일일 호출 한도 초과. 내일 같은 명령을 다시 실행하면 받은 건은 건너뛰고 이어서 수집.")
        if e.code == "30":
            print("  → 미등록 키. 활용신청 승인 직후라면 1~2시간 뒤 다시 시도하고, Decoding 키인지 확인.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
