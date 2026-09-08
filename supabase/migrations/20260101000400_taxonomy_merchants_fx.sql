-- ─────────────────────────────────────────────────────────────────────────────
-- Taxonomia: categorias, comercios normalizados y tipos de cambio.
-- Va antes de transactions porque es a donde apuntan sus claves foraneas.
-- ─────────────────────────────────────────────────────────────────────────────

-- `user_id NULL` = categoria del sistema, visible para todos, editable por nadie.
-- Cada usuario puede crear las suyas encima.
create table app.categories (
  id        uuid primary key default gen_random_uuid(),
  user_id   uuid references app.profiles(id) on delete cascade,
  parent_id uuid references app.categories(id) on delete set null,
  slug      text not null,
  name      text not null,
  kind      text not null default 'expense' check (
              kind in ('expense', 'income', 'transfer', 'tax', 'fee')),
  color     text,
  icon      text,
  is_active boolean not null default true,
  sort_order smallint not null default 100,
  created_at timestamptz not null default now()
);

-- El COALESCE con el UUID nulo agrupa todas las filas del sistema bajo una misma
-- "clave de dueño", de modo que un usuario pueda tener un slug que coincide con
-- uno del sistema sin chocar.
create unique index categories_slug_uq on app.categories (
  coalesce(user_id, '00000000-0000-0000-0000-000000000000'::uuid), slug
);
create index categories_user_idx on app.categories (user_id) where is_active;

comment on column app.categories.kind is
  'Separa consumo de impuestos, intereses y comisiones. En un resumen argentino '
  '(IVA 21%, percepcion RG 4815, IIBB, sellos) eso es ~30% de las lineas y no es gasto.';

-- ─────────────────────────────────────────────────────────────────────────────
-- merchants: el nombre limpio detras de "MERPAGO*SPOTIFY 1234 BUENOS AIRES AR".
-- ─────────────────────────────────────────────────────────────────────────────
create table app.merchants (
  id                  uuid primary key default gen_random_uuid(),
  user_id             uuid not null references app.profiles(id) on delete cascade,
  slug                text not null,
  canonical_name      text not null,
  default_category_id uuid references app.categories(id) on delete set null,
  website             text,
  created_at          timestamptz not null default now(),
  updated_at          timestamptz not null default now(),
  unique (user_id, id),
  unique (user_id, slug)
);

create trigger merchants_touch before update on app.merchants
  for each row execute function app.touch_updated_at();

-- Memoria de normalizacion. Es lo que evita pagarle al LLM cada mes por resolver
-- que "MERPAGO*SPOTIFY 1234" es Spotify: la primera vez lo decide el modelo, de
-- ahi en adelante sale de esta tabla. Tambien registra las correcciones a mano,
-- que valen mas que la respuesta del modelo (source = 'user').
create table app.merchant_aliases (
  id          uuid primary key default gen_random_uuid(),
  user_id     uuid not null,
  merchant_id uuid not null,
  -- Descripcion normalizada: mayusculas, sin digitos ni sufijos de sucursal.
  raw_key     text not null,
  source      text not null check (source in ('llm', 'user', 'seed')),
  confidence  numeric(3, 2) check (confidence between 0 and 1),
  hits        integer not null default 0,
  created_at  timestamptz not null default now(),
  unique (user_id, raw_key),
  foreign key (user_id, merchant_id) references app.merchants (user_id, id) on delete cascade
);

-- ─────────────────────────────────────────────────────────────────────────────
-- subscriptions: gastos recurrentes detectados.
-- ─────────────────────────────────────────────────────────────────────────────
create table app.subscriptions (
  id             uuid primary key default gen_random_uuid(),
  user_id        uuid not null,
  merchant_id    uuid not null,
  label          text not null,
  nominal_amount numeric(14, 2),
  currency       char(3),
  cadence        text not null default 'monthly' check (
                   cadence in ('monthly', 'yearly', 'other')),
  first_seen     date,
  last_seen      date,
  -- 'suspected' lo pone la deteccion automatica; pasa a 'active' cuando el
  -- usuario confirma. Nada se afirma sin que una persona lo valide.
  status         text not null default 'suspected' check (
                   status in ('active', 'paused', 'canceled', 'suspected')),
  created_at     timestamptz not null default now(),
  updated_at     timestamptz not null default now(),
  unique (user_id, id),
  foreign key (user_id, merchant_id) references app.merchants (user_id, id) on delete cascade
);

create trigger subscriptions_touch before update on app.subscriptions
  for each row execute function app.touch_updated_at();

-- ─────────────────────────────────────────────────────────────────────────────
-- fx_rates: tipo de cambio por mes.
--
-- Un resumen argentino trae consumos en pesos y en dolares. Para ver un total
-- consolidado hace falta una cotizacion, y cual usar es una decision del usuario
-- (oficial, MEP, la que el banco aplico). Por eso: carga manual por mes, con
-- `source` para saber de donde salio.
-- ─────────────────────────────────────────────────────────────────────────────
create table app.fx_rates (
  id             uuid primary key default gen_random_uuid(),
  user_id        uuid references app.profiles(id) on delete cascade,  -- NULL = global
  period_year    smallint not null check (period_year between 2000 and 2100),
  period_month   smallint not null check (period_month between 1 and 12),
  base_currency  char(3) not null,
  quote_currency char(3) not null,
  -- Cuantas unidades de quote_currency vale una de base_currency.
  rate           numeric(18, 6) not null check (rate > 0),
  source         text not null default 'manual' check (
                   source in ('manual', 'bcra', 'statement')),
  as_of          date,
  created_at     timestamptz not null default now(),
  check (base_currency <> quote_currency)
);

create unique index fx_rates_uq on app.fx_rates (
  coalesce(user_id, '00000000-0000-0000-0000-000000000000'::uuid),
  period_year, period_month, base_currency, quote_currency, source
);
create index fx_rates_lookup_idx on app.fx_rates (period_year, period_month);
