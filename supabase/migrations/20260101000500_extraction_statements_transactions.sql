-- ─────────────────────────────────────────────────────────────────────────────
-- ETAPA 2: texto crudo -> resumenes y transacciones.
-- ─────────────────────────────────────────────────────────────────────────────

-- Una fila por cada vez que se le pidio algo al LLM sobre un documento.
--
-- `raw_response` se guarda entero a proposito: permite re-derivar transacciones
-- con logica de parseo nueva SIN volver a llamar (ni pagar) al modelo, y deja
-- auditable que version de prompt produjo cada dato cuando algo sale raro tres
-- meses despues.
create table app.extraction_runs (
  id               uuid primary key default gen_random_uuid(),
  user_id          uuid not null,
  document_id      uuid not null,
  document_text_id uuid,
  stage            text not null check (stage in ('parse', 'enrich', 'repair')),
  provider         text not null,          -- 'gemini'
  model            text not null,          -- 'gemini-2.5-flash'
  prompt_version   text not null,          -- 'parse_transactions_v1'
  status           text not null default 'running' check (
                     status in ('running', 'succeeded', 'failed', 'superseded')),
  input_tokens     integer,
  output_tokens    integer,
  cost_usd         numeric(10, 6),
  latency_ms       integer,
  raw_response     jsonb,
  -- ValidationReport: cuadre, alucinaciones sospechadas, conflictos, huerfanos.
  validation       jsonb,
  error            text,
  started_at       timestamptz not null default now(),
  finished_at      timestamptz,

  unique (user_id, id),
  foreign key (user_id, document_id) references app.documents (user_id, id) on delete cascade
);

create index extraction_runs_doc_idx on app.extraction_runs (user_id, document_id, started_at desc);

-- ─────────────────────────────────────────────────────────────────────────────
-- statements: UNO POR (documento, moneda).
--
-- Un resumen argentino trae una seccion en pesos y otra en dolares, con totales
-- propios cada una. Modelarlas como un solo statement obligaria a mezclar montos
-- de monedas distintas en los mismos campos de total, y el cuadre de saldo — que
-- es la validacion principal del proyecto — dejaria de ser calculable.
-- ─────────────────────────────────────────────────────────────────────────────
create table app.statements (
  id               uuid primary key default gen_random_uuid(),
  user_id          uuid not null,
  document_id      uuid not null,
  card_id          uuid,
  currency         char(3) not null,
  period_year      smallint not null check (period_year between 2000 and 2100),
  period_month     smallint not null check (period_month between 1 and 12),
  closing_date     date,
  due_date         date,

  previous_balance numeric(14, 2),
  payments_credits numeric(14, 2),
  -- El cuadre se valida contra ESTE campo, no contra total_due.
  -- total_due = previous_balance - payments_credits + new_charges incluye el saldo
  -- anterior; validar la suma de transacciones contra el es el error clasico.
  new_charges      numeric(14, 2),
  total_due        numeric(14, 2),
  minimum_payment  numeric(14, 2),

  -- 'confirmed' solo por accion explicita del usuario. El dashboard, por default,
  -- agrega unicamente statements confirmados: los numeros que se muestran nunca
  -- son silenciosamente incorrectos.
  status           text not null default 'draft' check (
                     status in ('draft', 'needs_review', 'confirmed')),
  -- Diferencia de cuadre que el usuario decidio aceptar, si la hubo.
  accepted_delta   numeric(14, 2),
  notes            text,
  created_at       timestamptz not null default now(),
  updated_at       timestamptz not null default now(),

  unique (user_id, id),
  unique (user_id, document_id, currency),
  foreign key (user_id, document_id) references app.documents (user_id, id) on delete cascade,
  foreign key (user_id, card_id)     references app.cards (user_id, id)
    on delete set null (card_id)
);

-- Dos resumenes confirmados del mismo mes y tarjeta serian doble conteo en el
-- dashboard. Parcial sobre 'confirmed' para no bloquear reprocesar un borrador.
create unique index statements_period_uq
  on app.statements (user_id, card_id, currency, period_year, period_month)
  where status = 'confirmed';

create index statements_period_idx on app.statements (user_id, period_year, period_month);
create index statements_status_idx on app.statements (user_id, status);

create trigger statements_touch before update on app.statements
  for each row execute function app.touch_updated_at();

-- ─────────────────────────────────────────────────────────────────────────────
-- transactions
-- ─────────────────────────────────────────────────────────────────────────────
create table app.transactions (
  id                uuid primary key default gen_random_uuid(),
  user_id           uuid not null,
  statement_id      uuid not null,
  card_id           uuid,
  extraction_run_id uuid,

  posted_date       date not null,      -- fecha con la que figura en el resumen
  transaction_date  date,               -- fecha real de la compra, si aparece
  description_raw   text not null,
  -- Linea literal del PDF de la que salio esta transaccion. Es el ancla
  -- anti-alucinacion: se verifica por similitud contra el texto de entrada, y la
  -- pantalla de revision la muestra al lado del dato extraido.
  source_line       text,
  source_page       smallint,

  -- Monto SIEMPRE positivo; el sentido va aparte en `direction`. Los resumenes
  -- argentinos marcan credito con sufijos inconsistentes ('CR', '-', 'H'), asi
  -- que interpretar el signo es fragil: se le pide al modelo un entero explicito.
  amount            numeric(14, 2) not null check (amount >= 0),
  currency          char(3) not null,
  direction         smallint not null check (direction in (-1, 1)),

  kind              text not null default 'charge' check (
                      kind in ('charge', 'installment', 'payment', 'refund', 'interest',
                               'fee', 'tax', 'fx_charge', 'adjustment', 'unknown')),

  -- ─── Cuotas ───────────────────────────────────────────────────────────────
  -- El dashboard usa flujo de caja: `amount` es la cuota que se paga ESTE mes.
  -- Se persiste el total de la compra y el N/M para poder reconstruir la vista
  -- devengada y proyectar las cuotas futuras comprometidas.
  installment_number    smallint,
  installment_total     smallint,
  purchase_total_amount numeric(14, 2) check (purchase_total_amount >= 0),
  purchase_date         date,
  -- Agrupa las cuotas de una misma compra a lo largo de los meses.
  installment_group_key text,

  merchant_id     uuid,
  category_id     uuid references app.categories(id) on delete set null,
  is_recurring    boolean not null default false,
  subscription_id uuid,

  dedupe_key       char(64) not null check (dedupe_key ~ '^[0-9a-f]{64}$'),
  -- Dos cafes iguales el mismo dia en el mismo comercio son legitimos y no deben
  -- colapsarse. Al insertar un lote se agrupa por dedupe_key y se enumera 0..n-1:
  -- reimportar el mismo resumen choca con el unique, pero los duplicados reales
  -- del resumen sobreviven.
  occurrence_index smallint not null default 0 check (occurrence_index >= 0),

  confidence    numeric(3, 2) check (confidence between 0 and 1),
  review_status text not null default 'pending' check (
                  review_status in ('pending', 'confirmed', 'edited', 'rejected')),
  notes         text,
  created_at    timestamptz not null default now(),
  updated_at    timestamptz not null default now(),

  -- Una cuota tiene numero y total, o ninguno de los dos.
  constraint installments_coherent check (
    (installment_number is null and installment_total is null)
    or (installment_number between 1 and installment_total
        and installment_total between 1 and 120)
  ),

  unique (user_id, id),
  -- DEDUPE / IDEMPOTENCIA de transaccion.
  unique (user_id, statement_id, dedupe_key, occurrence_index),

  foreign key (user_id, statement_id)      references app.statements (user_id, id) on delete cascade,
  foreign key (user_id, card_id)           references app.cards (user_id, id)
    on delete set null (card_id),
  foreign key (user_id, merchant_id)       references app.merchants (user_id, id)
    on delete set null (merchant_id),
  foreign key (user_id, subscription_id)   references app.subscriptions (user_id, id)
    on delete set null (subscription_id),
  foreign key (user_id, extraction_run_id) references app.extraction_runs (user_id, id)
    on delete set null (extraction_run_id)
);

create index tx_user_date_idx  on app.transactions (user_id, posted_date desc);
create index tx_statement_idx  on app.transactions (user_id, statement_id);
create index tx_cat_date_idx   on app.transactions (user_id, category_id, posted_date);
create index tx_merchant_idx   on app.transactions (user_id, merchant_id);
create index tx_review_idx     on app.transactions (user_id, review_status)
  where review_status = 'pending';
create index tx_instgroup_idx  on app.transactions (user_id, installment_group_key)
  where installment_group_key is not null;

create trigger transactions_touch before update on app.transactions
  for each row execute function app.touch_updated_at();

-- ─────────────────────────────────────────────────────────────────────────────
-- transaction_revisions: que corrigio el usuario a mano.
--
-- Sirve para tres cosas: medir la accuracy real del modelo por emisor, alimentar
-- ejemplos few-shot con las correcciones, y tener bitacora de por que un dato no
-- coincide con el PDF.
-- ─────────────────────────────────────────────────────────────────────────────
create table app.transaction_revisions (
  id             uuid primary key default gen_random_uuid(),
  user_id        uuid not null,
  transaction_id uuid not null,
  field          text not null,
  old_value      jsonb,
  new_value      jsonb,
  changed_at     timestamptz not null default now(),
  foreign key (user_id, transaction_id) references app.transactions (user_id, id) on delete cascade
);

create index tx_revisions_idx on app.transaction_revisions (user_id, transaction_id, changed_at desc);
