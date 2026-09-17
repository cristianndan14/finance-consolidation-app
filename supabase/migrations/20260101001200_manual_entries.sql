-- ─────────────────────────────────────────────────────────────────────────────
-- Movimientos manuales: ingresos y gastos que no vienen de un resumen de
-- tarjeta (efectivo, transferencias, sueldo, etc.).
--
-- Va en una tabla propia y no en `app.transactions` a proposito: esa tabla esta
-- modelada enteramente alrededor del pipeline de tarjetas (`statement_id`
-- obligatorio, `dedupe_key` para idempotencia de reparse, cuotas,
-- `extraction_run_id`). Ninguna de esas columnas tiene sentido para una carga
-- manual, y forzarla ahi complicaria el cuadre financiero (la validacion
-- principal del proyecto) sin necesidad.
--
-- Un movimiento manual no pasa por revision: se confirma solo, no tiene
-- `review_status`. Es carga directa del usuario, no una extraccion de un
-- modelo que pueda alucinar.
-- ─────────────────────────────────────────────────────────────────────────────

create table app.manual_entries (
  id           uuid        primary key default gen_random_uuid(),
  user_id      uuid        not null references app.profiles(id) on delete cascade,
  entry_date   date        not null,
  -- 'income' suma como ingreso; 'expense' resta como consumo. Es la unica
  -- entrada de signo: category_id es libre y no se usa para inferir el sentido
  -- del movimiento (una categoria de sistema como 'otros' sirve para las dos).
  kind         text        not null check (kind in ('income', 'expense')),
  description  text        not null,
  amount       numeric(14, 2) not null check (amount > 0),
  currency     char(3)     not null,
  category_id  uuid        references app.categories(id) on delete set null,
  notes        text,
  created_at   timestamptz not null default now(),
  updated_at   timestamptz not null default now(),

  unique (user_id, id)
);

create index manual_entries_user_date_idx on app.manual_entries (user_id, entry_date desc);

create trigger manual_entries_touch before update on app.manual_entries
  for each row execute function app.touch_updated_at();

-- ─── RLS: mismo patron que el resto de las tablas con dueño simple ───────────
alter table app.manual_entries enable row level security;
alter table app.manual_entries force row level security;

create policy manual_entries_owner on app.manual_entries for all to authenticated
  using (user_id = auth.uid()) with check (user_id = auth.uid());

grant select, insert, update, delete on app.manual_entries to authenticated;

create trigger manual_entries_force_owner before insert or update on app.manual_entries
  for each row execute function app.force_owner();
