-- ─────────────────────────────────────────────────────────────────────────────
-- Tarjetas y ETAPA 1 del pipeline: documento -> texto crudo.
--
-- Sobre `unique (user_id, id)` en tablas que ya tienen `id` como primary key:
-- parece redundante y no lo es. Habilita CLAVES FORANEAS COMPUESTAS
-- `(user_id, card_id) references app.cards(user_id, id)`, que hacen
-- ESTRUCTURALMENTE IMPOSIBLE que una fila de un usuario apunte a una fila de
-- otro. Sin eso, un bug podria colgar un documento mio de una tarjeta ajena y
-- ninguna politica RLS lo notaria: cada fila, por separado, tiene el user_id
-- correcto.
-- ─────────────────────────────────────────────────────────────────────────────

create table app.cards (
  id           uuid    primary key default gen_random_uuid(),
  user_id      uuid    not null references app.profiles(id) on delete cascade,
  issuer_slug  text    not null,        -- 'galicia', 'santander', 'mercadopago', 'uala'
  issuer_name  text    not null,
  product_name text,                    -- 'Visa Signature'
  brand        text    check (brand in ('visa', 'mastercard', 'amex', 'cabal', 'other')),
  last4        char(4) check (last4 ~ '^[0-9]{4}$'),
  closing_day  smallint check (closing_day between 1 and 31),
  due_day      smallint check (due_day between 1 and 31),
  is_active    boolean not null default true,
  created_at   timestamptz not null default now(),
  updated_at   timestamptz not null default now(),
  unique (user_id, id)
);

-- Constraint de unicidad con COALESCE: tiene que ser un indice de expresion,
-- no un `unique (...)` de tabla, porque last4 y product_name son nullables y en
-- SQL dos NULL no son iguales entre si (dos tarjetas sin last4 pasarian).
create unique index cards_identity_uq on app.cards (
  user_id, issuer_slug, coalesce(last4, ''), coalesce(product_name, '')
);

create index cards_active_idx on app.cards (user_id) where is_active;

create trigger cards_touch before update on app.cards
  for each row execute function app.touch_updated_at();

-- ─────────────────────────────────────────────────────────────────────────────
-- documents: el PDF subido.
-- ─────────────────────────────────────────────────────────────────────────────
create table app.documents (
  id                uuid    primary key default gen_random_uuid(),
  user_id           uuid    not null references app.profiles(id) on delete cascade,
  card_id           uuid,
  storage_bucket    text    not null default 'statements',
  storage_path      text    not null,
  original_filename text    not null,
  mime_type         text    not null default 'application/pdf',
  byte_size         bigint  not null check (byte_size > 0),
  sha256            char(64) not null check (sha256 ~ '^[0-9a-f]{64}$'),
  page_count        smallint check (page_count > 0),
  -- Galicia, Santander y BBVA mandan el resumen cifrado con el DNI del titular.
  -- Se guarda descifrado; la contraseña no se persiste nunca.
  was_encrypted     boolean not null default false,
  status            text    not null default 'uploaded' check (
                      status in ('uploaded', 'text_extracted', 'parsed',
                                 'needs_review', 'confirmed', 'failed')),
  failure_reason    text,
  uploaded_at       timestamptz not null default now(),
  updated_at        timestamptz not null default now(),

  unique (user_id, id),
  -- IDEMPOTENCIA DE ARCHIVO: subir el mismo resumen dos veces (aun renombrado)
  -- no crea un segundo documento ni gasta una segunda corrida de LLM.
  unique (user_id, sha256),
  foreign key (user_id, card_id) references app.cards (user_id, id)
    -- La lista de columnas es obligatoria en una FK compuesta: sin ella,
    -- `set null` anularia TAMBIEN user_id y la fila quedaria sin dueño.
    on delete set null (card_id)
);

create index documents_user_status_idx on app.documents (user_id, status);
create index documents_recent_idx      on app.documents (user_id, uploaded_at desc);

create trigger documents_touch before update on app.documents
  for each row execute function app.touch_updated_at();

-- ─────────────────────────────────────────────────────────────────────────────
-- document_texts: la salida de pdfplumber.
--
-- Versionado por (extractor, extractor_version) a proposito: permite mejorar la
-- extraccion de texto y comparar contra la anterior sin perder nada. La etapa 2
-- elige la mejor version disponible.
--
-- Se guardan TRES representaciones por pagina porque cada emisor responde a una
-- distinta y probar cuesta cero (es texto):
--   text_layout -> extract_text(layout=True), preserva columnas. Clave en tablas.
--   text_flow   -> extract_text(), orden de lectura natural.
--   tables      -> extract_tables(), cuando la pagina tiene lineas de tabla.
-- ─────────────────────────────────────────────────────────────────────────────
create table app.document_texts (
  id                uuid    primary key default gen_random_uuid(),
  user_id           uuid    not null,
  document_id       uuid    not null,
  extractor         text    not null,          -- 'pdfplumber'
  extractor_version text    not null,          -- '0.11.4/layout+flow+tables'
  page_texts        jsonb   not null,          -- [{page, text_layout, text_flow, tables}]
  full_text         text    not null,
  char_count        integer not null check (char_count >= 0),
  -- Si es false, el PDF es un escaneo y este proyecto no lo procesa (todavia):
  -- el documento pasa a 'failed' con reason 'no_text_layer'.
  has_text_layer    boolean not null,
  extracted_at      timestamptz not null default now(),

  unique (user_id, document_id, extractor, extractor_version),
  foreign key (user_id, document_id) references app.documents (user_id, id) on delete cascade
);

create index document_texts_lookup_idx
  on app.document_texts (user_id, document_id, extracted_at desc);
