-- ─────────────────────────────────────────────────────────────────────────────
-- Vistas de consulta para el dashboard.
--
-- `security_invoker = true` (Postgres 15+) es obligatorio: sin eso, una vista
-- corre con los permisos de QUIEN LA CREO (el owner del schema), y como el owner
-- no esta sujeto a las politicas, la vista se convertiria en un agujero que
-- expone las filas de todos los usuarios. Con security_invoker, las tablas base
-- se leen con la identidad del que consulta y RLS aplica normalmente.
-- ─────────────────────────────────────────────────────────────────────────────

-- Transacciones con todo lo que el dashboard necesita mostrar, ya unido.
create view app.v_tx_enriched with (security_invoker = true) as
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
  -- Monto con signo: negativo para pagos y devoluciones. Sirve para sumar.
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
  left join app.categories cat on cat.id = t.category_id;

comment on view app.v_tx_enriched is
  'Transacciones con tarjeta, comercio, categoria y periodo. signed_amount ya lleva el signo.';

-- ─────────────────────────────────────────────────────────────────────────────
-- Flujo de caja mensual por categoria y moneda.
--
-- Solo statements CONFIRMADOS: un borrador puede tener transacciones que el LLM
-- leyo mal y que el usuario todavia no reviso. Mostrarlas daria un numero
-- plausible y equivocado, que es peor que no mostrar nada.
-- ─────────────────────────────────────────────────────────────────────────────
create view app.v_monthly_cashflow with (security_invoker = true) as
select
  user_id,
  period_year,
  period_month,
  currency,
  category_id,
  category_name,
  category_kind,
  kind,
  count(*)                as tx_count,
  sum(signed_amount)      as total
from app.v_tx_enriched
where statement_status = 'confirmed'
  and review_status <> 'rejected'
group by user_id, period_year, period_month, currency,
         category_id, category_name, category_kind, kind;

-- ─────────────────────────────────────────────────────────────────────────────
-- Lo mismo, consolidado en la moneda base del usuario.
--
-- La conversion usa el tipo de cambio del MISMO mes que el resumen, no el de hoy:
-- un consumo en dolares de marzo se compara con el resto de marzo. Si falta la
-- cotizacion del mes, la fila sale con `total_base` NULL y el dashboard lo avisa
-- en lugar de inventar un numero.
-- ─────────────────────────────────────────────────────────────────────────────
create view app.v_monthly_base with (security_invoker = true) as
select
  f.user_id,
  f.period_year,
  f.period_month,
  f.currency,
  f.category_id,
  f.category_name,
  f.category_kind,
  f.kind,
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
from app.v_monthly_cashflow f
  join app.profiles p on p.id = f.user_id
  left join lateral (
    select r.rate
      from app.fx_rates r
     where r.base_currency  = f.currency
       and r.quote_currency = p.base_currency
       and r.period_year    = f.period_year
       and r.period_month   = f.period_month
       and (r.user_id = f.user_id or r.user_id is null)
     -- La cotizacion cargada por el usuario gana sobre la global.
     order by r.user_id nulls last
     limit 1
  ) fx on true;

-- ─────────────────────────────────────────────────────────────────────────────
-- Vista devengada: el gasto completo impacta en el mes de la COMPRA.
--
-- El modelo principal del proyecto es flujo de caja (la cuota pega en el mes que
-- se paga). Esta vista existe para la pregunta complementaria: "cuanto gaste
-- realmente en marzo", contando una compra en 12 cuotas entera en marzo.
-- ─────────────────────────────────────────────────────────────────────────────
create view app.v_accrual with (security_invoker = true) as
select
  t.user_id,
  extract(year  from coalesce(t.purchase_date, t.transaction_date, t.posted_date))::smallint
    as accrual_year,
  extract(month from coalesce(t.purchase_date, t.transaction_date, t.posted_date))::smallint
    as accrual_month,
  t.currency,
  t.category_id,
  t.category_name,
  t.merchant_name,
  -- En una compra en cuotas el gasto devengado es el total, y se cuenta una sola
  -- vez: en la primera cuota. Las demas ya estan representadas por ese total.
  case
    when t.installment_total is not null and t.installment_total > 1
      then coalesce(t.purchase_total_amount, t.amount * t.installment_total)
    else t.amount
  end * t.direction as accrued_amount
from app.v_tx_enriched t
where t.statement_status = 'confirmed'
  and t.review_status <> 'rejected'
  and (t.installment_number is null or t.installment_number = 1);

-- ─────────────────────────────────────────────────────────────────────────────
-- Cuotas futuras comprometidas: lo que ya se debe pero todavia no se pago.
--
-- Es el numero que no aparece en ningun resumen y que mas importa para no
-- llevarse una sorpresa: cuanto de los proximos meses ya esta gastado.
-- ─────────────────────────────────────────────────────────────────────────────
create view app.v_installment_forward with (security_invoker = true) as
select
  t.user_id,
  t.installment_group_key,
  t.currency,
  t.merchant_name,
  t.category_name,
  t.card_id,
  t.issuer_name,
  max(t.installment_total)                          as installment_total,
  max(t.installment_number)                         as installments_paid,
  max(t.installment_total) - max(t.installment_number) as installments_left,
  max(t.amount)                                     as installment_amount,
  max(t.amount) * (max(t.installment_total) - max(t.installment_number))
                                                    as remaining_amount,
  max(t.posted_date)                                as last_posted_date
from app.v_tx_enriched t
where t.statement_status = 'confirmed'
  and t.review_status <> 'rejected'
  and t.installment_group_key is not null
  and t.installment_total > 1
group by t.user_id, t.installment_group_key, t.currency, t.merchant_name,
         t.category_name, t.card_id, t.issuer_name
having max(t.installment_number) < max(t.installment_total);

grant select on app.v_tx_enriched, app.v_monthly_cashflow, app.v_monthly_base,
                app.v_accrual, app.v_installment_forward
  to authenticated;
