# -*- coding: utf-8 -*-
"""
data/dart_bizno_input_list.txt 의 사업자번호로
data/dart_biz_master_list.csv(사업자번호·업체명·공시번호·종목코드) 를 생성합니다.

1) company.json?bizr_no=... (지원되는 경우에만 성공)
2) 실패 시: corpCode.xml 전체 목록을 받아 각 고유번호로 company.json 조회하며
   사업자번호가 일치하는 행만 기록 (오픈다트 공식 방식)

사용: python build_dart_registry.py
     python build_dart_registry.py --resume-scan
     python build_dart_registry.py --list-incomplete   # 종목코드 비어 있는 행만 출력(API 없음)
     python build_dart_registry.py --scan-status       # corp 목록 길이·재개 인덱스 요약(API 필요)
     python build_dart_registry.py --export-no-seed-match   # 마스터 CSV만으로 못 채운 입력 번호 내보내기(API 없음)

.env: DART_API_KEY, DART_BUILD_DAILY_CALL_CAP(기본 40000), DART_BUILD_MAX_SCAN(2단계 기본 상한),
      DART_BUILD_API_MARGIN(기본 300), DART_CORPCODE_IGNORE_CACHE=1 은 corpCode.zip 캐시 무시
실행 이력: data/dart_build_history.json
(.env 에 DART_API_KEY 필요)
"""

from __future__ import annotations

import argparse
import csv
import os
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")
except ImportError:
    pass

from services.dart_service import (  # noqa: E402
    DART_BIZNO_INPUT_LIST,
    DART_BIZ_MASTER_CSV,
    REGISTRY_CSV,
    fetch_company_json,
    fetch_company_json_by_bizr_no,
    get_dart_api_key,
    load_corp_list,
    parse_biz_master_csv_row,
)
from services.news_rss import normalize_biz_number  # noqa: E402
from services.dart_build_history import (  # noqa: E402
    STOCK_CODE_GAP_CAUSES_KO,
    default_daily_call_cap,
    default_stage2_max_scan,
    print_last_run_summary,
    save_history,
)

DEFAULT_INPUT = DART_BIZNO_INPUT_LIST
DEFAULT_OUTPUT = DART_BIZ_MASTER_CSV

# 2단계 스캔 중단 시 다음 실행에서 corp 목록의 이 인덱스부터 이어서 (--scan-start-index 또는 --resume-scan)
SCAN_RESUME_FILENAME = ".dart_build_scan_next_index"


def _scan_resume_path(out_csv: Path) -> Path:
    return out_csv.parent / SCAN_RESUME_FILENAME


def write_master_csv(
    out_path: Path,
    ordered_biz: list[str],
    results: dict[str, dict[str, str]],
) -> int:
    """ordered_biz 순서대로 results 에 있는 행만 기록. 반환: 기록 행 수."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = ["사업자번호", "업체명", "공시번호", "종목코드"]
    rows_written = 0
    with out_path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames, lineterminator="\n")
        w.writeheader()
        for biz in ordered_biz:
            row = results.get(biz)
            if not row:
                continue
            w.writerow(
                {
                    "사업자번호": row["business_number"],
                    "업체명": row["corp_name"],
                    "공시번호": row["corp_code"],
                    "종목코드": row.get("stock_code") or "",
                }
            )
            rows_written += 1
    return rows_written


def apply_seed_csv_restoration(
    results: dict[str, dict[str, str]],
    pending: set[str],
) -> None:
    """0단계: dart_biz_master_list · dart_company_registry 에서 pending 번호만 복원."""
    for seed_path in (DART_BIZ_MASTER_CSV, REGISTRY_CSV):
        if not seed_path.is_file():
            continue
        try:
            with seed_path.open(encoding="utf-8-sig", newline="") as ef:
                for row in csv.DictReader(ef):
                    parsed = parse_biz_master_csv_row(row)
                    if not parsed:
                        continue
                    b, entry = parsed
                    if b not in pending:
                        continue
                    results[b] = {
                        "business_number": b,
                        "corp_name": entry["corp_name"],
                        "corp_code": entry["corp_code"],
                        "stock_code": entry.get("stock_code") or "",
                    }
                    pending.discard(b)
        except OSError:
            pass


def cmd_export_no_seed_match(ordered_biz: list[str], out_path: Path | None) -> int:
    dest = out_path if out_path is not None else ROOT / "data" / "dart_input_no_seed_match.csv"
    results: dict[str, dict[str, str]] = {}
    pending = set(ordered_biz)
    apply_seed_csv_restoration(results, pending)
    dest.parent.mkdir(parents=True, exist_ok=True)
    note = "마스터·dart_company_registry 에서 파싱 가능한 행 없음 (API 호출 전 0단계 기준)"
    with dest.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(["사업자번호", "비고"])
        for b in ordered_biz:
            if b in pending:
                w.writerow([b, note])
    print(f"=== --export-no-seed-match: {len(pending)}건 → {dest} ===", flush=True)
    return 0


def cmd_scan_status(out_csv: Path) -> int:
    rp = _scan_resume_path(out_csv)
    resume = 0
    if rp.is_file():
        try:
            t = rp.read_text(encoding="utf-8").strip()
            if t.isdigit():
                resume = int(t)
        except OSError:
            pass
    print("=== --scan-status ===", flush=True)
    print(f"재개 인덱스 파일({rp.name}): {resume}", flush=True)
    key = get_dart_api_key()
    if not key:
        print("corp 목록 길이 확인에는 .env 의 DART_API_KEY 가 필요합니다.", flush=True)
        return 0
    print(
        "corpCode 로드 중… (캐시 또는 다운로드 후 XML 파싱으로 수십 초~수 분 걸릴 수 있음)",
        flush=True,
    )
    corp = load_corp_list(key)
    corp.sort(key=lambda r: (0 if _corp_has_listed_stock(r) else 1, r.get("corp_name") or ""))
    n = len(corp)
    remaining = max(0, n - resume)
    print(f"공시대상 corp 정렬 목록 길이: {n}", flush=True)
    print(f"정렬 목록 기준 인덱스 {resume}부터 남은 원소 수: {remaining}", flush=True)
    if resume >= n:
        print(
            "→ 인덱스가 목록 길이 이상이면 --resume-scan 시 2단계 API 호출 없이 끝날 수 있습니다.",
            flush=True,
        )
        print(
            "  corp 목록 최신 분량 확인: "
            "`python build_dart_registry.py --fresh-corpcache --scan-status` "
            "(또는 DART_CORPCODE_IGNORE_CACHE=1)",
            flush=True,
        )
    return 0


def read_biz_lines(path: Path) -> list[str]:
    if not path.is_file():
        print("없음:", path)
        print("샘플:", DEFAULT_INPUT.name, "파일을 만들고 사업자번호를 한 줄에 하나씩 넣으세요.")
        return []
    out: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        out.append(s)
    return out


def _corp_has_listed_stock(row: dict) -> bool:
    sc = re.sub(r"\D", "", (row.get("stock_code") or "").strip())
    return len(sc) == 6 and sc != "000000"


def scan_corpus_for_biz(
    api_key: str,
    pending: set[str],
    max_calls: int,
    sleep_s: float = 0.03,
    *,
    scan_start_index: int = 0,
    checkpoint_path: Path | None = None,
    checkpoint_ordered: list[str] | None = None,
    checkpoint_results: dict[str, dict[str, str]] | None = None,
    checkpoint_every: int = 400,
) -> dict[str, dict[str, str]]:
    """고유번호 목록을 순회하며 company.json 의 bizr_no 가 pending 과 일치하면 기록."""
    found: dict[str, dict[str, str]] = {}
    if not pending:
        return found, 0

    print(
        f"corpCode 목록 다운로드 후 스캔… (남은 사업자번호 {len(pending)}건, API 상한 {max_calls}회, "
        f"시작 인덱스 {scan_start_index})",
        flush=True,
    )
    corp = load_corp_list(api_key)
    corp.sort(key=lambda r: (0 if _corp_has_listed_stock(r) else 1, r.get("corp_name") or ""))

    resume_file = _scan_resume_path(checkpoint_path) if checkpoint_path else None

    calls = 0
    resume_next_index = scan_start_index
    for i, c in enumerate(corp):
        if i < scan_start_index:
            continue
        if not pending:
            break
        if calls >= max_calls:
            break
        cc = (c.get("corp_code") or "").strip()
        if not cc:
            resume_next_index = i + 1
            continue
        prof = fetch_company_json(cc, api_key)
        calls += 1
        resume_next_index = i + 1
        if calls % 100 == 0:
            print(f"  …스캔 진행 API {calls}회, 매칭 {len(found)}건, 미해결 {len(pending)}건", flush=True)

        if not prof:
            time.sleep(sleep_s)
            continue
        b = normalize_biz_number(str(prof.get("bizr_no") or ""))
        if len(b) == 10 and b in pending:
            found[b] = {
                "business_number": b,
                "corp_name": (prof.get("corp_name") or "").strip(),
                "corp_code": cc,
                "stock_code": (str(prof.get("stock_code") or "").strip()),
            }
            pending.discard(b)
            if checkpoint_results is not None:
                checkpoint_results[b] = found[b]
            print("매칭", b, found[b]["corp_name"], cc, found[b]["stock_code"], flush=True)
        time.sleep(sleep_s)

        if (
            checkpoint_path
            and checkpoint_ordered is not None
            and checkpoint_results is not None
            and checkpoint_every > 0
            and calls % checkpoint_every == 0
        ):
            n = write_master_csv(checkpoint_path, checkpoint_ordered, checkpoint_results)
            print(f"  …중간 저장: {checkpoint_path.name} ({n}행)", flush=True)
            if resume_file is not None:
                try:
                    resume_file.write_text(str(resume_next_index), encoding="ascii")
                except OSError:
                    pass

    if pending:
        cap_hit = calls >= max_calls
        reason = (
            "일일 상한·max-scan 도달"
            if cap_hit
            else "corp 목록을 시작 인덱스부터 끝까지 탐색 완료"
        )
        print(
            f"스캔 종료: 미매칭 {len(pending)}건 (API {calls}회, {reason}). "
            "미공시·비법인 등은 수동 CSV 추가가 필요할 수 있습니다.",
            flush=True,
        )
        if resume_file is not None:
            try:
                cap = min(resume_next_index, len(corp))
                resume_file.write_text(str(cap), encoding="ascii")
                print(
                    f"  …이어하기: python build_dart_registry.py --resume-scan "
                    f"(또는 --scan-start-index {cap})",
                    flush=True,
                )
            except OSError:
                pass
    else:
        if resume_file is not None:
            try:
                resume_file.unlink(missing_ok=True)
            except OSError:
                pass

    return found, calls


def _list_incomplete_rows(out_path: Path, ordered_biz: list[str]) -> None:
    """마스터 CSV + 입력 순서 기준으로 종목코드가 비어 있는 사업자번호만 출력."""
    by_biz: dict[str, dict[str, str]] = {}
    if out_path.is_file():
        try:
            with out_path.open(encoding="utf-8-sig", newline="") as f:
                for row in csv.DictReader(f):
                    b = normalize_biz_number(row.get("사업자번호") or row.get("business_number") or "")
                    if len(b) != 10:
                        continue
                    by_biz[b] = {
                        "업체명": (row.get("업체명") or row.get("corp_name") or "").strip(),
                        "종목코드": (row.get("종목코드") or row.get("stock_code") or "").strip(),
                    }
        except OSError as e:
            print("마스터 CSV 읽기 실패:", e, flush=True)
            return
    missing_input: list[str] = []
    for b in ordered_biz:
        r = by_biz.get(b)
        if not r or not (r.get("종목코드") or "").strip():
            missing_input.append(b)
    only_in_file_empty = sum(
        1 for b, r in by_biz.items() if not (r.get("종목코드") or "").strip()
    )
    print(
        f"=== --list-incomplete: 입력 {len(ordered_biz)}건 중 종목코드 없음 {len(missing_input)}건 ===",
        flush=True,
    )
    print(f"(마스터 파일 내 종목코드 빈 행: {only_in_file_empty}건)", flush=True)
    for b in missing_input[:500]:
        nm = (by_biz.get(b) or {}).get("업체명") or ""
        print(b, nm, sep="\t", flush=True)
    if len(missing_input) > 500:
        print(f"... 외 {len(missing_input) - 500}건 생략", flush=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--input",
        type=Path,
        default=DEFAULT_INPUT,
        help="사업자번호 목록 텍스트 경로",
    )
    ap.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help="생성할 CSV 경로",
    )
    ap.add_argument(
        "--max-scan",
        type=int,
        default=None,
        help="2단계 corp 스캔 company.json 최대 호출 수. 미지정 시 환경변수 DART_BUILD_MAX_SCAN(기본 40000)",
    )
    ap.add_argument(
        "--list-incomplete",
        action="store_true",
        help="API 없이 마스터 CSV+입력 기준 종목코드가 비어 있는 사업자번호만 출력하고 종료",
    )
    ap.add_argument(
        "--scan-start-index",
        type=int,
        default=None,
        metavar="N",
        help="2단계 corp 목록 스캔을 인덱스 N부터 시작(중단 후 이어하기). --resume-scan 보다 우선",
    )
    ap.add_argument(
        "--resume-scan",
        action="store_true",
        help=f"2단계 스캔을 출력 CSV와 같은 폴더의 {SCAN_RESUME_FILENAME} 에 저장된 인덱스부터 이어서",
    )
    ap.add_argument(
        "--scan-status",
        action="store_true",
        help="corp 정렬 목록 길이와 재개 인덱스 대비 남은 구간 요약 후 종료(API 필요)",
    )
    ap.add_argument(
        "--export-no-seed-match",
        action="store_true",
        help="마스터·레지스트리 CSV만으로 복원 안 되는 입력 사업자번호만 CSV로 저장(API 없음)",
    )
    ap.add_argument(
        "--export-no-seed-match-out",
        type=Path,
        default=None,
        metavar="PATH",
        help="--export-no-seed-match 출력 경로 (기본 data/dart_input_no_seed_match.csv)",
    )
    ap.add_argument(
        "--fresh-corpcache",
        action="store_true",
        help="corpCode.zip 캐시 무시 후 재다운로드(목록 증가 확인 등)",
    )
    args = ap.parse_args()

    if args.fresh_corpcache:
        os.environ["DART_CORPCODE_IGNORE_CACHE"] = "1"

    if args.scan_status:
        return cmd_scan_status(args.output)

    lines = read_biz_lines(args.input)
    if not lines:
        return 1

    ordered_biz: list[str] = []
    for raw in lines:
        biz = normalize_biz_number(raw)
        if len(biz) != 10:
            print("건너뜀(10자리 아님):", raw)
            continue
        ordered_biz.append(biz)

    if not ordered_biz:
        return 1

    if args.list_incomplete:
        _list_incomplete_rows(args.output, ordered_biz)
        return 0

    if args.export_no_seed_match:
        return cmd_export_no_seed_match(ordered_biz, args.export_no_seed_match_out)

    key = get_dart_api_key()
    if not key:
        print("DART_API_KEY 가 .env 또는 환경변수에 없습니다.")
        return 1

    print_last_run_summary()
    print("=== 종목코드가 중간부터 비는 경우(요약) ===", flush=True)
    for line in STOCK_CODE_GAP_CAUSES_KO.strip().splitlines()[:6]:
        print(line, flush=True)
    print("  (전문: services/dart_build_history.py 의 STOCK_CODE_GAP_CAUSES_KO 참고)", flush=True)

    daily_cap = default_daily_call_cap()
    margin = int(os.environ.get("DART_BUILD_API_MARGIN") or "300")
    stage2_max_user = args.max_scan if args.max_scan is not None else default_stage2_max_scan()

    print(
        f"=== 입력 유효 사업자번호 {len(ordered_biz)}건 → {args.output} ===",
        flush=True,
    )
    print(
        f"=== 일일 API 상한(DART_BUILD_DAILY_CALL_CAP)={daily_cap}, 2단계 요청 상한={stage2_max_user}, 여유={margin} ===",
        flush=True,
    )

    results: dict[str, dict[str, str]] = {}
    pending: set[str] = set(ordered_biz)
    out_path = args.output

    scan_start_index = 0
    if args.scan_start_index is not None:
        scan_start_index = max(0, int(args.scan_start_index))
    elif args.resume_scan:
        rp = _scan_resume_path(out_path)
        if rp.is_file():
            try:
                t = rp.read_text(encoding="utf-8").strip()
                if t.isdigit():
                    scan_start_index = int(t)
            except OSError:
                scan_start_index = 0
        print(f"=== --resume-scan: corp 시작 인덱스 {scan_start_index} ({rp.name}) ===", flush=True)

    apply_seed_csv_restoration(results, pending)
    if results:
        print(f"=== 0단계: 기존 마스터/레지스트리에서 {len(results)}건 복원 ===", flush=True)

    print("=== 1단계: bizr_no 직접 조회 ===", flush=True)
    s1_total = len(pending)
    s1_api_calls = 0
    for s1_i, biz in enumerate(list(pending), start=1):
        if s1_i == 1 or s1_i % 25 == 0:
            print(
                f"  …1단계 진행 {s1_i}/{s1_total} (누적 매칭 {len(results)}건, 미처리 {len(pending)}건)",
                flush=True,
            )
        js = fetch_company_json_by_bizr_no(biz, key)
        s1_api_calls += 1
        time.sleep(0.12)
        if not js:
            continue
        cc = str(js.get("corp_code") or "").strip()
        if not cc:
            continue
        results[biz] = {
            "business_number": biz,
            "corp_name": (js.get("corp_name") or "").strip(),
            "corp_code": cc,
            "stock_code": (str(js.get("stock_code") or "").strip()),
        }
        pending.discard(biz)
        print("OK(bizr)", biz, results[biz]["corp_name"], cc, flush=True)

    rows_written = write_master_csv(out_path, ordered_biz, results)
    print(f"=== 1단계 후 중간 저장: {rows_written}행 → {out_path} ===", flush=True)

    s2_api_calls = 0
    effective_stage2 = min(stage2_max_user, max(0, daily_cap - s1_api_calls - margin))
    if effective_stage2 < stage2_max_user:
        print(
            f"=== 2단계 스캔 상한 조정: 요청 {stage2_max_user} → 일일 cap·1단계 반영 후 {effective_stage2} ===",
            flush=True,
        )
    if pending and effective_stage2 <= 0:
        print(
            "=== 경고: DART_BUILD_DAILY_CALL_CAP 에서 1단계·여유를 뺀 뒤 2단계에 남은 호출이 없습니다. "
            "내일 한도 리셋 후 python build_dart_registry.py --resume-scan ===",
            flush=True,
        )
    elif pending:
        print(f"=== 2단계: 고유번호 목록 스캔 (남음 {len(pending)}건) ===", flush=True)
        more, s2_api_calls = scan_corpus_for_biz(
            key,
            pending,
            max_calls=effective_stage2,
            sleep_s=0.03,
            scan_start_index=scan_start_index,
            checkpoint_path=out_path,
            checkpoint_ordered=ordered_biz,
            checkpoint_results=results,
            checkpoint_every=400,
        )
        results.update(more)

    rows_written = write_master_csv(out_path, ordered_biz, results)

    still = [b for b in ordered_biz if b not in results]
    print("=== 완료 ===", out_path, flush=True)
    print("기록 행 수:", rows_written, "/ 입력 유효 건수:", len(ordered_biz), flush=True)
    if still:
        print("미기록 건수:", len(still), "(처음 20개)", still[:20], flush=True)

    rp_next = _scan_resume_path(out_path)
    next_idx: int | None = None
    if rp_next.is_file():
        try:
            t = rp_next.read_text(encoding="utf-8").strip()
            if t.isdigit():
                next_idx = int(t)
        except OSError:
            pass

    exit_note = "정상 종료" if not still else f"미매칭 {len(still)}건 남음 — 내일 --resume-scan 권장"
    save_history(
        {
            "matched_rows": rows_written,
            "input_valid": len(ordered_biz),
            "still_unmatched": len(still),
            "stage1_api_calls": s1_api_calls,
            "stage2_api_calls": s2_api_calls,
            "daily_cap": daily_cap,
            "stage2_max_calls_effective": effective_stage2 if pending or s2_api_calls else 0,
            "next_corp_scan_index": next_idx,
            "output_csv": str(out_path),
            "input_txt": str(args.input.resolve()),
            "exit_note": exit_note,
        }
    )
    print("=== 이력 저장:", (ROOT / "data" / "dart_build_history.json").resolve(), "===", flush=True)

    return 0 if rows_written > 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
