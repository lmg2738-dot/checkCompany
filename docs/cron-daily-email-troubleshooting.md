# 10시 일일 메일 Cron — 점검 가이드

## 동작 방식

| 경로 | 용도 |
|------|------|
| `GET /api/cron/daily-risk-email` | **Vercel Cron** (`vercel.json` · 동기 JSON) |
| `GET /api/cron/daily-risk-email?ping=1` | 인증·경로만 확인 (메일 미발송) |
| `POST /api/send-risk-email_stream` | 화면 「메일발송」(NDJSON 진행률) |
| `GET /jobs/daily-risk-email?run=1&format=json` | Make·수동 백업 |

## Vercel Cron 인증

Production에 `CRON_SECRET` 이 있으면 Vercel이 Cron 호출 시 자동으로 보냅니다.

- `Authorization: Bearer <CRON_SECRET>`
- `x-vercel-cron: 1` 또는 `User-Agent: vercel-cron/1.0`

둘 중 하나만 맞아도 통과합니다 (`/api/cron/auth-check` 참고).

### ping 테스트

```bash
curl -sS "https://check-company.vercel.app/api/cron/daily-risk-email?ping=1" \
  -H "Authorization: Bearer YOUR_CRON_SECRET"
```

`"ok": true, "ping": true` 이면 Cron 경로·인증 정상.

### 전체 작업 테스트 (2~5분)

```bash
curl -sS "https://check-company.vercel.app/api/cron/daily-risk-email" \
  -H "Authorization: Bearer YOUR_CRON_SECRET"
```

응답 JSON의 `"ok": true` 와 `result` 확인.

## 메일이 안 왔을 때

### 1. Vercel Cron 로그

1. [Vercel 프로젝트](https://vercel.com) → **Settings** → **Cron Jobs**
2. `daily-risk-email` → **View Logs**
3. 확인:
   - **401** → `CRON_SECRET` 불일치 (ping으로 Bearer 테스트)
   - **504 / FUNCTION_INVOCATION_TIMEOUT** → 분석 시간 초과 (`vercel.json` `fluid: true`, `maxDuration: 300`)
   - **500** + `"ok": false` → 본문 `error`·`log_tail` (Resend·Supabase·분석 오류)
   - **200** + `"ok": true` → Resend 수신함·스팸함 확인

### 2. Hobby 플랜

- Cron **하루 1회**, `0 1 * * *` (UTC) → **10:00~10:59 KST** 사이 임의 실행
- 함수 최대 **300초** (Fluid compute, `vercel.json` 설정)

### 3. 환경 변수

- `RESEND_API_KEY`, `RESEND_FROM`, `RESEND_TO`
- `DART_API_KEY`, `SUPABASE_URL`, `SUPABASE_KEY`
- `CRON_SECRET` (Production)
- `DAILY_EMAIL_ENABLED` ≠ `0`
- `ANALYZE_BULK_WORKERS=6` (선택, 분석 속도)

`/api/health` → `resend`, `cron`, `supabase` 확인

### 4. GitHub Actions 백업

저장소 **Settings → Secrets → Actions** 에 `CRON_SECRET` 추가 시  
`.github/workflows/daily-risk-email.yml` 이 동일 Cron API를 호출합니다.
