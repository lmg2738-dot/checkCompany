# -*- coding: utf-8 -*-
"""로컬 웹 서버: 고객 리스트 기반 이탈·이슈 위험 대시보드."""

from __future__ import annotations

import csv
import io
import json as json_std
import os
import time
from pathlib import Path
from typing import Any, Iterator

try:
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).resolve().parent / ".env")
except ImportError:
    pass

from flask import Flask, Response, jsonify, render_template, request, stream_with_context

from customer_analyzer import analyze_customer
from services.dart_service import (
    DART_BIZ_MASTER_CSV,
    DART_BIZNO_INPUT_LIST,
    dart_bizno_input_customer_rows,
    load_biz_registry,
    load_bizno_input_numbers,
    normalize_disclosure_corp_code,
)
from services.news_rss import normalize_biz_number

BASE = Path(__file__).resolve().parent

# 서버리스(Vercel) 번들에서도 템플릿·정적 경로를 확실히 잡기 위해 절대 경로 지정
app = Flask(
    __name__,
    template_folder=str(BASE / "templates"),
    static_folder=str(BASE / "static"),
    static_url_path="/static",
)
app.config["MAX_CONTENT_LENGTH"] = 2 * 1024 * 1024  # 2MB

_SAMPLE_CSV_PATH = BASE / "data" / "sample_customers.csv"


def load_default_sample_csv() -> str:
    if _SAMPLE_CSV_PATH.is_file():
        return _SAMPLE_CSV_PATH.read_text(encoding="utf-8-sig")
    return ""


def initial_csv_text_for_get() -> str:
    """첫 화면: 마스터 CSV → 입력순+레지스트리 조합 → 사업자만 → 예시 CSV."""
    if DART_BIZ_MASTER_CSV.is_file():
        return DART_BIZ_MASTER_CSV.read_text(encoding="utf-8-sig")
    nums = load_bizno_input_numbers()
    reg = load_biz_registry()
    if nums and reg:
        buf = io.StringIO()
        w = csv.writer(buf, lineterminator="\n")
        w.writerow(["사업자번호", "업체명", "공시번호", "종목코드"])
        for b in nums:
            h = reg.get(b)
            if h:
                w.writerow([b, h.get("corp_name") or "", h.get("corp_code") or "", h.get("stock_code") or ""])
            else:
                w.writerow([b, "", "", ""])
        return buf.getvalue()
    if nums:
        return "사업자번호,업체명,공시번호,종목코드\n" + "\n".join(f"{b},,," for b in nums)
    return load_default_sample_csv()


def _norm_key(k: str) -> str:
    return (k or "").strip().lower().replace("\ufeff", "")


def _pick(row: dict, *names: str) -> str:
    m = {_norm_key(k): v for k, v in row.items()}
    for n in names:
        if _norm_key(n) in m and (m[_norm_key(n)] or "").strip():
            return str(m[_norm_key(n)]).strip()
    return ""


def parse_csv_text(text: str) -> list[dict]:
    """CSV 텍스트 → {company_name, business_number, ticker_override} 리스트."""
    text = text.strip()
    if not text:
        return []
    f = io.StringIO(text)
    reader = csv.DictReader(f)
    rows = []
    for raw in reader:
        name = _pick(raw, "업체명", "고객사명", "회사명", "company_name", "name")
        biz = _pick(raw, "사업자번호", "사업자등록번호", "business_number", "biz", "사업자")
        tick = _pick(raw, "종목코드", "ticker", "stock_code", "상장코드")
        dart_cc_raw = _pick(raw, "공시번호", "고유번호", "corp_code", "dart_corp_code", "DART고유번호")
        dart_cc = normalize_disclosure_corp_code(dart_cc_raw) or None
        biz_norm = normalize_biz_number(biz)
        if biz and (name or len(biz_norm) == 10):
            rows.append(
                {
                    "company_name": name,
                    "business_number": biz,
                    "ticker_override": tick or None,
                    "dart_corp_code": dart_cc,
                }
            )
    return rows


def csv_sheet_preview(csv_text: str, max_display_rows: int = 50) -> dict[str, Any]:
    """화면 표 미리보기용: 헤더(첫 행) + 본문 상한 높이에서 스크롤."""
    t = (csv_text or "").strip()
    meta: dict[str, Any] = {
        "empty": True,
        "headers": [],
        "rows": [],
        "overflow": False,
    }
    if not t:
        return meta
    meta["empty"] = False
    reader = csv.reader(io.StringIO(t))
    rows = list(reader)
    if not rows:
        return meta
    headers = [(str(c) if c is not None else "").strip() for c in rows[0]]
    body = rows[1:]
    overflow = len(body) > max_display_rows
    body = body[:max_display_rows]
    nh = len(headers)
    padded: list[list[str]] = []
    for raw in body:
        cells = [(str(x) if x is not None else "").strip() for x in raw]
        if nh:
            cells = (cells + [""] * nh)[:nh]
        padded.append(cells)
    meta["headers"] = headers
    meta["rows"] = padded
    meta["overflow"] = overflow
    return meta


def dart_template_path_vars() -> dict[str, str]:
    return {
        "dart_bizno_rel": str(DART_BIZNO_INPUT_LIST.relative_to(BASE)).replace("\\", "/"),
        "dart_master_rel": str(DART_BIZ_MASTER_CSV.relative_to(BASE)).replace("\\", "/"),
    }


def sorted_results_for_template(rows: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    return sorted(rows or [], key=lambda r: float(r.get("composite_score") or 0), reverse=True)


def analysis_results_fragment_kwargs(results_payload: list[dict[str, Any]]) -> dict[str, Any]:
    results_with_gaps = [r for r in results_payload if not r.get("ticker_available")]
    return {
        **dart_template_path_vars(),
        "sorted_results": sorted_results_for_template(results_payload),
        "results": results_payload,
        "results_ticker_missing_count": len(results_with_gaps),
    }


@app.route("/", methods=["GET", "POST"])
def index():
    results = []
    err = None
    raw_csv = initial_csv_text_for_get() if request.method == "GET" else ""

    if request.method == "POST":
        raw_csv = request.form.get("csv_text") or ""

        if not err:
            try:
                customers = parse_csv_text(raw_csv)
                if not customers:
                    err = "유효한 행이 없습니다. 사업자번호(10자리) 또는 업체명+사업자번호 컬럼을 확인하세요."
                if not err:
                    gap = float(os.environ.get("DART_ANALYZE_SLEEP_SEC") or "0")
                    for i, c in enumerate(customers):
                        if i and gap > 0:
                            time.sleep(gap)
                        results.append(
                            analyze_customer(
                                c.get("company_name") or "",
                                c.get("business_number") or "",
                                c.get("ticker_override"),
                                c.get("dart_corp_code"),
                            )
                        )
            except Exception as e:
                err = str(e)

    csv_preview = csv_sheet_preview(raw_csv)
    ticker_missing_ct = len([r for r in results if not r.get("ticker_available")])

    ctx = dart_template_path_vars()
    sorted_rows = sorted_results_for_template(results)
    return render_template(
        "index.html",
        results=results,
        sorted_results=sorted_rows,
        results_ticker_missing_count=ticker_missing_ct,
        error=err,
        csv_text=raw_csv,
        csv_preview=csv_preview,
        **ctx,
        formula_summary={
            "weights": "S = 0.20·E + 0.30·N + 0.25·M + 0.25·C",
            "churn_prob": "P = σ(-2 + 4·S),  σ(x)=1/(1+e^{-x})",
        },
    )


@app.route("/api/analyze", methods=["POST"])
def api_analyze():
    payload = request.get_json(force=True, silent=True) or {}
    if payload.get("source") == "dart_bizno_input_list" or payload.get("use_dart_bizno_list"):
        rows = dart_bizno_input_customer_rows()
    else:
        rows = payload.get("customers") or []
    out = []
    gap = float(os.environ.get("DART_ANALYZE_SLEEP_SEC") or "0")
    for i, r in enumerate(rows):
        if i and gap > 0:
            time.sleep(gap)
        out.append(
            analyze_customer(
                str(r.get("company_name", "")),
                str(r.get("business_number", "")),
                r.get("ticker_override") or r.get("ticker"),
                normalize_disclosure_corp_code(
                    str(r.get("dart_corp_code") or r.get("공시번호") or r.get("corp_code") or "")
                )
                or None,
            )
        )

    def ser(x):
        if hasattr(x, "__dataclass_fields__"):
            from dataclasses import asdict

            return asdict(x)
        return x

    clean = []
    for o in out:
        est = o.get("estimate")
        clean.append(
            {
                **{k: v for k, v in o.items() if k != "estimate"},
                "estimate": ser(est) if est else None,
            }
        )
    return jsonify({"results": clean})


@app.route("/api/analyze_stream", methods=["POST"])
def analyze_stream():
    """NDJSON 줄 단위 진행(progress) 후 마지막에 렌더된 결과 HTML(fragment) 포함."""
    body = request.get_json(force=True, silent=True) or {}
    raw = body.get("csv_text")
    csv_text = raw if isinstance(raw, str) else ""

    def chunks() -> Iterator[bytes]:
        try:
            customers = parse_csv_text(csv_text.strip())
            if not customers:
                payload = {
                    "type": "error",
                    "message": "유효한 행이 없습니다. 사업자번호(10자리) 또는 업체명+사업자번호 컬럼을 확인하세요.",
                }
                yield (json_std.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")
                return

            gap = float(os.environ.get("DART_ANALYZE_SLEEP_SEC") or "0")
            n = len(customers)
            t0 = time.perf_counter()
            results_payload: list[dict[str, Any]] = []

            for i, c in enumerate(customers):
                if i and gap > 0:
                    time.sleep(gap)
                try:
                    row = analyze_customer(
                        c.get("company_name") or "",
                        c.get("business_number") or "",
                        c.get("ticker_override"),
                        c.get("dart_corp_code"),
                    )
                    results_payload.append(row)
                except Exception as e:
                    err = {"type": "error", "message": f"{i + 1}번째 행 분석 오류: {e}"}
                    yield (json_std.dumps(err, ensure_ascii=False) + "\n").encode("utf-8")
                    return

                elapsed = time.perf_counter() - t0
                cur = len(results_payload)
                rate = elapsed / cur if cur else 0.0
                eta_sec = max(0.0, rate * (n - cur))
                prog = {
                    "type": "progress",
                    "current": cur,
                    "total": n,
                    "fraction": cur / n if n else 1.0,
                    "pct": round(100.0 * cur / n, 1) if n else 100.0,
                    "elapsed_sec": round(elapsed, 1),
                    "eta_sec": round(eta_sec, 1),
                }
                yield (json_std.dumps(prog, ensure_ascii=False) + "\n").encode("utf-8")

            frag_ctx = analysis_results_fragment_kwargs(results_payload)
            fragment = render_template("partials/analysis_results.html", **frag_ctx)
            done_payload = {"type": "done", "fragment": fragment}
            yield (json_std.dumps(done_payload, ensure_ascii=False) + "\n").encode("utf-8")

        except Exception as e:
            err = {"type": "error", "message": str(e)}
            yield (json_std.dumps(err, ensure_ascii=False) + "\n").encode("utf-8")

    return Response(
        stream_with_context(chunks()),
        mimetype="application/x-ndjson; charset=utf-8",
        headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
    )


def main():
    import errno
    import sys

    host = (os.environ.get("FLASK_HOST") or "127.0.0.1").strip() or "127.0.0.1"
    try:
        port = int(os.environ.get("FLASK_PORT") or "5058")
    except ValueError:
        port = 5058

    dbg_raw = (os.environ.get("FLASK_DEBUG") or "1").strip().lower()
    debug = dbg_raw not in ("0", "false", "no")

    env_reload = os.environ.get("FLASK_USE_RELOADER", "").strip().lower()
    force_reload = env_reload in ("1", "true", "yes")
    # Windows: debug 리자동 리로더가 IDE/포트 재점유 문제를 자주 만듭니다. 필요 시 FLASK_USE_RELOADER=1
    if debug and not sys.platform.startswith("win"):
        use_reloader = True
    elif debug and sys.platform.startswith("win") and force_reload:
        use_reloader = True
    else:
        use_reloader = False

    print(f"브라우저에서 http://{host}:{port} 열기")
    print("DART 상장 매칭·공시 보강: 환경변수 DART_API_KEY 설정 권장 (오픈다트 무료 발급)")
    try:
        app.run(host=host, port=port, debug=debug, use_reloader=use_reloader)
    except OSError as e:
        eno = getattr(e, "errno", None)
        winerr = getattr(e, "winerror", None)
        if eno == errno.EADDRINUSE or winerr == 10048:
            print(
                f"포트 {port} 가 이미 사용 중입니다. 다른 터미널의 서버를 종료하거나 "
                f"FLASK_PORT=5051 환경변수로 포트를 바꾸세요.",
                file=sys.stderr,
            )
        raise


if __name__ == "__main__":
    main()
