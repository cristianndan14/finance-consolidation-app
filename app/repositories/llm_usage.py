"""Gasto de LLM acumulado por usuario y mes: `app.llm_usage`.

# Por que existe

Un bucle de reprocesamiento —un job que falla, se reintenta, vuelve a llamar al
modelo, vuelve a fallar— puede gastar mucho dinero rapido sin que nadie se
entere hasta la factura. El tope de `LLM_MONTHLY_BUDGET_USD` se consulta **antes**
de encolar y **antes** de cada corrida, y corta.

Es por usuario, no global: que un usuario reprocese su corpus entero no puede
dejar sin extracciones a los demas.

El tope es una red de seguridad, no un sistema de facturacion. Se acumula lo que
el proveedor informa (o lo que estima `llm/cost.py`), con la precision que eso
tenga.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

_RECORD = text(
    """
    insert into app.llm_usage (
        period_year, period_month, provider, model, calls, input_tokens, output_tokens, cost_usd
    )
    values (
        :period_year, :period_month, :provider, :model, 1, :input_tokens, :output_tokens, :cost_usd
    )
    on conflict (user_id, period_year, period_month, provider, model) do update
       set calls         = app.llm_usage.calls + 1,
           input_tokens  = app.llm_usage.input_tokens + excluded.input_tokens,
           output_tokens = app.llm_usage.output_tokens + excluded.output_tokens,
           cost_usd      = app.llm_usage.cost_usd + excluded.cost_usd
    returning cost_usd
    """
)

_MONTH_TOTAL = text(
    """
    select coalesce(sum(cost_usd), 0) as spent,
           coalesce(sum(calls), 0)    as calls
      from app.llm_usage
     where period_year = :period_year and period_month = :period_month
    """
)


@dataclass(frozen=True)
class MonthUsage:
    year: int
    month: int
    spent_usd: Decimal
    calls: int

    def remaining(self, budget: Decimal) -> Decimal:
        return max(budget - self.spent_usd, Decimal("0"))

    def exhausted(self, budget: Decimal) -> bool:
        return self.spent_usd >= budget


async def record(
    conn: AsyncConnection,
    *,
    year: int,
    month: int,
    provider: str,
    model: str,
    input_tokens: int,
    output_tokens: int,
    cost_usd: Decimal,
) -> Decimal:
    """Suma una llamada al acumulado del mes. Devuelve el total del par proveedor/modelo."""
    row = (
        (
            await conn.execute(
                _RECORD,
                {
                    "period_year": year,
                    "period_month": month,
                    "provider": provider,
                    "model": model,
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                    "cost_usd": cost_usd,
                },
            )
        )
        .mappings()
        .one()
    )
    return Decimal(str(row["cost_usd"]))


async def month_usage(conn: AsyncConnection, *, year: int, month: int) -> MonthUsage:
    row = (
        (await conn.execute(_MONTH_TOTAL, {"period_year": year, "period_month": month}))
        .mappings()
        .one()
    )
    return MonthUsage(
        year=year,
        month=month,
        spent_usd=Decimal(str(row["spent"])),
        calls=int(row["calls"]),
    )
