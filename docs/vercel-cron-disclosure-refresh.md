# DART 공시 보강 Cron (비활성화)

Vercel Hobby 플랜은 **Cron 작업 1개**만 허용합니다.  
현재 `vercel.json` 은 **일일 위험 리포트 이메일** (`/api/cron/daily-risk-email`) 만 등록합니다.

## DART 보강을 다시 Cron 으로 돌리려면

`vercel.json` 의 `crons` 를 아래처럼 바꾸거나, Pro 플랜에서 Cron 을 추가하세요.

```json
"crons": [
  {
    "path": "/api/cron/disclosure-refresh",
    "schedule": "0 1 * * *"
  }
]
```

수동 호출은 계속 가능합니다.

```http
GET /api/cron/disclosure-refresh
Authorization: Bearer <CRON_SECRET>
```
