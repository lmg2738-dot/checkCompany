# 10시 일일 메일 Cron — 점검 가이드

## 동작 방식

| 경로 | 용도 |
|------|------|
| `GET /api/cron/daily-risk-email` | Vercel Cron (`vercel.json`) |
| `POST /api/send-risk-email_stream` | 화면 「메일발송」·GitHub Actions 백업 |

둘 다 **동일 분석·Resend 발송** 로직을 사용합니다.

## 메일이 안 왔을 때

### 1. Vercel Cron 로그

1. [Vercel 프로젝트](https://vercel.com) → **Settings** → **Cron Jobs**
2. `daily-risk-email` → **View Logs**
3. 확인:
   - **401** → `CRON_SECRET` 값이 Production 환경과 일치하는지
   - **504 / Task timed out** → 분석 시간 초과 (Pro + `maxDuration: 300` 권장)
   - **200** 이지만 메일 없음 → Resend 오류 로그 본문 확인

### 2. Hobby 플랜

- Cron은 **하루 1회**
- `0 1 * * *` (UTC) → **10:00~10:59 KST** 사이 임의 시각에 실행될 수 있음
- 함수 실행 시간 **최대 60초** 제한일 수 있음 → 긴 분석은 타임아웃

### 3. 수동 테스트 (Cron과 동일)

```bash
curl -sS "https://check-company.vercel.app/api/cron/daily-risk-email" \
  -H "Authorization: Bearer YOUR_CRON_SECRET"
```

또는 화면 **메일발송** 버튼.

### 4. GitHub Actions 백업 (선택)

저장소 **Settings → Secrets → Actions** 에 `CRON_SECRET` 추가 후  
`.github/workflows/daily-risk-email.yml` 이 매일 `send-risk-email_stream` 을 호출합니다.

## 환경 변수 체크리스트

- `RESEND_API_KEY`, `RESEND_FROM`, `RESEND_TO`
- `DART_API_KEY`, `SUPABASE_URL`, `SUPABASE_KEY`
- `CRON_SECRET` (Vercel Production)
- `DAILY_EMAIL_ENABLED` ≠ `0`

`/api/health` → `resend.resend_ready`, `cron` 섹션 확인
