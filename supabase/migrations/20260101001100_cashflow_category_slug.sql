-- ─────────────────────────────────────────────────────────────────────────────
-- `v_monthly_cashflow` no exponia `category_slug`, pero
-- `app/repositories/analytics.py::by_category` lo consulta ahi (no en
-- `v_tx_enriched`, que si lo tiene) para agrupar por categoria en el
-- dashboard. Sin esta columna, cualquier mes con transacciones categorizadas
-- rompia `/` con un `UndefinedColumnError` -- nunca se detecto porque hasta
-- ahora no habia ni templates de dashboard ni datos confirmados para llegar a
-- ese query.
--
-- `create or replace view` es seguro aca siempre que las columnas existentes
-- no cambien de nombre, tipo NI POSICION: `category_slug` tiene que ir
-- despues de todas las columnas que ya existian (`total` es la ultima), o
-- postgres lo interpreta como un rename de la columna que ocupaba ese lugar
-- en vez de una columna nueva. `v_monthly_base` lee de esta vista por nombre
-- de columna explicito y no pide `category_slug`, asi que agregarla al final
-- no le afecta.
-- ─────────────────────────────────────────────────────────────────────────────
create or replace view app.v_monthly_cashflow with (security_invoker = true) as
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
  sum(signed_amount)      as total,
  category_slug
from app.v_tx_enriched
where statement_status = 'confirmed'
  and review_status <> 'rejected'
group by user_id, period_year, period_month, currency,
         category_id, category_slug, category_name, category_kind, kind;
