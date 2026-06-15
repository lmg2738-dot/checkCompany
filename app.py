# -*- coding: utf-8 -*-
"""로컬 웹 서버: 고객 리스트 기반 이탈·이슈 위험 대시보드."""

from __future__ import annotations

import csv
import io
import json as json_std
import os
import time
from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Iterator

from services.env_config import bootstrap_env

bootstrap_env()

from flask import Flask, Response, jsonify, render_template, request, stream_with_context

from churn_model import composite_weights_formula
from customer_analyzer import analyze_customer
from services.analysis_context import analyze_runtime, set_thread_bulk_analyze
from services.dart_service import (
    DART_BIZ_MASTER_CSV,
    DART_BIZNO_INPUT_LIST,
    dart_bizno_input_customer_rows,
    load_biz_registry,
    load_bizno_input_numbers,
    normalize_disclosure_corp_code,
)
from services.news_rss import normalize_biz_number

try:
    from services import supabase_customers
except ImportError:
    supabase_customers = None  # type: ignore

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


def _supabase_ready() -> bool:
    return supabase_customers is not None and supabase_customers.is_configured()


def customer_source_context() -> dict[str, Any]:
    if _supabase_ready():
        ctx: dict[str, Any] = {
            "supabase_configured": True,
            "customer_data_source": "Supabase · customers 테이블",
        }
        try:
            ctx["missing_disclosure_count"] = supabase_customers.count_missing_disclosure()
        except Exception:
            ctx["missing_disclosure_count"] = None
    else:
        ctx = {
            "supabase_configured": False,
            "customer_data_source": f"로컬 · {DART_BIZ_MASTER_CSV.name}",
            "missing_disclosure_count": None,
        }
    try:
        from services.resend_client import config_status

        ctx["resend"] = config_status()
        ctx["resend_ready"] = bool(ctx["resend"].get("resend_ready"))
    except Exception:
        ctx["resend_ready"] = False
    return ctx


def _ndjson_line(payload: dict[str, Any]) -> bytes:
    return (json_std.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")


def _ndjson_stream_headers() -> dict[str, str]:
    return {
        "Cache-Control": "no-store, no-transform",
        "X-Accel-Buffering": "no",
        "X-Content-Type-Options": "nosniff",
    }


def _iter_risk_email_job_events() -> Iterator[dict[str, Any]]:
    """화면 메일발송·Vercel Cron 공통 — 분석 후 Resend 발송 이벤트."""
    from services.daily_risk_email import email_subject, send_daily_risk_report
    from services.resend_client import resend_configured

    yield {
        "type": "progress",
        "phase": "connecting",
        "current": 0,
        "total": 0,
        "fraction": 0,
        "pct": 0,
        "elapsed_sec": 0,
        "eta_sec": None,
        "message": "시도 중… 서버에 연결합니다.",
    }

    if not resend_configured():
        yield {
            "type": "error",
            "message": "Resend 가 설정되지 않았습니다. (RESEND_API_KEY, RESEND_FROM, RESEND_TO)",
        }
        return

    yield {"type": "info", "message": "시도 중… 분석 대상 고객 목록을 준비합니다."}

    customers, prep = prepare_customers_for_analysis("")
    if not customers:
        msg = "공시번호(8자리)가 있는 고객이 없어 메일을 보낼 수 없습니다."
        if prep.get("total_raw"):
            msg += (
                f" (전체 {prep['total_raw']}건, "
                f"제외 {prep.get('skipped_no_disclosure', 0)}건)"
            )
        yield {"type": "error", "message": msg}
        return

    if prep.get("skipped_no_disclosure"):
        yield {
            "type": "info",
            "message": (
                f"공시번호 없음 {prep['skipped_no_disclosure']}건 제외 · "
                f"분석 {prep.get('count', 0)}건"
            ),
        }

    n = len(customers)
    t0 = time.perf_counter()
    gap = float(os.environ.get("DART_ANALYZE_SLEEP_SEC") or "0")
    workers = _bulk_analyze_workers()
    load_biz_registry()
    results_payload: list[dict[str, Any] | None] = [None] * n

    def progress_event(done: int) -> dict[str, Any]:
        elapsed = time.perf_counter() - t0
        rate = elapsed / done if done else 0.0
        eta_sec = max(0.0, rate * (n - done))
        return {
            "type": "progress",
            "phase": "analyzing",
            "current": done,
            "total": n,
            "fraction": done / n if n else 1.0,
            "pct": round(100.0 * done / n, 1) if n else 100.0,
            "elapsed_sec": round(elapsed, 1),
            "eta_sec": round(eta_sec, 1),
            "message": f"위험 분석 중: {done} / {n}건",
        }

    with analyze_runtime():
        if n <= 2 or workers <= 1:
            for i, c in enumerate(customers):
                if i and gap > 0:
                    time.sleep(gap)
                try:
                    results_payload[i] = _analyze_customer_row(c)
                except Exception as e:
                    yield {
                        "type": "error",
                        "message": f"{i + 1}번째 행 분석 오류: {e}",
                    }
                    return
                yield progress_event(i + 1)
        else:
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures = {
                    pool.submit(_analyze_customer_row, c): i
                    for i, c in enumerate(customers)
                }
                done = 0
                for fut in as_completed(futures):
                    i = futures[fut]
                    try:
                        results_payload[i] = fut.result()
                    except Exception as e:
                        yield {
                            "type": "error",
                            "message": f"{i + 1}번째 행 분석 오류: {e}",
                        }
                        return
                    done += 1
                    yield progress_event(done)

    results_payload = [r for r in results_payload if r is not None]
    sorted_rows = sorted_results_for_template(results_payload)

    yield {
        "type": "info",
        "message": f"분석 완료 · 위험 상위 {min(10, len(sorted_rows))}개 메일 발송 중…",
    }
    yield {
        "type": "progress",
        "phase": "sending",
        "current": n,
        "total": n,
        "fraction": 1.0,
        "pct": 100.0,
        "elapsed_sec": round(time.perf_counter() - t0, 1),
        "eta_sec": 0,
        "message": "Resend 로 메일을 보내는 중…",
    }

    mail = send_daily_risk_report(sorted_rows, prep_meta=prep)
    if mail.get("skipped"):
        yield {
            "type": "error",
            "message": f"메일 발송 생략됨: {mail.get('reason', 'DAILY_EMAIL_ENABLED')}",
        }
        return
    if mail.get("ok") is False:
        yield {
            "type": "error",
            "message": mail.get("reason") or "메일 발송 실패",
        }
        return

    yield {
        "type": "done",
        "subject": email_subject(),
        "analyzed_count": len(results_payload),
        "emailed_top": min(10, len(sorted_rows)),
        "to": mail.get("to"),
        "resend_email_id": mail.get("resend_email_id"),
        "message": "메일 발송이 완료되었습니다.",
    }


def run_daily_risk_email_job() -> dict[str, Any]:
    """동기 실행(스크립트·테스트용)."""
    collected = collect_risk_email_job_events()
    if not collected["ok"]:
        raise RuntimeError(collected.get("error") or "이메일 발송 실패")
    return {"ok": True, **(collected.get("result") or {})}


def collect_risk_email_job_events() -> dict[str, Any]:
    """메일발송·Make·동기 Cron 공통 — 이벤트 수집."""
    events: list[dict[str, Any]] = []
    for ev in _iter_risk_email_job_events():
        events.append(ev)
        if ev.get("type") in ("error", "done"):
            break
    last = events[-1] if events else {}
    if last.get("type") == "done":
        return {"ok": True, "events": events, "result": last}
    if last.get("type") == "error":
        return {
            "ok": False,
            "events": events,
            "error": str(last.get("message") or "이메일 발송 실패"),
        }
    return {"ok": False, "events": events, "error": "이메일 작업이 완료되지 않았습니다."}


def _jobs_daily_email_urls() -> dict[str, str]:
    base = (os.environ.get("DASHBOARD_URL") or request.url_root or "").strip()
    if not base.endswith("/"):
        base += "/"
    path = "jobs/daily-risk-email"
    run_json = f"{base}{path}?run=1&format=json"
    run_html = f"{base}{path}?run=1"
    return {
        "info_url": f"{base}{path}",
        "run_json_url": run_json,
        "run_html_url": run_html,
    }


def csv_preview_fragment(csv_text: str) -> str:
    preview = csv_sheet_preview(csv_text)
    if preview.get("empty"):
        return ""
    return render_template(
        "partials/csv_sheet_preview.html",
        csv_preview=preview,
    )


def _filter_disclosure_fallback(
    customers: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], int]:
    ok: list[dict[str, Any]] = []
    skipped = 0
    for c in customers:
        cc = normalize_disclosure_corp_code(c.get("dart_corp_code") or "") or ""
        if len(cc) == 8:
            ok.append({**c, "dart_corp_code": cc})
        else:
            skipped += 1
    return ok, skipped


def prepare_customers_for_analysis(csv_text: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Supabase 우선 · 공시번호 8자리 없는 행은 분석 제외."""
    meta: dict[str, Any] = {
        "source": "csv",
        "total_raw": 0,
        "skipped_no_disclosure": 0,
        "count": 0,
    }
    if _supabase_ready():
        meta["source"] = "supabase"
        mapped = supabase_customers.customers_as_analyze_dicts()
    else:
        mapped = parse_csv_text((csv_text or "").strip())
    meta["total_raw"] = len(mapped)
    if supabase_customers is not None:
        good, skipped = supabase_customers.filter_customers_with_disclosure(mapped)
    else:
        good, skipped = _filter_disclosure_fallback(mapped)
    meta["skipped_no_disclosure"] = skipped
    meta["count"] = len(good)
    return good, meta


def initial_csv_text_for_get() -> str:
    """첫 화면: Supabase → 마스터 CSV → 입력순+레지스트리 → 예시 CSV."""
    if _supabase_ready():
        try:
            return supabase_customers.customers_to_csv_text()
        except Exception:
            pass
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


def _bulk_analyze_workers() -> int:
    try:
        n = int(os.environ.get("ANALYZE_BULK_WORKERS", "6"))
    except ValueError:
        n = 6
    return max(1, min(n, 12))


def _analyze_customer_row(customer: dict[str, Any]) -> dict[str, Any]:
    set_thread_bulk_analyze(True)
    try:
        return analyze_customer(
            customer.get("company_name") or "",
            customer.get("business_number") or "",
            customer.get("ticker_override"),
            customer.get("dart_corp_code"),
        )
    finally:
        set_thread_bulk_analyze(False)


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


def csv_sheet_preview(csv_text: str) -> dict[str, Any]:
    """화면 표 미리보기용: 헤더(첫 행) + 데이터 행 전체(스크롤 영역)."""
    t = (csv_text or "").strip()
    meta: dict[str, Any] = {
        "empty": True,
        "headers": [],
        "rows": [],
        "row_count": 0,
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
    nh = len(headers)
    padded: list[list[str]] = []
    for raw in body:
        cells = [(str(x) if x is not None else "").strip() for x in raw]
        if nh:
            cells = (cells + [""] * nh)[:nh]
        padded.append(cells)
    meta["headers"] = headers
    meta["rows"] = padded
    meta["row_count"] = len(padded)
    return meta


def dart_template_path_vars() -> dict[str, str]:
    return {
        "dart_bizno_rel": str(DART_BIZNO_INPUT_LIST.relative_to(BASE)).replace("\\", "/"),
        "dart_master_rel": str(DART_BIZ_MASTER_CSV.relative_to(BASE)).replace("\\", "/"),
    }


def sorted_results_for_template(rows: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    return sorted(rows or [], key=lambda r: float(r.get("composite_score") or 0), reverse=True)


def analysis_results_fragment_kwargs(
    results_payload: list[dict[str, Any]],
    prep_meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    results_with_gaps = [r for r in results_payload if not r.get("ticker_available")]
    market_excluded = [
        r for r in results_payload if not r.get("market_in_composite", r.get("ticker_available"))
    ]
    prep = prep_meta or {}
    return {
        **dart_template_path_vars(),
        **customer_source_context(),
        "sorted_results": sorted_results_for_template(results_payload),
        "results": results_payload,
        "results_ticker_missing_count": len(results_with_gaps),
        "results_market_excluded_count": len(market_excluded),
        "analysis_prep": prep,
        "composite_weights_with_m": composite_weights_formula(include_market=True),
        "composite_weights_no_m": composite_weights_formula(include_market=False),
    }


def _cron_secret_value() -> str:
    raw = (os.environ.get("CRON_SECRET") or "").strip()
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "\"'":
        raw = raw[1:-1].strip()
    return raw


def _header_get(name: str) -> str:
    """Flask headers + WSGI environ fallback (Vercel rewrite 호환)."""
    v = (request.headers.get(name) or "").strip()
    if v:
        return v
    key = "HTTP_" + name.upper().replace("-", "_")
    return (request.environ.get(key) or "").strip()


def _remote_addr() -> str:
    return (request.environ.get("REMOTE_ADDR") or "").strip()


def _forwarded_client_ip() -> str:
    raw = (
        _header_get("X-Forwarded-For")
        or request.environ.get("HTTP_X_FORWARDED_FOR")
        or ""
    ).strip()
    if not raw:
        return ""
    return raw.split(",")[0].strip()


def _is_likely_external_http_client() -> bool:
    """브라우저·curl 등 공인 URL로 들어온 요청 (Cron 내부 프록시와 구분)."""
    client = _forwarded_client_ip()
    if client and client not in ("127.0.0.1", "::1"):
        return True
    if _remote_addr() not in ("127.0.0.1", "::1", ""):
        return True
    return False


def _is_vercel_cron_invoke() -> bool:
    if _header_get("x-vercel-cron") == "1":
        return True
    if (request.environ.get("HTTP_X_VERCEL_CRON") or "").strip() == "1":
        return True
    if _header_get("x-vercel-cron-schedule"):
        return True
    if (request.environ.get("HTTP_X_VERCEL_CRON_SCHEDULE") or "").strip():
        return True
    ua = (
        _header_get("User-Agent")
        or request.environ.get("HTTP_USER_AGENT")
        or ""
    ).lower()
    if "vercel-cron" in ua:
        return True
    return False


def _is_vercel_internal_cron_proxy() -> bool:
    """
    Vercel 스케줄러가 127.0.0.1 로 호출할 때 Authorization/x-vercel-cron 이
    빠지는 경우(로그: GET /api/cron/daily-risk-email 401, remote 127.0.0.1).
    외부 클라이언트(x-forwarded-for 실 IP)는 제외 — Bearer/쿼리 필요.
    """
    if not os.environ.get("VERCEL"):
        return False
    path = (request.path or "").split("?")[0].rstrip("/")
    if path != "/api/cron/daily-risk-email":
        return False
    if _is_likely_external_http_client():
        return False
    if _is_vercel_cron_invoke():
        return True
    if _remote_addr() in ("127.0.0.1", "::1"):
        # Cron은 Production 배포에서만 실행
        return (os.environ.get("VERCEL_ENV") or "").strip() == "production"
    return False


def _cron_query_secret_ok() -> bool:
    """Make 등 URL 쿼리 ?key= (권장은 Authorization 헤더)."""
    expected = _cron_secret_value()
    if not expected:
        return False
    q = (
        request.args.get("key")
        or request.args.get("cron_secret")
        or request.args.get("secret")
        or ""
    ).strip()
    return bool(q and q == expected)


def _cron_bearer_matches(auth_header: str, expected: str) -> bool:
    auth = (auth_header or "").strip()
    if not auth or not expected:
        return False
    if auth == expected:
        return True
    if auth == f"Bearer {expected}":
        return True
    if auth.lower().startswith("bearer "):
        return auth[7:].strip() == expected
    return False


def _cron_auth_status() -> dict[str, Any]:
    expected = _cron_secret_value()
    auth = _header_get("Authorization") or (
        request.environ.get("HTTP_AUTHORIZATION") or ""
    ).strip()
    bearer_ok = _cron_bearer_matches(auth, expected) if expected else False
    vercel_cron = _is_vercel_cron_invoke()
    internal_proxy = _is_vercel_internal_cron_proxy()
    query_ok = _cron_query_secret_ok()
    # Vercel Cron: Bearer / x-vercel-cron / 내부 127.0.0.1 스케줄 프록시
    authorized = (
        (not expected)
        or bearer_ok
        or vercel_cron
        or internal_proxy
        or query_ok
    )
    return {
        "cron_secret_configured": bool(expected),
        "authorization_header_present": bool(auth),
        "bearer_matches": bearer_ok,
        "query_secret_matches": query_ok,
        "vercel_cron_detected": vercel_cron,
        "vercel_internal_cron_proxy": internal_proxy,
        "remote_addr": _remote_addr(),
        "forwarded_client_ip": _forwarded_client_ip(),
        "external_client": _is_likely_external_http_client(),
        "user_agent": (
            _header_get("User-Agent") or request.environ.get("HTTP_USER_AGENT") or ""
        )[:120],
        "authorized": authorized,
    }


def _cron_authorized() -> bool:
    return bool(_cron_auth_status()["authorized"])


def _cron_unauthorized_response() -> tuple[Any, int]:
    st = _cron_auth_status()
    hint = (
        "Authorization: Bearer <CRON_SECRET> 헤더가 필요합니다. "
        "Vercel Production 환경변수 CRON_SECRET 과 동일한 값을 넣으세요. "
        "브라우저 주소창만으로는 호출할 수 없습니다."
    )
    if st.get("external_client") and st["cron_secret_configured"]:
        hint = (
            "CRON_SECRET 이 설정되어 있습니다. 예: "
            'curl -H "Authorization: Bearer YOUR_SECRET" '
            "https://check-company.vercel.app/api/cron/daily-risk-email"
        )
    elif st["cron_secret_configured"] and not st["authorization_header_present"]:
        hint = (
            "외부 호출에는 Authorization: Bearer CRON_SECRET 이 필요합니다. "
            "Vercel Cron(127.0.0.1 내부 호출)은 배포 후 internal_proxy 로 통과해야 합니다."
        )
    return (
        jsonify(
            {
                "ok": False,
                "error": "unauthorized",
                "hint": hint,
                "auth_check": st,
            }
        ),
        401,
    )


@app.route("/jobs/daily-risk-email", methods=["GET", "POST"])
def jobs_daily_risk_email():
    """
    Make·수동 호출용 — 화면 「메일발송」과 동일.
    GET ?run=1 + Authorization: Bearer CRON_SECRET
    JSON: &format=json
    """
    urls = _jobs_daily_email_urls()
    run_requested = (
        (request.args.get("run") or "").strip() in ("1", "true", "yes")
        or (request.values.get("run") or "").strip() in ("1", "true", "yes")
        or request.method == "POST"
    )
    wants_json = (request.args.get("format") or "").strip().lower() == "json"

    if not run_requested:
        return render_template(
            "jobs_daily_risk_email.html",
            page_mode="info",
            cron_secret_set=bool(_cron_secret_value()),
            **urls,
        )

    if not _cron_authorized():
        hint = (
            "Authorization: Bearer <CRON_SECRET> 헤더가 필요합니다. "
            "Vercel Production 의 CRON_SECRET 과 동일하게 설정하세요."
        )
        if wants_json:
            return jsonify(
                {
                    "ok": False,
                    "error": "unauthorized",
                    "hint": hint,
                    "auth_check": _cron_auth_status(),
                    **urls,
                }
            ), 401
        return (
            render_template(
                "jobs_daily_risk_email.html",
                page_mode="error",
                error_message=hint,
                cron_secret_set=bool(_cron_secret_value()),
                **urls,
            ),
            401,
        )

    kst = timezone(timedelta(hours=9))
    t0 = time.perf_counter()
    started = datetime.now(kst).strftime("%Y-%m-%d %H:%M:%S KST")
    collected = collect_risk_email_job_events()
    elapsed = round(time.perf_counter() - t0, 1)
    finished = datetime.now(kst).strftime("%Y-%m-%d %H:%M:%S KST")

    if wants_json:
        body: dict[str, Any] = {
            "ok": collected["ok"],
            "elapsed_sec": elapsed,
            "finished_at": finished,
            "events": collected.get("events") or [],
            **urls,
        }
        if collected.get("result"):
            body["result"] = collected["result"]
        if collected.get("error"):
            body["error"] = collected["error"]
        return jsonify(body), 200 if collected["ok"] else 500

    return render_template(
        "jobs_daily_risk_email.html",
        page_mode="result",
        ok=collected["ok"],
        events=collected.get("events") or [],
        result=collected.get("result"),
        error_message=collected.get("error"),
        started_at=started,
        finished_at=finished,
        elapsed_sec=elapsed,
        cron_secret_set=bool(_cron_secret_value()),
        **urls,
    )


@app.route("/api/cron/auth-check", methods=["GET", "HEAD"])
def cron_auth_check():
    """Cron 인증 디버그(시크릿 값은 노출하지 않음)."""
    return jsonify(_cron_auth_status())


def _cron_job_log_lines(events: list[dict[str, Any]], *, limit: int = 8) -> list[str]:
    lines: list[str] = []
    for ev in events:
        et = ev.get("type")
        if et == "progress" and ev.get("phase") == "analyzing":
            lines.append(
                f"분석 {ev.get('current')}/{ev.get('total')} "
                f"({ev.get('pct')}%)"
            )
        elif ev.get("message"):
            lines.append(f"[{et}] {ev['message']}")
        elif et in ("error", "done"):
            lines.append(str(ev))
    return lines[-limit:]


@app.route("/api/cron/daily-risk-email", methods=["GET", "POST", "HEAD"])
def cron_daily_risk_email():
    """
    매일 오전 10시(KST) Vercel Cron — 화면 메일발송과 동일(동기 JSON).
    01:00 UTC = 10:00~10:59 KST (Hobby는 해당 시각대 내 임의 시각).
    ?ping=1 — 인증·경로 확인만 (작업 미실행).
    """
    if request.method == "HEAD":
        return ("", 200) if _cron_authorized() else ("", 401)

    auth_st = _cron_auth_status()
    if not auth_st["authorized"]:
        return _cron_unauthorized_response()

    schedule_hdr = _header_get("x-vercel-cron-schedule")
    ping_only = (request.args.get("ping") or "").strip().lower() in (
        "1",
        "true",
        "yes",
    )
    if ping_only:
        return jsonify(
            {
                "ok": True,
                "ping": True,
                "path": "/api/cron/daily-risk-email",
                "schedule": schedule_hdr or None,
                "auth_check": auth_st,
                "note": "Vercel Cron 경로·인증 정상. ?ping 없이 호출 시 메일 작업 실행.",
            }
        )

    kst = timezone(timedelta(hours=9))
    started_at = datetime.now(kst).isoformat(timespec="seconds")
    print(
        f"[cron] daily-risk-email START schedule={schedule_hdr or 'manual'} "
        f"vercel_cron={auth_st.get('vercel_cron_detected')}",
        flush=True,
    )
    t0 = time.perf_counter()
    try:
        collected = collect_risk_email_job_events()
    except Exception as e:
        print(f"[cron] daily-risk-email FATAL {e}", flush=True)
        return (
            jsonify(
                {
                    "ok": False,
                    "error": str(e),
                    "schedule": schedule_hdr or None,
                    "started_at": started_at,
                    "elapsed_sec": round(time.perf_counter() - t0, 1),
                }
            ),
            500,
        )

    elapsed = round(time.perf_counter() - t0, 1)
    events = collected.get("events") or []
    for line in _cron_job_log_lines(events):
        print(f"[cron] {line}", flush=True)

    body: dict[str, Any] = {
        "ok": collected["ok"],
        "schedule": schedule_hdr or None,
        "started_at": started_at,
        "finished_at": datetime.now(kst).isoformat(timespec="seconds"),
        "elapsed_sec": elapsed,
        "auth_check": {
            "vercel_cron_detected": auth_st.get("vercel_cron_detected"),
            "bearer_matches": auth_st.get("bearer_matches"),
        },
        "log_tail": _cron_job_log_lines(events),
    }
    if collected.get("result"):
        body["result"] = collected["result"]
    if collected.get("error"):
        body["error"] = collected["error"]

    status = 200 if collected["ok"] else 500
    print(
        f"[cron] daily-risk-email END ok={collected['ok']} elapsed={elapsed}s",
        flush=True,
    )
    return jsonify(body), status


@app.route("/api/send-risk-email_stream", methods=["POST"])
def send_risk_email_stream():
    """NDJSON — 화면 「메일발송」: Cron 과 동일 분석·발송, 진행률 스트림."""

    def chunks() -> Iterator[bytes]:
        try:
            for ev in _iter_risk_email_job_events():
                yield _ndjson_line(ev)
                if ev.get("type") == "error":
                    return
        except Exception as e:
            yield _ndjson_line({"type": "error", "message": str(e)})

    return Response(
        stream_with_context(chunks()),
        mimetype="application/x-ndjson; charset=utf-8",
        headers=_ndjson_stream_headers(),
    )


@app.route("/api/cron/disclosure-refresh", methods=["GET", "POST"])
def cron_disclosure_refresh():
    """
    공시번호 없는 customers 를 DART로 보강 (수동·향후 Cron 재활성화용).
    Vercel Hobby 는 Cron 1개만 허용 → vercel.json 에서는 비활성화됨.
    CRON_SECRET 설정 시: Authorization: Bearer <CRON_SECRET>
    """
    if not _cron_authorized():
        return _cron_unauthorized_response()

    if supabase_customers is None or not supabase_customers.is_configured():
        return jsonify({"ok": False, "error": "supabase not configured"}), 503

    from services.customer_enrichment import run_disclosure_refresh_batch

    try:
        pending = supabase_customers.fetch_rows_missing_disclosure(force_refresh=True)
        stats = run_disclosure_refresh_batch(pending)
        still_missing = supabase_customers.count_missing_disclosure(force_refresh=True)
        return jsonify(
            {
                "ok": True,
                "schedule_note": "Vercel Cron 01:00 UTC = 10:00 KST",
                **stats,
                "still_missing_disclosure": still_missing,
            }
        )
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/health")
def api_health():
    """배포·환경 변수 확인(시크릿 값은 노출하지 않음)."""
    from services.dart_service import get_dart_api_key

    body: dict[str, Any] = {
        "dart_api_key_set": bool(get_dart_api_key()),
        "vercel": bool(os.environ.get("VERCEL")),
    }
    if supabase_customers is not None:
        body["supabase"] = supabase_customers.config_status()
        if body["supabase"].get("supabase_ready"):
            try:
                rows = supabase_customers.fetch_all_customer_rows(force_refresh=True)
                body["supabase"]["customer_row_count"] = len(rows)
                body["supabase"]["missing_disclosure_count"] = (
                    supabase_customers.count_missing_disclosure(force_refresh=True)
                )
            except Exception as e:
                body["supabase"]["fetch_error"] = str(e)[:300]
    else:
        body["supabase"] = {"supabase_ready": False}

    try:
        from services.resend_client import config_status

        body["resend"] = config_status()
    except Exception as e:
        body["resend"] = {"resend_ready": False, "error": str(e)[:200]}

    body["cron"] = {
        "daily_risk_email_path": "/api/cron/daily-risk-email",
        "daily_risk_email_ping": "/api/cron/daily-risk-email?ping=1",
        "make_daily_email_page": "/jobs/daily-risk-email",
        "make_trigger_url": "/jobs/daily-risk-email?run=1&format=json",
        "vercel_schedule_utc": "0 1 * * *",
        "kst_window": "10:00~10:59 (Hobby는 해당 시각대 내 임의 실행)",
        "cron_secret_set": bool((os.environ.get("CRON_SECRET") or "").strip()),
        "function_max_duration_sec": 300,
        "fluid_compute": True,
        "response_format": "application/json (동기, NDJSON 아님)",
        "note": "Cron 로그에서 ok:false·504 확인. ping으로 인증만 테스트.",
    }

    return jsonify(body)


def _pending_rows_for_enrich_page(*, force_refresh: bool = True) -> list[dict[str, Any]]:
    if not _supabase_ready():
        return []
    raw = supabase_customers.fetch_rows_biz_number_only(force_refresh=force_refresh)
    return [supabase_customers.pending_row_display(r) for r in raw]


@app.route("/customers/enrich", methods=["GET"])
def customers_enrich_page():
    """사업자번호만 등록된 customers — 행별 DART 갱신."""
    if not _supabase_ready():
        return (
            render_template(
                "customers_enrich.html",
                pending_rows=[],
                missing_disclosure_count=None,
                error="Supabase 가 설정되지 않았습니다. SUPABASE_URL · SUPABASE_KEY 를 확인하세요.",
            ),
            503,
        )
    try:
        missing = supabase_customers.count_missing_disclosure(force_refresh=True)
        pending_rows = _pending_rows_for_enrich_page(force_refresh=True)
    except Exception as e:
        return (
            render_template(
                "customers_enrich.html",
                pending_rows=[],
                missing_disclosure_count=None,
                error=str(e),
            ),
            500,
        )
    return render_template(
        "customers_enrich.html",
        pending_rows=pending_rows,
        missing_disclosure_count=missing,
        error=None,
    )


@app.route("/api/customers/add", methods=["POST"])
def api_customers_add():
    if not _supabase_ready():
        return jsonify({"ok": False, "error": "supabase_not_configured", "message": "Supabase 미설정"}), 503
    payload = request.get_json(force=True, silent=True) or {}
    raw_biz = str(payload.get("business_number") or payload.get("biz") or "").strip()
    try:
        row = supabase_customers.insert_business_number_only(raw_biz)
        display = supabase_customers.pending_row_display(row)
        return jsonify(
            {
                "ok": True,
                "message": f"사업자번호 {display['business_number']} 등록되었습니다.",
                "row": display,
            }
        )
    except supabase_customers.DuplicateBusinessNumberError as e:
        return jsonify(
            {"ok": False, "error": "duplicate_business_number", "message": str(e)}
        ), 409
    except ValueError as e:
        return jsonify({"ok": False, "error": "validation", "message": str(e)}), 400
    except Exception as e:
        return jsonify({"ok": False, "error": "server", "message": str(e)}), 500


@app.route("/api/customers/delete", methods=["POST"])
def api_customers_delete():
    if not _supabase_ready():
        return jsonify({"ok": False, "error": "supabase_not_configured", "message": "Supabase 미설정"}), 503
    payload = request.get_json(force=True, silent=True) or {}
    raw_biz = str(payload.get("business_number") or payload.get("biz") or "").strip()
    if not raw_biz:
        return jsonify(
            {"ok": False, "error": "empty", "message": "삭제할 사업자번호를 입력하세요."}
        ), 400
    try:
        deleted = supabase_customers.delete_customer_by_business_number(raw_biz)
        biz = deleted.get("business_number") or normalize_biz_number(raw_biz)
        return jsonify(
            {
                "ok": True,
                "message": f"사업자번호 {biz}를 DB에서 삭제했습니다.",
                "row": deleted,
            }
        )
    except supabase_customers.CustomerNotFoundError as e:
        return jsonify({"ok": False, "error": "not_found", "message": str(e)}), 404
    except ValueError as e:
        return jsonify({"ok": False, "error": "validation", "message": str(e)}), 400
    except Exception as e:
        return jsonify({"ok": False, "error": "server", "message": str(e)}), 500


@app.route("/api/customers/enrich-one", methods=["POST"])
def api_customers_enrich_one():
    if not _supabase_ready():
        return jsonify({"ok": False, "error": "supabase_not_configured", "message": "Supabase 미설정"}), 503
    payload = request.get_json(force=True, silent=True) or {}
    raw_biz = str(payload.get("business_number") or payload.get("biz") or "").strip()
    biz = normalize_biz_number(raw_biz)
    if len(biz) != 10:
        return jsonify({"ok": False, "message": "사업자번호는 10자리 숫자여야 합니다."}), 400

    from services.customer_enrichment import enrich_customer_from_dart

    name = ""
    try:
        existing = supabase_customers.fetch_customer_by_business_number(biz)
        if existing:
            name = (str(existing.get("company_name") or "")).strip()
    except Exception:
        pass

    outcome = enrich_customer_from_dart(biz, name)
    if outcome.error:
        status = 503 if outcome.fatal else 400
        return jsonify({"ok": False, "message": outcome.error, "fatal": outcome.fatal}), status
    if not outcome.row:
        return jsonify({"ok": False, "message": "DART 조회 결과가 없습니다."}), 400

    supabase_customers.upsert_customer_rows([outcome.row])
    display = supabase_customers.pending_row_display(outcome.row)
    corp = display.get("company_name") or biz
    return jsonify(
        {
            "ok": True,
            "message": f"{corp} — 업체명·공시번호·종목코드 갱신 완료",
            "row": display,
        }
    )


@app.route("/api/customers/enrich-pending-stream", methods=["POST"])
def api_customers_enrich_pending_stream():
    """NDJSON — 사업자번호만 등록된 customers 행을 DART로 일괄 보강."""

    def chunks() -> Iterator[bytes]:
        try:
            yield _ndjson_line(
                {
                    "type": "progress",
                    "phase": "connecting",
                    "current": 0,
                    "total": 0,
                    "fraction": 0,
                    "pct": 0,
                    "elapsed_sec": 0,
                    "eta_sec": None,
                    "message": "서버에 연결합니다…",
                }
            )

            if not _supabase_ready():
                yield _ndjson_line(
                    {"type": "error", "message": "Supabase 가 설정되지 않았습니다."}
                )
                return

            from services.customer_enrichment import iter_disclosure_refresh_events

            yield _ndjson_line(
                {"type": "info", "message": "갱신 대기 목록을 불러옵니다…"}
            )

            pending = supabase_customers.fetch_rows_biz_number_only(force_refresh=True)
            if not pending:
                yield _ndjson_line(
                    {
                        "type": "error",
                        "message": "갱신할 사업자번호가 없습니다.",
                    }
                )
                return

            for ev in iter_disclosure_refresh_events(pending, max_items=None):
                if ev.get("type") == "item" and ev.get("ok") and ev.get("row"):
                    ev = {
                        **ev,
                        "row": supabase_customers.pending_row_display(ev["row"]),
                    }
                elif ev.get("type") == "done":
                    supabase_customers.invalidate_customer_cache()
                yield _ndjson_line(ev)

        except Exception as e:
            yield _ndjson_line({"type": "error", "message": str(e)})

    return Response(
        stream_with_context(chunks()),
        mimetype="application/x-ndjson; charset=utf-8",
        headers=_ndjson_stream_headers(),
    )


@app.route("/", methods=["GET", "POST"])
def index():
    results = []
    err = None
    raw_csv = initial_csv_text_for_get() if request.method == "GET" else ""

    if request.method == "POST":
        raw_csv = request.form.get("csv_text") or ""

        if not err:
            try:
                customers, prep = prepare_customers_for_analysis(raw_csv)
                if not customers:
                    if prep.get("skipped_no_disclosure"):
                        err = (
                            f"공시번호(8자리)가 있는 고객이 없습니다. "
                            f"전체 {prep.get('total_raw', 0)}건 중 {prep['skipped_no_disclosure']}건 제외됨."
                        )
                    else:
                        err = "유효한 행이 없습니다. 사업자번호(10자리) 또는 업체명+사업자번호 컬럼을 확인하세요."
                if not err:
                    gap = float(os.environ.get("DART_ANALYZE_SLEEP_SEC") or "0")
                    load_biz_registry()
                    with analyze_runtime():
                        for i, c in enumerate(customers):
                            if i and gap > 0:
                                time.sleep(gap)
                            results.append(_analyze_customer_row(c))
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
        **customer_source_context(),
        formula_summary={
            "weights": composite_weights_formula(include_market=True),
            "weights_note": (
                "종목 코드가 없는 행은 M을 복합점수에 넣지 않고 "
                f"{composite_weights_formula(include_market=False)} 로 N·C 비중을 키웁니다."
            ),
            "churn_prob": "P = σ(-2 + 4·S),  σ(x)=1/(1+e^{-x})",
        },
    )


@app.route("/api/analyze", methods=["POST"])
def api_analyze():
    payload = request.get_json(force=True, silent=True) or {}
    if payload.get("source") == "dart_bizno_input_list" or payload.get("use_dart_bizno_list"):
        mapped = dart_bizno_input_customer_rows()
        if supabase_customers is not None:
            rows, _prep = supabase_customers.filter_customers_with_disclosure(mapped)
        else:
            rows, _prep = _filter_disclosure_fallback(mapped)
    elif _supabase_ready() and not payload.get("customers"):
        rows, _prep = prepare_customers_for_analysis("")
    else:
        raw_rows = payload.get("customers") or []
        mapped = [
            {
                "company_name": str(r.get("company_name", "")),
                "business_number": str(r.get("business_number", "")),
                "ticker_override": r.get("ticker_override") or r.get("ticker"),
                "dart_corp_code": normalize_disclosure_corp_code(
                    str(
                        r.get("dart_corp_code")
                        or r.get("공시번호")
                        or r.get("corp_code")
                        or ""
                    )
                )
                or None,
            }
            for r in raw_rows
        ]
        if supabase_customers is not None:
            rows, _prep = supabase_customers.filter_customers_with_disclosure(mapped)
        else:
            rows, _prep = _filter_disclosure_fallback(mapped)
    out = []
    gap = float(os.environ.get("DART_ANALYZE_SLEEP_SEC") or "0")
    load_biz_registry()
    with analyze_runtime():
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


@app.route("/api/enrich_stream", methods=["POST"])
def enrich_stream():
    """NDJSON — 공시번호 없는 customers 를 DART로 보강(10시 Cron 과 동일 로직)."""

    def chunks() -> Iterator[bytes]:
        try:
            yield _ndjson_line(
                {
                    "type": "progress",
                    "phase": "connecting",
                    "current": 0,
                    "total": 0,
                    "fraction": 0,
                    "pct": 0,
                    "elapsed_sec": 0,
                    "eta_sec": None,
                    "message": "시도 중… 서버에 연결합니다.",
                }
            )

            if not _supabase_ready():
                err = {"type": "error", "message": "Supabase 가 설정되지 않았습니다."}
                yield _ndjson_line(err)
                return

            from services.customer_enrichment import enrich_manual_max_items, iter_disclosure_refresh_events

            yield _ndjson_line(
                {"type": "info", "message": "시도 중… 공시번호 없는 고객 목록을 불러옵니다."}
            )

            pending = supabase_customers.fetch_rows_missing_disclosure(force_refresh=True)
            manual_cap = enrich_manual_max_items()
            max_items = manual_cap if manual_cap is not None else len(pending)

            for ev in iter_disclosure_refresh_events(pending, max_items=max_items):
                if ev.get("type") == "done":
                    supabase_customers.invalidate_customer_cache()
                    csv_text = supabase_customers.customers_to_csv_text()
                    ev = {
                        **ev,
                        "csv_text": csv_text,
                        "fragment": csv_preview_fragment(csv_text),
                    }
                yield _ndjson_line(ev)

        except Exception as e:
            err = {"type": "error", "message": str(e)}
            yield _ndjson_line(err)

    return Response(
        stream_with_context(chunks()),
        mimetype="application/x-ndjson; charset=utf-8",
        headers=_ndjson_stream_headers(),
    )


@app.route("/api/analyze_stream", methods=["POST"])
def analyze_stream():
    """NDJSON 줄 단위 진행(progress) 후 마지막에 렌더된 결과 HTML(fragment) 포함."""
    body = request.get_json(force=True, silent=True) or {}
    raw = body.get("csv_text")
    csv_text = raw if isinstance(raw, str) else ""

    def chunks() -> Iterator[bytes]:
        try:
            customers, prep = prepare_customers_for_analysis(csv_text)
            if prep.get("skipped_no_disclosure"):
                info = {
                    "type": "info",
                    "message": (
                        f"공시번호 없음 {prep['skipped_no_disclosure']}건 제외 · "
                        f"분석 {prep.get('count', 0)}건"
                        + (
                            f" (출처: Supabase)"
                            if prep.get("source") == "supabase"
                            else ""
                        )
                    ),
                }
                yield (json_std.dumps(info, ensure_ascii=False) + "\n").encode("utf-8")
            if not customers:
                msg = "공시번호(8자리)가 있는 고객이 없어 분석할 수 없습니다."
                if prep.get("total_raw"):
                    msg += f" (전체 {prep['total_raw']}건, 제외 {prep.get('skipped_no_disclosure', 0)}건)"
                payload = {"type": "error", "message": msg}
                yield (json_std.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")
                return

            gap = float(os.environ.get("DART_ANALYZE_SLEEP_SEC") or "0")
            n = len(customers)
            t0 = time.perf_counter()
            results_payload: list[dict[str, Any] | None] = [None] * n
            workers = _bulk_analyze_workers()
            load_biz_registry()

            def emit_progress(done: int) -> bytes:
                elapsed = time.perf_counter() - t0
                rate = elapsed / done if done else 0.0
                eta_sec = max(0.0, rate * (n - done))
                prog = {
                    "type": "progress",
                    "current": done,
                    "total": n,
                    "fraction": done / n if n else 1.0,
                    "pct": round(100.0 * done / n, 1) if n else 100.0,
                    "elapsed_sec": round(elapsed, 1),
                    "eta_sec": round(eta_sec, 1),
                }
                return (json_std.dumps(prog, ensure_ascii=False) + "\n").encode("utf-8")

            def run_sequential() -> Iterator[bytes]:
                with analyze_runtime():
                    for i, c in enumerate(customers):
                        if i and gap > 0:
                            time.sleep(gap)
                        try:
                            results_payload[i] = _analyze_customer_row(c)
                        except Exception as e:
                            err = {"type": "error", "message": f"{i + 1}번째 행 분석 오류: {e}"}
                            yield (json_std.dumps(err, ensure_ascii=False) + "\n").encode("utf-8")
                            return
                        yield emit_progress(i + 1)

            def run_parallel() -> Iterator[bytes]:
                with analyze_runtime():
                    with ThreadPoolExecutor(max_workers=workers) as pool:
                        futures = {
                            pool.submit(_analyze_customer_row, c): i
                            for i, c in enumerate(customers)
                        }
                        done = 0
                        for fut in as_completed(futures):
                            i = futures[fut]
                            try:
                                results_payload[i] = fut.result()
                            except Exception as e:
                                err = {
                                    "type": "error",
                                    "message": f"{i + 1}번째 행 분석 오류: {e}",
                                }
                                yield (json_std.dumps(err, ensure_ascii=False) + "\n").encode(
                                    "utf-8"
                                )
                                return
                            done += 1
                            yield emit_progress(done)

            if n <= 2 or workers <= 1:
                yield from run_sequential()
            else:
                yield from run_parallel()

            results_payload = [r for r in results_payload if r is not None]

            frag_ctx = analysis_results_fragment_kwargs(results_payload, prep_meta=prep)
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
