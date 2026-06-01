# Make.com — 매일 10시 일일 리포트 메일

Vercel Cron이 불안정할 때 **화면 「메일발송」과 동일한 작업**을 Make로 호출합니다.

## URL

| 용도 | URL |
|------|-----|
| 설정 안내 (브라우저) | `https://check-company.vercel.app/jobs/daily-risk-email` |
| **Make 호출 (JSON)** | `https://check-company.vercel.app/jobs/daily-risk-email?run=1&format=json` |
| 결과 확인 (HTML) | `https://check-company.vercel.app/jobs/daily-risk-email?run=1` |

## Make 시나리오 예시

1. **Schedule** — Every day, **10:00**, Time zone **Asia/Seoul**
2. **HTTP** — Make a request  
   - URL: `https://check-company.vercel.app/jobs/daily-risk-email?run=1&format=json`  
   - Method: `GET`  
   - Headers: `Authorization` = `Bearer <Vercel의 CRON_SECRET>`  
   - Timeout: **600** 초 이상 권장 (분석 2~5분)
3. 응답 JSON의 `ok` 가 `true` 이면 성공

## 환경 변수 (Vercel Production)

- `CRON_SECRET` — Make 헤더와 동일 값
- `RESEND_API_KEY`, `RESEND_FROM`, `RESEND_TO`
- `DAILY_EMAIL_ENABLED=1`
- `SUPABASE_URL`, `SUPABASE_KEY` (고객 목록)

## curl 테스트

```bash
curl -fsS -H "Authorization: Bearer YOUR_CRON_SECRET" \
  "https://check-company.vercel.app/jobs/daily-risk-email?run=1&format=json"
```
