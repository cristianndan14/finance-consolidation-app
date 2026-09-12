"""Las consultas del dashboard, sobre las vistas del esquema.

# Por que todo sale de vistas y no de queries armadas aca

Las vistas llevan `security_invoker = true`, asi que se leen con la identidad de
quien consulta y RLS aplica igual que en una tabla. Y encapsulan las dos reglas
que no se pueden olvidar en ningun query del dashboard:

- **solo `statement_status = 'confirmed'`** — un borrador puede tener
  transacciones que el modelo leyo mal y que nadie reviso todavia. Mostrarlas
  daria un numero plausible y equivocado, que es peor que no mostrar nada.
- **`review_status <> 'rejected'`** — lo que el usuario descarto no suma.

Si esas reglas vivieran en cada query, alcanzaria con que una se olvidara para
que el dashboard mintiera en una pantalla.

# Por que el consumo no es la suma de todo

`category_kind` separa el gasto real de lo que cobra el banco y el fisco. En un
resumen argentino, impuestos + intereses + comisiones son cerca de un tercio de
las lineas; sumarlos al consumo hace que el total del mes no se parezca a lo que
la persona cree que gasto. El dashboard los muestra, pero aparte.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

# Los `kind` de categoria que cuentan como consumo de la persona.
SPENDING_KINDS = ("expense",)

_MONTHS = text(
    """
    select distinct period_year, period_month
      from app.v_monthly_cashflow
     order by period_year desc, period_month desc
     limit :limit
    """
)

_MONTH_TOTALS = text(
    """
    select currency,
           category_kind,
           sum(total)     as total,
           sum(tx_count)  as tx_count
      from app.v_monthly_cashflow
     where period_year = :year and period_month = :month
     group by currency, category_kind
     order by currency, category_kind
    """
)

_BY_CATEGORY = text(
    """
    select currency,
           category_slug,
           category_name,
           category_kind,
           sum(total)    as total,
           sum(tx_count) as tx_count
      from app.v_monthly_cashflow
     where period_year = :year and period_month = :month
       and category_kind = any(cast(:kinds as text[]))
     group by currency, category_slug, category_name, category_kind
     having sum(total) <> 0
     order by sum(total) desc
    """
)

_BY_MERCHANT = text(
    """
    select currency, merchant_name, sum(signed_amount) as total, count(*) as tx_count
      from app.v_tx_enriched
     where period_year = :year and period_month = :month
       and statement_status = 'confirmed'
       and review_status <> 'rejected'
       and category_kind = any(cast(:kinds as text[]))
     group by currency, merchant_name
     having sum(signed_amount) <> 0
     order by sum(signed_amount) desc
     limit :limit
    """
)

_IN_BASE = text(
    """
    select currency,
           sum(total)      as total,
           sum(total_base) as total_base,
           max(base_currency) as base_currency,
           bool_or(total_base is null) as missing_rate
      from app.v_monthly_base
     where period_year = :year and period_month = :month
       and category_kind = any(cast(:kinds as text[]))
     group by currency
    """
)

_FORWARD = text(
    """
    select currency, merchant_name, category_name, installment_total,
           installments_paid, installments_left, installment_amount,
           remaining_amount, last_posted_date
      from app.v_installment_forward
     order by remaining_amount desc
     limit :limit
    """
)

_UNCATEGORIZED = text(
    """
    select count(*) as pending
      from app.v_tx_enriched
     where period_year = :year and period_month = :month
       and statement_status = 'confirmed'
       and review_status <> 'rejected'
       and category_id is null
    """
)


@dataclass(frozen=True)
class Period:
    year: int
    month: int

    def previous(self) -> Period:
        return Period(self.year - 1, 12) if self.month == 1 else Period(self.year, self.month - 1)

    def __str__(self) -> str:
        return f"{self.month:02d}/{self.year}"


@dataclass(frozen=True)
class KindTotal:
    currency: str
    category_kind: str
    total: Decimal
    tx_count: int


@dataclass(frozen=True)
class CategoryTotal:
    currency: str
    category_slug: str | None
    category_name: str
    category_kind: str
    total: Decimal
    tx_count: int


@dataclass(frozen=True)
class MerchantTotal:
    currency: str
    merchant_name: str
    total: Decimal
    tx_count: int


@dataclass(frozen=True)
class BaseTotal:
    """El total de una moneda, y su conversion a la moneda base si se puede."""

    currency: str
    total: Decimal
    total_base: Decimal | None
    base_currency: str | None
    missing_rate: bool


@dataclass(frozen=True)
class ForwardInstallment:
    currency: str
    merchant_name: str
    category_name: str
    installment_total: int
    installments_paid: int
    installments_left: int
    installment_amount: Decimal
    remaining_amount: Decimal
    last_posted_date: date


async def available_periods(conn: AsyncConnection, *, limit: int = 36) -> list[Period]:
    """Los meses que tienen datos confirmados, del mas nuevo al mas viejo."""
    rows = (await conn.execute(_MONTHS, {"limit": limit})).mappings()
    return [Period(year=row["period_year"], month=row["period_month"]) for row in rows]


async def totals_by_kind(conn: AsyncConnection, period: Period) -> list[KindTotal]:
    rows = (
        await conn.execute(_MONTH_TOTALS, {"year": period.year, "month": period.month})
    ).mappings()
    return [
        KindTotal(
            currency=row["currency"],
            category_kind=row["category_kind"],
            total=Decimal(str(row["total"])),
            tx_count=int(row["tx_count"]),
        )
        for row in rows
    ]


async def by_category(
    conn: AsyncConnection, period: Period, *, kinds: tuple[str, ...] = SPENDING_KINDS
) -> list[CategoryTotal]:
    rows = (
        await conn.execute(
            _BY_CATEGORY, {"year": period.year, "month": period.month, "kinds": list(kinds)}
        )
    ).mappings()
    return [
        CategoryTotal(
            currency=row["currency"],
            category_slug=row["category_slug"],
            category_name=row["category_name"],
            category_kind=row["category_kind"],
            total=Decimal(str(row["total"])),
            tx_count=int(row["tx_count"]),
        )
        for row in rows
    ]


async def by_merchant(
    conn: AsyncConnection,
    period: Period,
    *,
    limit: int = 10,
    kinds: tuple[str, ...] = SPENDING_KINDS,
) -> list[MerchantTotal]:
    rows = (
        await conn.execute(
            _BY_MERCHANT,
            {"year": period.year, "month": period.month, "limit": limit, "kinds": list(kinds)},
        )
    ).mappings()
    return [
        MerchantTotal(
            currency=row["currency"],
            merchant_name=row["merchant_name"],
            total=Decimal(str(row["total"])),
            tx_count=int(row["tx_count"]),
        )
        for row in rows
    ]


async def in_base_currency(
    conn: AsyncConnection, period: Period, *, kinds: tuple[str, ...] = SPENDING_KINDS
) -> list[BaseTotal]:
    """Totales por moneda y su conversion. `missing_rate` es lo que la UI avisa.

    Sin cotizacion cargada para el mes, `total_base` viene NULL a proposito: es
    preferible mostrar los dos totales por separado y decir que falta el tipo de
    cambio, antes que inventar una conversion con la cotizacion de hoy para un
    consumo de hace seis meses.
    """
    rows = (
        await conn.execute(
            _IN_BASE, {"year": period.year, "month": period.month, "kinds": list(kinds)}
        )
    ).mappings()
    return [
        BaseTotal(
            currency=row["currency"],
            total=Decimal(str(row["total"])),
            total_base=Decimal(str(row["total_base"])) if row["total_base"] is not None else None,
            base_currency=row["base_currency"],
            missing_rate=bool(row["missing_rate"]),
        )
        for row in rows
    ]


async def forward_installments(
    conn: AsyncConnection, *, limit: int = 20
) -> list[ForwardInstallment]:
    """Lo que ya se debe de los meses que vienen.

    Es el numero que no aparece en ningun resumen: cuanto de los proximos meses
    ya esta comprometido en cuotas.
    """
    rows = (await conn.execute(_FORWARD, {"limit": limit})).mappings()
    return [
        ForwardInstallment(
            currency=row["currency"],
            merchant_name=row["merchant_name"],
            category_name=row["category_name"],
            installment_total=int(row["installment_total"]),
            installments_paid=int(row["installments_paid"]),
            installments_left=int(row["installments_left"]),
            installment_amount=Decimal(str(row["installment_amount"])),
            remaining_amount=Decimal(str(row["remaining_amount"])),
            last_posted_date=row["last_posted_date"],
        )
        for row in rows
    ]


async def uncategorized_count(conn: AsyncConnection, period: Period) -> int:
    row = (
        (await conn.execute(_UNCATEGORIZED, {"year": period.year, "month": period.month}))
        .mappings()
        .one()
    )
    return int(row["pending"])
