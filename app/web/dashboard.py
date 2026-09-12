"""El dashboard: el mes, en qué se fue y qué queda comprometido.

# Lo que el dashboard elige NO mostrar

Solo agrega resúmenes **confirmados**. Un borrador puede tener transacciones que
el modelo leyó mal y que nadie revisó; incluirlas daría un número plausible y
equivocado. Un dashboard vacío que dice "confirmá el resumen de agosto" es más
útil que uno lleno de cifras que no se sabe si son ciertas — la regla se aplica
en las vistas del esquema, no acá, para que ninguna consulta pueda saltearla.

# Por qué el consumo no es la suma de todo

`category_kind` separa el gasto de lo que cobran el banco y el fisco. En un
resumen argentino, impuestos + intereses + comisiones son cerca de un tercio de
las líneas. El dashboard los muestra, pero aparte: mezclarlos hace que el total
del mes no se parezca a lo que la persona cree que gastó.

# Los gráficos

Se dibujan con ECharts desde un endpoint JSON, no renderizando SVG en el
servidor: los divs de una imagen generada no sobreviven bien a los swaps de HTMX,
y un JSON chico permite cambiar de mes sin recargar la página.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Request
from starlette.responses import JSONResponse, Response

from app.deps import CurrentProfileDep, CurrentUserDep, SettingsDep
from app.infra import db
from app.repositories import analytics
from app.security import csrf
from app.web.templates import render

router = APIRouter(tags=["dashboard"])

# Cuántas categorías se listan antes de agrupar el resto.
TOP_CATEGORIES = 8


@router.get("/", include_in_schema=False)
async def dashboard(
    request: Request,
    user: CurrentUserDep,
    profile: CurrentProfileDep,
    settings: SettingsDep,
    year: int | None = None,
    month: int | None = None,
) -> Response:
    async with db.user_tx(user.db_claims) as conn:
        periods = await analytics.available_periods(conn)
        period = _pick(periods, year, month)

        if period is None:
            return render(
                request,
                "dashboard/empty.html",
                {"profile": profile, "csrf_token": csrf.issue(user.user_id, settings)},
            )

        current = await _snapshot(conn, period)
        previous = await _snapshot(conn, period.previous())
        forward = await analytics.forward_installments(conn)
        pending = await analytics.uncategorized_count(conn, period)

    return render(
        request,
        "dashboard/index.html",
        {
            "profile": profile,
            "period": period,
            "periods": periods,
            "current": current,
            "previous": previous,
            "deltas": _deltas(current, previous),
            "forward": forward,
            "forward_by_currency": _forward_totals(forward),
            "uncategorized": pending,
            "csrf_token": csrf.issue(user.user_id, settings),
        },
    )


@router.get("/api/dashboard/categories", include_in_schema=False)
async def categories_json(
    user: CurrentUserDep, year: int, month: int, currency: str = "ARS"
) -> Response:
    """Los datos del gráfico de categorías. Los dibuja ECharts en el browser."""
    period = analytics.Period(year=year, month=month)

    async with db.user_tx(user.db_claims) as conn:
        rows = await analytics.by_category(conn, period)

    data = [
        {"name": row.category_name, "value": float(abs(row.total))}
        for row in rows
        if row.currency == currency.upper() and row.total > 0
    ]
    return JSONResponse({"currency": currency.upper(), "data": data})


@router.get("/api/dashboard/trend", include_in_schema=False)
async def trend_json(user: CurrentUserDep, currency: str = "ARS", months: int = 12) -> Response:
    """Consumo mes a mes, para el gráfico de evolución."""
    async with db.user_tx(user.db_claims) as conn:
        periods = await analytics.available_periods(conn, limit=months)
        series = []
        # Del más viejo al más nuevo: un gráfico de evolución se lee así.
        for period in reversed(periods):
            totals = await analytics.totals_by_kind(conn, period)
            spending = sum(
                (
                    t.total
                    for t in totals
                    if t.currency == currency.upper() and t.category_kind == "expense"
                ),
                Decimal("0"),
            )
            series.append({"period": str(period), "value": float(spending)})

    return JSONResponse({"currency": currency.upper(), "series": series})


# ─────────────────────────────────────────────────────────────────────────────
# Interno
# ─────────────────────────────────────────────────────────────────────────────
def _pick(
    periods: list[analytics.Period], year: int | None, month: int | None
) -> analytics.Period | None:
    """El mes pedido, o el más reciente con datos.

    No se cae al mes actual del calendario: si todavía no subiste el resumen de
    este mes, mostrar un dashboard en cero sería un dato falso sobre tu consumo.
    """
    if year and month:
        return analytics.Period(year=year, month=month)
    if periods:
        return periods[0]
    return None


async def _snapshot(conn: Any, period: analytics.Period) -> dict[str, Any]:
    """Todo lo que se muestra de un mes."""
    kinds = await analytics.totals_by_kind(conn, period)
    categories = await analytics.by_category(conn, period)
    merchants = await analytics.by_merchant(conn, period)
    base = await analytics.in_base_currency(conn, period)

    return {
        "period": period,
        "spending": _by_currency(kinds, "expense"),
        "taxes": _by_currency(kinds, "tax"),
        "fees": _by_currency(kinds, "fee"),
        "transfers": _by_currency(kinds, "transfer"),
        "categories": _top(categories),
        "merchants": merchants,
        "base": base,
        "currencies": sorted({k.currency for k in kinds}),
        "has_data": bool(kinds),
    }


def _by_currency(totals: list[analytics.KindTotal], kind: str) -> dict[str, Decimal]:
    return {t.currency: t.total for t in totals if t.category_kind == kind}


def _top(rows: list[analytics.CategoryTotal]) -> dict[str, list[analytics.CategoryTotal]]:
    """Las categorías por moneda, recortadas a las que importan."""
    grouped: dict[str, list[analytics.CategoryTotal]] = {}
    for row in rows:
        grouped.setdefault(row.currency, []).append(row)
    return {currency: items[:TOP_CATEGORIES] for currency, items in grouped.items()}


def _deltas(current: dict[str, Any], previous: dict[str, Any]) -> dict[str, Decimal | None]:
    """Variación del consumo contra el mes anterior, por moneda.

    `None` cuando el mes anterior no tiene datos: un 100% de aumento contra un
    mes que nunca se cargó no es información, es ruido.
    """
    deltas: dict[str, Decimal | None] = {}
    for currency, total in current["spending"].items():
        before = previous["spending"].get(currency)
        deltas[currency] = None if not before else total - before
    return deltas


def _forward_totals(rows: list[analytics.ForwardInstallment]) -> dict[str, Decimal]:
    totals: dict[str, Decimal] = {}
    for row in rows:
        totals[row.currency] = totals.get(row.currency, Decimal("0")) + row.remaining_amount
    return totals


def current_period() -> analytics.Period:
    now = datetime.now(UTC)
    return analytics.Period(year=now.year, month=now.month)
