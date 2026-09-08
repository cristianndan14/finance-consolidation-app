-- ─────────────────────────────────────────────────────────────────────────────
-- Cola de trabajos y control de gasto del LLM.
-- ─────────────────────────────────────────────────────────────────────────────

create table app.processing_jobs (
  id           uuid primary key default gen_random_uuid(),
  user_id      uuid not null references app.profiles(id) on delete cascade,
  document_id  uuid,
  job_type     text not null check (
                 job_type in ('extract_text', 'parse_statement', 'enrich', 'revalidate')),
  status       text not null default 'queued' check (
                 status in ('queued', 'running', 'succeeded', 'failed', 'canceled')),
  payload      jsonb not null default '{}',
  attempts     smallint not null default 0,
  max_attempts smallint not null default 3,
  scheduled_at timestamptz not null default now(),
  -- El reaper devuelve a 'queued' los jobs cuyo locked_at quedo viejo. Hace falta
  -- porque un redeploy de Fly mata los BackgroundTasks a mitad de camino y si no
  -- el documento se queda en 'running' para siempre.
  locked_at    timestamptz,
  locked_by    text,
  started_at   timestamptz,
  finished_at  timestamptz,
  error        text,
  result       jsonb,
  created_at   timestamptz not null default now(),

  foreign key (user_id, document_id) references app.documents (user_id, id) on delete cascade
);

-- Encolar dos veces el mismo trabajo es un no-op en lugar de una segunda corrida
-- de LLM pagada. Parcial sobre los estados activos: una vez terminado, se puede
-- volver a encolar (reparse).
create unique index jobs_active_uq on app.processing_jobs (user_id, document_id, job_type)
  where status in ('queued', 'running');

-- El indice que usa el `for update skip locked` del runner.
create index jobs_pickup_idx on app.processing_jobs (scheduled_at)
  where status = 'queued';

create index jobs_stale_idx on app.processing_jobs (locked_at)
  where status = 'running';

create index jobs_user_idx on app.processing_jobs (user_id, created_at desc);

-- ─────────────────────────────────────────────────────────────────────────────
-- llm_usage: gasto acumulado por usuario y mes.
--
-- Existe para que un bucle de reprocesamiento no pueda vaciar la cuenta. El
-- servicio consulta esto ANTES de encolar y corta si supera el tope configurado.
-- ─────────────────────────────────────────────────────────────────────────────
create table app.llm_usage (
  id            uuid primary key default gen_random_uuid(),
  user_id       uuid not null references app.profiles(id) on delete cascade,
  period_year   smallint not null check (period_year between 2000 and 2100),
  period_month  smallint not null check (period_month between 1 and 12),
  provider      text not null,
  model         text not null,
  calls         integer not null default 0 check (calls >= 0),
  input_tokens  bigint  not null default 0 check (input_tokens >= 0),
  output_tokens bigint  not null default 0 check (output_tokens >= 0),
  cost_usd      numeric(10, 6) not null default 0 check (cost_usd >= 0),
  updated_at    timestamptz not null default now(),
  unique (user_id, period_year, period_month, provider, model)
);

create trigger llm_usage_touch before update on app.llm_usage
  for each row execute function app.touch_updated_at();
