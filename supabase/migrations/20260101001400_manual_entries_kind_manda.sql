-- ─────────────────────────────────────────────────────────────────────────────
-- En un movimiento manual, el `kind` del movimiento decide `category_kind`,
-- nunca la categoria elegida.
--
-- `manual_entries_en_vistas_y_devengado` armaba la rama manual de
-- `v_tx_enriched` con `coalesce(cat.kind, e.kind)`: la categoria pisaba el tipo
-- que eligio el usuario. Como el signo (`direction`/`signed_amount`) si sale de
-- `e.kind`, un ingreso bajo una categoria de gasto (p. ej. la de sistema
-- "Otros") terminaba contado como un gasto NEGATIVO en `v_monthly_cashflow` /
-- `v_monthly_accrual` (achicando el consumo en vez de sumar al ingreso), y un
-- gasto bajo "Devoluciones" (income) sumaba del lado del ingreso.
--
-- El comentario de `app.manual_entries.kind` en `manual_entries.sql` ("una
-- categoria de sistema como 'otros' sirve para las dos") describe la intencion
-- -- `kind` es la unica entrada de signo -- pero la vista no la cumplia. Con
-- este cambio `category_kind` y `signed_amount` salen los dos de `e.kind`, asi
-- que siempre son coherentes entre si, igual que en una transaccion de resumen
-- (donde un `kind = 'income'` lleva `signed_amount` negativo). La categoria
-- queda solo como etiqueta (`category_id`/`slug`/`name`). Ademas, la web ya
-- rechaza combinar un tipo con una categoria del tipo contrario.
--
-- `create or replace view` alcanza: solo cambia la expresion de
-- `category_kind`, que sigue siendo `text` y en la misma posicion, asi que
-- `v_monthly_cashflow`, `v_monthly_base`, `v_accrual`, `v_accrual_enriched`,
-- `v_monthly_accrual`, `v_monthly_base_accrual` y `v_installment_forward` no
-- se tocan, y los grants existentes sobre la vista se conservan.
-- ─────────────────────────────────────────────────────────────────────────────
create or replace view app.v_tx_enriched with (security_invoker = true) as
select
  t.id,
  t.user_id,
  t.statement_id,
  t.posted_date,
  t.transaction_date,
  t.description_raw,
  t.amount,
  t.currency,
  t.direction,
  (t.amount * t.direction)            as signed_amount,
  t.kind,
  t.installment_number,
  t.installment_total,
  t.purchase_total_amount,
  t.purchase_date,
  t.installment_group_key,
  t.is_recurring,
  t.confidence,
  t.review_status,
  s.period_year,
  s.period_month,
  s.status                            as statement_status,
  c.id                                as card_id,
  c.issuer_slug,
  c.issuer_name,
  c.last4,
  m.id                                as merchant_id,
  coalesce(m.canonical_name, t.description_raw) as merchant_name,
  cat.id                              as category_id,
  cat.slug                            as category_slug,
  coalesce(cat.name, 'Sin categoria') as category_name,
  coalesce(cat.kind, 'expense')       as category_kind
from app.transactions t
  join app.statements s   on s.id = t.statement_id  and s.user_id = t.user_id
  left join app.cards c      on c.id = t.card_id     and c.user_id = t.user_id
  left join app.merchants m  on m.id = t.merchant_id and m.user_id = t.user_id
  left join app.categories cat on cat.id = t.category_id

union all

select
  e.id,
  e.user_id,
  null::uuid                          as statement_id,
  e.entry_date                        as posted_date,
  null::date                          as transaction_date,
  e.description                       as description_raw,
  e.amount,
  e.currency,
  -- 'income' baja lo consumido (como una devolucion); 'expense' lo sube.
  (case when e.kind = 'income' then -1 else 1 end)::smallint as direction,
  (case when e.kind = 'income' then -e.amount else e.amount end) as signed_amount,
  e.kind,
  null::smallint                      as installment_number,
  null::smallint                      as installment_total,
  null::numeric(14, 2)                as purchase_total_amount,
  null::date                          as purchase_date,
  null::text                          as installment_group_key,
  false                               as is_recurring,
  null::numeric(3, 2)                 as confidence,
  -- Carga manual: no pasa por revision, se confirma sola.
  'confirmed'                         as review_status,
  extract(year  from e.entry_date)::smallint as period_year,
  extract(month from e.entry_date)::smallint as period_month,
  'confirmed'                         as statement_status,
  null::uuid                          as card_id,
  null::text                          as issuer_slug,
  null::text                          as issuer_name,
  null::char(4)                       as last4,
  null::uuid                          as merchant_id,
  e.description                       as merchant_name,
  cat.id                              as category_id,
  cat.slug                            as category_slug,
  coalesce(cat.name, 'Sin categoria') as category_name,
  -- El tipo del movimiento manda (es el mismo que decide el signo).
  e.kind                              as category_kind
from app.manual_entries e
  left join app.categories cat on cat.id = e.category_id;

comment on view app.v_tx_enriched is
  'Transacciones de resumen + movimientos manuales, ya unidos. signed_amount ya lleva el signo.';
