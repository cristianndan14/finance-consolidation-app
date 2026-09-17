-- ─────────────────────────────────────────────────────────────────────────────
-- Une los movimientos manuales a `v_tx_enriched` y agrega la vista devengada
-- agregada por mes (paralela a `v_monthly_cashflow`, pero por fecha de compra).
--
-- # Por que se unen ACA y no en cada vista de mas arriba
--
-- `v_monthly_cashflow`, `v_accrual` y `v_installment_forward` ya leen de
-- `v_tx_enriched`. Agregando los movimientos manuales una sola vez, en el nivel
-- mas bajo, las tres heredan la union sin tocarlas — y sin poder olvidarse de
-- alguna. `v_accrual` en particular ya calcula el mes por
-- `coalesce(purchase_date, transaction_date, posted_date)`; un movimiento
-- manual no tiene atraso de resumen, asi que alcanza con no setear
-- `purchase_date` para que caiga en el mismo mes en flujo de caja y en
-- devengado.
--
-- `create or replace view` exige que las columnas existentes no cambien de
-- nombre, tipo NI POSICION (ver la migracion `cashflow_category_slug`). Esta
-- vista mantiene exactamente el mismo orden de columnas que la definicion
-- original (`views.sql`): solo cambia el FROM, de un solo `select` a un
-- `union all` con la misma forma.
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
  coalesce(cat.kind, e.kind)          as category_kind
from app.manual_entries e
  left join app.categories cat on cat.id = e.category_id;

comment on view app.v_tx_enriched is
  'Transacciones de resumen + movimientos manuales, ya unidos. signed_amount ya lleva el signo.';

-- ─────────────────────────────────────────────────────────────────────────────
-- Vista devengada a nivel de fila: mismo mes que usa `v_accrual` (mes de la
-- COMPRA, no el mes en que cae la cuota en el resumen), mas `category_kind` y
-- `category_slug`, que `v_accrual` no expone y hacen falta para poder filtrar
-- consumo real igual que hace `v_monthly_cashflow`.
--
-- `v_monthly_accrual` agrega sobre esta, igual que `v_monthly_cashflow` agrega
-- sobre `v_tx_enriched`: la logica de cuotas (compra completa en la primera
-- cuota) vive en un solo lugar.
-- ─────────────────────────────────────────────────────────────────────────────
create view app.v_accrual_enriched with (security_invoker = true) as
select
  t.id,
  t.user_id,
  extract(year  from coalesce(t.purchase_date, t.transaction_date, t.posted_date))::smallint
    as period_year,
  extract(month from coalesce(t.purchase_date, t.transaction_date, t.posted_date))::smallint
    as period_month,
  t.currency,
  t.category_id,
  t.category_slug,
  t.category_name,
  t.category_kind,
  t.merchant_name,
  (
    case
      when t.installment_total is not null and t.installment_total > 1
        then coalesce(t.purchase_total_amount, t.amount * t.installment_total)
      else t.amount
    end * t.direction
  ) as signed_amount
from app.v_tx_enriched t
where t.statement_status = 'confirmed'
  and t.review_status <> 'rejected'
  and (t.installment_number is null or t.installment_number = 1);

comment on view app.v_accrual_enriched is
  'Como v_tx_enriched pero con el mes de la compra real y el monto devengado (cuota completa en la primera).';

create view app.v_monthly_accrual with (security_invoker = true) as
select
  user_id, period_year, period_month, currency,
  category_id, category_slug, category_name, category_kind,
  count(*)           as tx_count,
  sum(signed_amount) as total
  from app.v_accrual_enriched
 group by user_id, period_year, period_month, currency,
          category_id, category_slug, category_name, category_kind;

comment on view app.v_monthly_accrual is
  'Igual que v_monthly_cashflow, pero agrupado por el mes de la compra real, no el mes del resumen.';

-- Misma conversion a moneda base que `v_monthly_base`, sobre la vista devengada.
create view app.v_monthly_base_accrual with (security_invoker = true) as
select
  f.user_id,
  f.period_year,
  f.period_month,
  f.currency,
  f.category_id,
  f.category_name,
  f.category_kind,
  f.tx_count,
  f.total,
  p.base_currency,
  case
    when f.currency = p.base_currency then f.total
    else f.total * fx.rate
  end as total_base,
  case
    when f.currency = p.base_currency then null
    else fx.rate
  end as fx_rate_used
from app.v_monthly_accrual f
  join app.profiles p on p.id = f.user_id
  left join lateral (
    select r.rate
      from app.fx_rates r
     where r.base_currency  = f.currency
       and r.quote_currency = p.base_currency
       and r.period_year    = f.period_year
       and r.period_month   = f.period_month
       and (r.user_id = f.user_id or r.user_id is null)
     order by r.user_id nulls last
     limit 1
  ) fx on true;

grant select on app.v_accrual_enriched, app.v_monthly_accrual, app.v_monthly_base_accrual
  to authenticated;
