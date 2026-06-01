-- 선택: 최신 등록순 정렬용 (없으면 사업자번호 내림차순으로 대체)
alter table public.customers
  add column if not exists created_at timestamptz not null default now();

create index if not exists customers_created_at_desc_idx
  on public.customers (created_at desc);
