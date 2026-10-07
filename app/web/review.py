"""La pantalla de revision: `/statements/{id}/review`.

# Por que es la pantalla mas importante del proyecto

Es la que convierte "el sistema extrajo transacciones" en "los numeros del
dashboard son ciertos". Todo lo demas se puede rehacer; un mes confirmado con un
monto mal leido contamina el historico y nadie se entera.

# El patron de HTMX que se usa acá, y por que este y no otro

Cada accion (editar una fila, confirmar en bloque, dividir, agregar) devuelve el
**panel entero** —banner de cuadre + acciones + tabla— y no solo la fila tocada.

Es mas bytes por request y es a proposito: el banner de cuadre depende de la suma
de todas las filas, asi que devolver solo la fila editada dejaria el delta viejo
en pantalla. Un delta desactualizado en la pantalla que existe para verificar
numeros es exactamente el bug que esta fase tiene que no tener. Con ~200 filas y
un usuario, el costo del re-render no se nota.

Sin JavaScript la pantalla igual funciona: los mismos endpoints responden con un
redirect 303 a la pagina completa cuando la request no viene de HTMX.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from fastapi import APIRouter, Form, Request
from starlette.datastructures import FormData
from starlette.responses import RedirectResponse, Response

from app.deps import CurrentProfileDep, CurrentUserDep, SettingsDep
from app.infra import db
from app.logging_config import get_logger
from app.repositories import categories as categories_repo
from app.repositories import documents as documents_repo
from app.security import csrf
from app.services import enrich as enrich_service
from app.services import review as review_service
from app.web.templates import is_htmx, render

log = get_logger(__name__)

router = APIRouter(prefix="/statements", tags=["review"])


# ─────────────────────────────────────────────────────────────────────────────
# La pagina
# ─────────────────────────────────────────────────────────────────────────────
@router.get("/{statement_id}/review", include_in_schema=False)
async def review_page(
    request: Request,
    user: CurrentUserDep,
    profile: CurrentProfileDep,
    settings: SettingsDep,
    statement_id: str,
    edit: str | None = None,
) -> Response:
    """`?edit=<id>` abre esa fila en modo edicion: es el camino sin JavaScript
    del link "Editar". Un id ajeno o inexistente no coincide con ninguna fila y
    la pagina queda entera en lectura."""
    async with db.user_tx(user.db_claims) as conn:
        state = await review_service.load(conn, statement_id, settings)
        document = await documents_repo.get(conn, state.statement.document_id) if state else None
        categories = await categories_repo.list_active(conn)

    if state is None:
        return _not_found(request, profile, user, settings)

    context = _context(request, state, document, profile, user, settings, categories)
    context["editing_id"] = edit
    return render(request, "review/page.html", context)


@router.get("/{statement_id}/transactions/{transaction_id}/row", include_in_schema=False)
async def transaction_row(
    request: Request,
    user: CurrentUserDep,
    profile: CurrentProfileDep,
    settings: SettingsDep,
    statement_id: str,
    transaction_id: str,
    mode: Literal["read", "edit"] = "read",
    focus: bool = False,
) -> Response:
    """Una sola fila, en lectura o en edicion, para el swap de HTMX.

    Es un GET sin efectos: no lleva CSRF, igual que la pagina. La pertenencia la
    deciden RLS (un resumen ajeno da `None`) y que la transaccion este entre las
    de este resumen: un id de otro resumen, aunque sea propio, es un 404.

    `focus=1` es el retorno de foco al cancelar: el "Editar" de la fila vuelve
    con `autofocus`, que htmx respeta despues del swap.
    """
    async with db.user_tx(user.db_claims) as conn:
        state = await review_service.load(conn, statement_id, settings)
        categories = await categories_repo.list_active(conn)

    tx = next((t for t in state.transactions if t.id == transaction_id), None) if state else None
    if state is None or tx is None:
        return _not_found(request, profile, user, settings, title="No existe esa transacción")

    if not is_htmx(request):
        if mode == "edit":
            target = f"/statements/{statement_id}/review?edit={tx.id}#tx-{tx.id}"
        else:
            target = f"/statements/{statement_id}/review#tx-{tx.id}"
        return RedirectResponse(target, status_code=303)

    context = _context(request, state, None, profile, user, settings, categories)
    context["tx"] = tx
    context["editing_id"] = tx.id if mode == "edit" and not state.statement.is_confirmed else None
    context["focus_id"] = tx.id if focus else None
    return render(request, "review/_row.html", context)


# ─────────────────────────────────────────────────────────────────────────────
# Las mutaciones
# ─────────────────────────────────────────────────────────────────────────────
@router.post("/{statement_id}/transactions/{transaction_id}", include_in_schema=False)
async def edit_transaction(
    request: Request,
    user: CurrentUserDep,
    profile: CurrentProfileDep,
    settings: SettingsDep,
    statement_id: str,
    transaction_id: str,
) -> Response:
    """Edicion inline de una fila."""
    form = await request.form()
    csrf.verify(_csrf_of(form), user.user_id, settings)

    # Si la edicion se rechaza, la fila vuelve abierta junto al mensaje. Muestra
    # los valores de la base, no lo que se tipeo: el mensaje dice que corregir.
    return await _mutate(
        request,
        user,
        profile,
        settings,
        statement_id,
        lambda conn: review_service.edit_transaction(
            conn, transaction_id, _fields(form, review_service.EDITABLE_FIELDS)
        ),
        editing_id_on_error=transaction_id,
    )


@router.post("/{statement_id}/transactions/{transaction_id}/category", include_in_schema=False)
async def set_category(
    request: Request,
    user: CurrentUserDep,
    profile: CurrentProfileDep,
    settings: SettingsDep,
    statement_id: str,
    transaction_id: str,
) -> Response:
    """Categoria a mano. Queda en `transaction_revisions`: es la materia prima
    del few-shot que usa el enriquecimiento de comercios (app/llm/fewshot.py)."""
    form = await request.form()
    csrf.verify(_csrf_of(form), user.user_id, settings)

    category_id = str(form.get("category_id", "")).strip()

    async def _apply(conn: db.AsyncConnection) -> None:
        if not category_id:
            raise enrich_service.EnrichError("elegí una categoría", reason="category_required")
        await enrich_service.recategorize(
            conn, transaction_id=transaction_id, category_id=category_id
        )

    return await _mutate(
        request,
        user,
        profile,
        settings,
        statement_id,
        _apply,
        editing_id_on_error=transaction_id,
    )


@router.post("/{statement_id}/bulk", include_in_schema=False)
async def bulk(
    request: Request,
    user: CurrentUserDep,
    profile: CurrentProfileDep,
    settings: SettingsDep,
    statement_id: str,
) -> Response:
    """Confirmar / rechazar / reabrir varias de una.

    Sin seleccion y con `action=confirm`, confirma todo lo pendiente: es el
    camino normal, en el que se corrigen las pocas que estan mal y se acepta el
    resto en bloque.
    """
    form = await request.form()
    csrf.verify(_csrf_of(form), user.user_id, settings)

    action = str(form.get("action", ""))
    ids = [str(value) for value in form.getlist("transaction_ids")]

    return await _mutate(
        request,
        user,
        profile,
        settings,
        statement_id,
        lambda conn: review_service.bulk_review(
            conn, statement_id, transaction_ids=ids, action=action
        ),
    )


@router.post("/{statement_id}/transactions", include_in_schema=False)
async def add_transaction(
    request: Request,
    user: CurrentUserDep,
    profile: CurrentProfileDep,
    settings: SettingsDep,
    statement_id: str,
) -> Response:
    """Agrega a mano lo que el modelo no vio."""
    form = await request.form()
    csrf.verify(_csrf_of(form), user.user_id, settings)

    fields = _fields(form, {"posted_date", "description_raw", "amount", "direction", "kind"})
    return await _mutate(
        request,
        user,
        profile,
        settings,
        statement_id,
        lambda conn: review_service.add_transaction(conn, statement_id, fields),
    )


@router.post("/{statement_id}/transactions/{transaction_id}/split", include_in_schema=False)
async def split_transaction(
    request: Request,
    user: CurrentUserDep,
    profile: CurrentProfileDep,
    settings: SettingsDep,
    statement_id: str,
    transaction_id: str,
) -> Response:
    """Divide una linea en varias conservando el total."""
    form = await request.form()
    csrf.verify(_csrf_of(form), user.user_id, settings)

    amounts = [str(value) for value in form.getlist("part_amount") if str(value).strip()]
    descriptions = [str(value) for value in form.getlist("part_description")]
    parts: list[dict[str, Any]] = [
        {
            "amount": amount,
            "description_raw": descriptions[index] if index < len(descriptions) else "",
        }
        for index, amount in enumerate(amounts)
    ]

    return await _mutate(
        request,
        user,
        profile,
        settings,
        statement_id,
        lambda conn: review_service.split_transaction(conn, transaction_id, parts),
        editing_id_on_error=transaction_id,
    )


@router.post("/{statement_id}/confirm", include_in_schema=False)
async def confirm(
    request: Request,
    user: CurrentUserDep,
    profile: CurrentProfileDep,
    settings: SettingsDep,
    statement_id: str,
    csrf_token: Annotated[str, Form()] = "",
    accept_delta: Annotated[str, Form()] = "",
) -> Response:
    """La unica puerta al dashboard."""
    csrf.verify(csrf_token, user.user_id, settings)

    accepted = accept_delta.strip().lower() in ("1", "true", "on", "yes")
    return await _mutate(
        request,
        user,
        profile,
        settings,
        statement_id,
        lambda conn: review_service.confirm_statement(
            conn, statement_id, settings, accept_delta=accepted
        ),
    )


@router.post("/{statement_id}/reopen", include_in_schema=False)
async def reopen(
    request: Request,
    user: CurrentUserDep,
    profile: CurrentProfileDep,
    settings: SettingsDep,
    statement_id: str,
    csrf_token: Annotated[str, Form()] = "",
) -> Response:
    """Devuelve un resumen confirmado a revision."""
    csrf.verify(csrf_token, user.user_id, settings)

    return await _mutate(
        request,
        user,
        profile,
        settings,
        statement_id,
        lambda conn: review_service.reopen_statement(conn, statement_id, settings),
    )


# ─────────────────────────────────────────────────────────────────────────────
# Interno
# ─────────────────────────────────────────────────────────────────────────────
async def _mutate(
    request: Request,
    user: CurrentUserDep,
    profile: CurrentProfileDep,
    settings: SettingsDep,
    statement_id: str,
    operation: Any,
    editing_id_on_error: str | None = None,
) -> Response:
    """Ejecuta la operacion y devuelve el panel actualizado.

    La operacion y la relectura del estado van en la **misma** transaccion: si
    se leyera despues, con otra conexion, el banner podria mostrar un total de
    antes del cambio que se acaba de hacer.

    Un `ReviewError` no es un 500: es una respuesta al usuario. Se re-renderiza
    el panel con el mensaje arriba y los datos como quedaron.
    """
    error: str | None = None

    async with db.user_tx(user.db_claims) as conn:
        try:
            await operation(conn)
        except review_service.ReviewError as exc:
            error = exc.message
            log.info("accion de revision rechazada", code=exc.code, detail=exc.message)
        except enrich_service.EnrichError as exc:
            error = exc.message
            log.info("accion de recategorizacion rechazada", reason=exc.reason, detail=exc.message)

        state = await review_service.load(conn, statement_id, settings)
        document = await documents_repo.get(conn, state.statement.document_id) if state else None
        categories = await categories_repo.list_active(conn)

    if state is None:
        return _not_found(request, profile, user, settings)

    context = _context(request, state, document, profile, user, settings, categories)
    context["error"] = error
    context["editing_id"] = editing_id_on_error if error else None

    if is_htmx(request):
        return render(request, "review/_panel.html", context)

    if error:
        # Sin HTMX se re-renderiza la pagina entera para no perder el mensaje en
        # un redirect.
        return render(request, "review/page.html", context, status_code=400)

    return RedirectResponse(f"/statements/{statement_id}/review", status_code=303)


def _context(
    request: Request,
    state: review_service.ReviewState,
    document: documents_repo.Document | None,
    profile: CurrentProfileDep,
    user: CurrentUserDep,
    settings: SettingsDep,
    categories: list[categories_repo.Category],
) -> dict[str, Any]:
    return {
        "profile": profile,
        "state": state,
        "statement": state.statement,
        "document": document,
        "kinds": sorted(review_service.KINDS),
        "categories": categories,
        "csrf_token": csrf.issue(user.user_id, settings),
    }


def _not_found(
    request: Request,
    profile: CurrentProfileDep,
    user: CurrentUserDep,
    settings: SettingsDep,
    title: str = "No existe ese resumen",
) -> Response:
    return render(
        request,
        "error.html",
        {
            "profile": profile,
            "title": title,
            "detail": "O no es tuyo, que para el sistema es lo mismo.",
            "csrf_token": csrf.issue(user.user_id, settings),
        },
        status_code=404,
    )


def _csrf_of(form: FormData) -> str:
    return str(form.get("csrf_token", ""))


def _fields(form: FormData, allowed: frozenset[str] | set[str]) -> dict[str, Any]:
    """Solo los campos permitidos que el formulario realmente mando.

    Que un campo ausente signifique "no lo cambies" (y no "poneme el default") es
    lo que permite mandar una fila entera o un solo campo con el mismo endpoint.
    """
    return {key: form[key] for key in allowed if key in form}
