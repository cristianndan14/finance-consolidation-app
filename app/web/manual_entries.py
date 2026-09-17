"""`/movements`: ingresos y gastos por fuera del resumen de tarjeta.

Carga directa, sin pipeline de LLM ni revision: lo que el usuario tipea es lo
que se guarda. La unica validacion es que el monto se pueda interpretar y que
la categoria (si se eligio una) sea visible para el usuario.
"""

from __future__ import annotations

from datetime import date
from typing import Annotated
from urllib.parse import quote

from fastapi import APIRouter, Form, Request
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

    try:
        value = parse_amount(amount)
    except AmountParseError as exc:
        return _back(f"Monto inválido: {exc}")

    if value <= 0:
        return _back("El monto tiene que ser mayor que cero.")

    async with db.user_tx(user.db_claims) as conn:
        await entries_repo.create(
            conn,
            entry_date=entry_date,
            kind=kind,
            description=description,
            amount=value,
            currency=currency,
            category_id=category_id or None,
            notes=notes.strip() or None,
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

    async with db.user_tx(user.db_claims) as conn:
        await entries_repo.delete(conn, entry_id)

    return RedirectResponse(MOVEMENTS_PATH, status_code=303)


def _back(message: str) -> RedirectResponse:
    return RedirectResponse(f"{MOVEMENTS_PATH}?error={quote(message)}", status_code=303)
