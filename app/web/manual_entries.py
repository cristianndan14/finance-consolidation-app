"""`/movements`: ingresos y gastos por fuera del resumen de tarjeta.

Carga directa, sin pipeline de LLM ni revision: lo que el usuario tipea es lo
que se guarda. La unica validacion es que el monto se pueda interpretar y que
la categoria (si se eligio una) sea visible para el usuario y del mismo tipo
(ingreso/gasto) que el movimiento.
"""

from __future__ import annotations

import re
import uuid
from datetime import date
from decimal import Decimal
from typing import Annotated
from urllib.parse import quote

from fastapi import APIRouter, Form, HTTPException, Request
from starlette.responses import RedirectResponse, Response

from app.deps import CurrentProfileDep, CurrentUserDep, SettingsDep
from app.domain.money import AmountParseError, parse_amount
from app.infra import db
from app.repositories import categories as categories_repo
from app.repositories import manual_entries as entries_repo
from app.security import csrf
from app.web.templates import render

router = APIRouter(prefix="/movements", tags=["movements"])

MOVEMENTS_PATH = "/movements"

_CURRENCY_RE = re.compile(r"^[A-Z]{3}$")
_MAX_AMOUNT = (
    Decimal(10) ** 12
)  # numeric(14,2): 12 digitos enteros; parse_amount ya redondea a centavos
_MAX_DESCRIPTION = 200
_MAX_NOTES = 1000


@router.get("", include_in_schema=False)
async def list_movements(
    request: Request,
    user: CurrentUserDep,
    profile: CurrentProfileDep,
    settings: SettingsDep,
    error: str = "",
) -> Response:
    async with db.user_tx(user.db_claims) as conn:
        entries = await entries_repo.list_recent(conn)
        categories = await categories_repo.list_active(conn)

    return render(
        request,
        "movements/index.html",
        {
            "profile": profile,
            "entries": entries,
            "categories": categories,
            "default_date": date.today().isoformat(),  # noqa: DTZ011 - prellena el form
            "error": error or None,
            "csrf_token": csrf.issue(user.user_id, settings),
        },
    )


@router.post("", include_in_schema=False)
async def create_movement(
    request: Request,
    user: CurrentUserDep,
    settings: SettingsDep,
    entry_date: Annotated[date, Form()],
    kind: Annotated[str, Form()],
    description: Annotated[str, Form()],
    amount: Annotated[str, Form()],
    currency: Annotated[str, Form()] = "ARS",
    category_id: Annotated[str, Form()] = "",
    notes: Annotated[str, Form()] = "",
    csrf_token: Annotated[str, Form()] = "",
) -> Response:
    csrf.verify(csrf_token, user.user_id, settings)

    if kind not in ("income", "expense"):
        return _back("Elegí si es un ingreso o un gasto.")

    description = description.strip()
    if not description:
        return _back("La descripción no puede estar vacía.")
    if "\x00" in description or len(description) > _MAX_DESCRIPTION:
        return _back(f"La descripción no puede superar {_MAX_DESCRIPTION} caracteres.")
    notes = notes.strip()
    if "\x00" in notes or len(notes) > _MAX_NOTES:
        return _back(f"Las notas no pueden superar {_MAX_NOTES} caracteres.")

    try:
        value = parse_amount(amount)
    except AmountParseError as exc:
        return _back(f"Monto inválido: {exc}")

    if value <= 0:
        return _back("El monto tiene que ser mayor que cero.")
    if value >= _MAX_AMOUNT:
        return _back("El monto es demasiado grande.")

    currency = currency.strip().upper()
    if not _CURRENCY_RE.match(currency):
        return _back("La moneda tiene que ser un código de tres letras (ARS, USD).")

    category_uuid = _parse_uuid(category_id.strip()) if category_id.strip() else None
    if category_id.strip() and category_uuid is None:
        return _back("La categoría elegida no existe.")

    async with db.user_tx(user.db_claims) as conn:
        if category_uuid is not None:
            # RLS: una categoria privada de otro usuario no se ve, asi que
            # `get` devuelve None igual que si no existiera.
            category = await categories_repo.get(conn, str(category_uuid))
            if category is None:
                return _back("La categoría elegida no existe.")
            if category.kind != kind:
                return _back("La categoría no corresponde a un ingreso o gasto según lo elegido.")

        await entries_repo.create(
            conn,
            entry_date=entry_date,
            kind=kind,
            description=description,
            amount=value,
            currency=currency,
            category_id=str(category_uuid) if category_uuid else None,
            notes=notes or None,
        )

    return RedirectResponse(MOVEMENTS_PATH, status_code=303)


@router.post("/{entry_id}/delete", include_in_schema=False)
async def delete_movement(
    request: Request,
    user: CurrentUserDep,
    settings: SettingsDep,
    entry_id: str,
    csrf_token: Annotated[str, Form()] = "",
) -> Response:
    csrf.verify(csrf_token, user.user_id, settings)

    entry_uuid = _parse_uuid(entry_id)
    if entry_uuid is None:
        raise HTTPException(status_code=404)

    async with db.user_tx(user.db_claims) as conn:
        await entries_repo.delete(conn, str(entry_uuid))

    return RedirectResponse(MOVEMENTS_PATH, status_code=303)


def _back(message: str) -> RedirectResponse:
    return RedirectResponse(f"{MOVEMENTS_PATH}?error={quote(message)}", status_code=303)


def _parse_uuid(raw: str) -> uuid.UUID | None:
    try:
        return uuid.UUID(raw)
    except ValueError:
        return None
