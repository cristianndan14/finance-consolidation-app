"""`/settings`: tipo de cambio del mes y moneda base.

El tipo de cambio se carga a mano y por mes, no se consulta a una API. Cuál
cotización usar —oficial, MEP, la que el banco efectivamente aplicó— es una
decisión del usuario que cambia el total del mes en un 40%, y no hay una
respuesta correcta que el sistema pueda elegir solo.
"""

from __future__ import annotations

from datetime import date
from urllib.parse import quote
from typing import Annotated

from fastapi import APIRouter, Form, Request
from starlette.responses import RedirectResponse, Response

from app.deps import CurrentProfileDep, CurrentUserDep, SettingsDep
from app.domain.money import AmountParseError, parse_amount
from app.infra import db
from app.repositories import fx as fx_repo
from app.security import csrf
from app.web.templates import render

router = APIRouter(prefix="/settings", tags=["settings"])

SETTINGS_PATH = "/settings"


@router.get("", include_in_schema=False)
async def settings_page(
    request: Request,
    user: CurrentUserDep,
    profile: CurrentProfileDep,
    settings: SettingsDep,
    error: str = "",
) -> Response:
    async with db.user_tx(user.db_claims) as conn:
        rates = await fx_repo.list_all(conn)

    today = date.today()  # noqa: DTZ011 - solo para prellenar el formulario
    return render(
        request,
        "settings/index.html",
        {
            "profile": profile,
            "rates": rates,
            "default_year": today.year,
            "default_month": today.month,
            "error": error or None,
            "csrf_token": csrf.issue(user.user_id, settings),
        },
    )


@router.post("/fx", include_in_schema=False)
async def save_rate(
    request: Request,
    user: CurrentUserDep,
    settings: SettingsDep,
    period_year: Annotated[int, Form()],
    period_month: Annotated[int, Form()],
    rate: Annotated[str, Form()],
    base_currency: Annotated[str, Form()] = "USD",
    quote_currency: Annotated[str, Form()] = "ARS",
    csrf_token: Annotated[str, Form()] = "",
) -> Response:
    """Guarda la cotización de un mes. Reemplaza la que hubiera."""
    csrf.verify(csrf_token, user.user_id, settings)

    try:
        value = parse_amount(rate)
    except AmountParseError as exc:
        return _back(f"Cotización inválida: {exc}")

    if value <= 0:
        return _back("La cotización tiene que ser mayor que cero.")
    if not 1 <= period_month <= 12:
        return _back("El mes tiene que estar entre 1 y 12.")
    if base_currency.upper() == quote_currency.upper():
        return _back("Las dos monedas no pueden ser la misma.")

    async with db.user_tx(user.db_claims) as conn:
        await fx_repo.upsert(
            conn,
            period_year=period_year,
            period_month=period_month,
            base_currency=base_currency,
            quote_currency=quote_currency,
            rate=value,
            as_of=None,
        )

    return RedirectResponse(SETTINGS_PATH, status_code=303)


@router.post("/fx/{rate_id}/delete", include_in_schema=False)
async def delete_rate(
    request: Request,
    user: CurrentUserDep,
    settings: SettingsDep,
    rate_id: str,
    csrf_token: Annotated[str, Form()] = "",
) -> Response:
    csrf.verify(csrf_token, user.user_id, settings)

    async with db.user_tx(user.db_claims) as conn:
        await fx_repo.delete(conn, rate_id)

    return RedirectResponse(SETTINGS_PATH, status_code=303)


def _back(message: str) -> RedirectResponse:
    return RedirectResponse(f"{SETTINGS_PATH}?error={quote(message)}", status_code=303)
