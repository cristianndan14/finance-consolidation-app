"""Revision y correccion: donde el sistema pasa de funcional a confiable.

# La idea

Las fases anteriores producen transacciones que estan *casi siempre* bien. "Casi
siempre" no alcanza para un dashboard de finanzas: un numero que puede estar mal
y no avisa es peor que no tener el numero, porque se usa igual para decidir.

Esta fase cierra eso con una regla: **un resumen solo pasa a `confirmed` por una
accion explicita tuya, y el dashboard por default agrega unicamente lo
confirmado.** Entre medio, la pantalla muestra la diferencia de cuadre al lado de
las transacciones y la linea literal del PDF al lado de cada dato, para que
verificar sea mirar dos columnas y no abrir el PDF en otra ventana.

# Las tres cosas que este modulo cuida

**El cuadre se recalcula leyendo la base, siempre.** No se guarda un total que
despues quede viejo: despues de cada edicion el banner tiene que decir lo que hay
ahora. Es barato (una agregacion sobre ~200 filas) y elimina una clase entera de
bugs de cache.

**Editar NO cambia la `dedupe_key`**, aunque cambie el monto o la descripcion
sobre los que esa clave se calcula. Es contraintuitivo y es el punto mas
delicado del modulo.

La clave de una fila extraida no es un hash de su contenido actual: es la
identidad de **la linea del modelo de la que salio**. Si al corregir un monto se
recalculara, el proximo reparse no reconoceria la fila — insertaria de nuevo la
version mal leida y dejaria la corregida como huerfana. El usuario veria el mes
inflado con las dos, sin ninguna señal de que algo se duplico.

Manteniendola, el reparse matchea la fila, ve que esta `edited`, no la toca y
registra el conflicto. Las transacciones agregadas a mano son el otro caso: no
tienen linea de origen, asi que su clave si sale del contenido.

**Dividir no puede cambiar el total.** Un split cuyas partes no suman el original
mueve el cuadre sin que el usuario lo haya pedido, y el numero que cambio no
aparece en ningun lado. Se rechaza.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any, Final

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection

from app.domain import dedupe as dedupe_mod
from app.domain.dates import DateParseError, parse_date
from app.domain.models import ParsedStatementHeader
from app.domain.money import AmountParseError, parse_amount, quantize
from app.domain.validation import expected_movement
from app.logging_config import get_logger
from app.repositories import extraction_runs as runs_repo
from app.repositories import statements as statements_repo
from app.repositories import transaction_revisions as revisions_repo
from app.repositories import transactions as transactions_repo
from app.schemas.llm import TransactionKind
from app.settings import Settings

log = get_logger(__name__)

# Los campos que la pantalla deja tocar. Todo lo demas (la moneda, el resumen al
# que pertenece, la linea de origen) no es una correccion sino otra transaccion.
EDITABLE_FIELDS: Final = frozenset(
    {
        "posted_date",
        "description_raw",
        "amount",
        "direction",
        "kind",
        "installment_number",
        "installment_total",
        "notes",
    }
)

KINDS: Final = frozenset(TransactionKind.__args__)  # type: ignore[attr-defined]

REVIEW_ACTIONS: Final = {"confirm": "confirmed", "reject": "rejected", "reset": "pending"}


class ReviewError(Exception):
    """Lo que pidio el usuario no se puede hacer, y el motivo es explicable.

    El mensaje se le muestra tal cual: no es un error interno, es una respuesta.
    """

    def __init__(self, message: str, *, code: str = "invalid") -> None:
        super().__init__(message)
        self.message = message
        self.code = code


# ─────────────────────────────────────────────────────────────────────────────
# El estado que ve la pantalla
# ─────────────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class CurrencyBalance:
    """El cuadre de una moneda, recalculado contra lo que hay en la base ahora."""

    currency: str
    declared: Decimal | None
    # De donde sale `declared`, para que el banner pueda decir contra que compara.
    basis: str
    computed: Decimal
    tolerance: Decimal
    count: int
    pending: int
    rejected: int

    # Las piezas con las que la pantalla reconstruye el resumen tal como lo lee
    # el usuario. El movimiento neto es la magnitud con la que se verifica, pero
    # no es un numero que aparezca impreso en ningun lado del PDF: mostrar solo
    # eso hace que una extraccion correcta parezca equivocada.
    previous_balance: Decimal | None = None
    declared_total_due: Decimal | None = None
    charges: Decimal = Decimal("0")
    credits: Decimal = Decimal("0")

    @property
    def computed_total_due(self) -> Decimal | None:
        """El total a pagar que se deduce de lo extraido.

        Es lo que se compara contra el total impreso en el resumen. Da
        exactamente la misma diferencia que comparar el movimiento neto —los dos
        lados llevan el mismo saldo anterior sumado— pero en los terminos en que
        el usuario puede verificarlo mirando el PDF.
        """
        if self.previous_balance is None:
            return None
        return quantize(self.previous_balance + self.computed)

    @property
    def delta(self) -> Decimal | None:
        if self.declared is None:
            return None
        return quantize(self.declared - self.computed)

    @property
    def balanced(self) -> bool:
        delta = self.delta
        return delta is not None and abs(delta) <= self.tolerance


@dataclass(frozen=True)
class ReviewState:
    """Todo lo que la pantalla necesita para decidir que mostrar y que bloquear."""

    statement: statements_repo.Statement
    transactions: list[transactions_repo.Transaction]
    balances: list[CurrencyBalance]

    # Filas que la ultima re-corrida no produjo pero que sobrevivieron porque vos
    # las habias tocado. Sin marcarlas, una transaccion que el modelo dejo de ver
    # se lee en la tabla como un duplicado inexplicable.
    orphan_ids: frozenset[str] = frozenset()
    # Filas que el modelo propuso cambiar y no se tocaron por el mismo motivo.
    conflict_ids: frozenset[str] = frozenset()

    @property
    def orphans(self) -> list[transactions_repo.Transaction]:
        return [tx for tx in self.transactions if tx.id in self.orphan_ids]

    @property
    def pending(self) -> int:
        return sum(b.pending for b in self.balances)

    @property
    def balanced(self) -> bool:
        return bool(self.balances) and all(b.balanced for b in self.balances)

    @property
    def can_confirm(self) -> bool:
        """Confirmar exige haber mirado todo. El cuadre se puede aceptar aparte."""
        return self.pending == 0 and not self.statement.is_confirmed

    @property
    def blocking_reason(self) -> str | None:
        if self.statement.is_confirmed:
            return "Este resumen ya está confirmado."
        if self.pending:
            return (
                f"Quedan {self.pending} transacciones sin revisar. "
                "Confirmá o rechazá cada una (o usá las acciones de arriba)."
            )
        return None


async def load(conn: AsyncConnection, statement_id: str, settings: Settings) -> ReviewState | None:
    """Arma el estado completo de la pantalla. `None` si el resumen no es tuyo."""
    statement = await statements_repo.get(conn, statement_id)
    if statement is None:
        return None

    rows = await transactions_repo.list_for_statement(conn, statement_id)
    totals = await transactions_repo.totals(conn, statement_id)

    # La moneda del statement siempre aparece, aunque todavia no haya ni una
    # transaccion: sin eso, un resumen del que el modelo no extrajo nada se veria
    # cuadrado en lugar de vacio.
    by_currency = {total.currency: total for total in totals}
    currencies = sorted({statement.currency, *by_currency})

    balances = [
        _balance(statement, by_currency.get(currency), currency, settings)
        for currency in currencies
    ]

    validation = await runs_repo.latest_validation(conn, statement.document_id)
    return ReviewState(
        statement=statement,
        transactions=rows,
        balances=balances,
        orphan_ids=_ids(validation.get("orphans")),
        conflict_ids=_ids(validation.get("conflicts")),
    )


def _ids(entries: Any) -> frozenset[str]:
    if not isinstance(entries, list):
        return frozenset()
    return frozenset(
        str(entry["transaction_id"])
        for entry in entries
        if isinstance(entry, dict) and entry.get("transaction_id")
    )


def _balance(
    statement: statements_repo.Statement,
    total: transactions_repo.StatementTotals | None,
    currency: str,
    settings: Settings,
) -> CurrencyBalance:
    # El statement es por (documento, moneda), asi que una moneda ajena no tiene
    # totales declarados contra los que comparar.
    #
    # La referencia sale de `expected_movement`, la misma funcion que usa la
    # validacion de la etapa 2: si la pantalla usara otra regla, el banner diria
    # una cosa y el reporte guardado otra sobre el mismo resumen.
    if currency == statement.currency:
        declared, basis = expected_movement(
            ParsedStatementHeader(
                currency=statement.currency,
                previous_balance=statement.previous_balance,
                payments_credits=statement.payments_credits,
                new_charges=statement.new_charges,
                total_due=statement.total_due,
            )
        )
    else:
        declared, basis = None, "el resumen no declara totales para esta moneda"

    computed = total.total if total else Decimal("0")
    reference = declared if declared is not None else computed
    propio = currency == statement.currency

    return CurrencyBalance(
        currency=currency,
        declared=declared,
        basis=basis,
        computed=quantize(computed),
        tolerance=max(
            abs(reference) * settings.reconciliation_tolerance_pct,
            settings.reconciliation_floor(currency),
        ),
        count=total.count if total else 0,
        pending=total.pending if total else 0,
        rejected=total.rejected if total else 0,
        previous_balance=statement.previous_balance if propio else None,
        declared_total_due=statement.total_due if propio else None,
        charges=total.charges if total else Decimal("0"),
        credits=total.credits if total else Decimal("0"),
    )


# ─────────────────────────────────────────────────────────────────────────────
# Editar una transaccion
# ─────────────────────────────────────────────────────────────────────────────
async def _reload(conn: AsyncConnection, transaction_id: str) -> transactions_repo.Transaction:
    """Relee una fila que se acaba de escribir en esta misma transaccion.

    No deberia poder faltar; si falta, es un bug y tiene que decirlo en vez de
    devolver `None` y romper tres llamadas mas arriba. Un `assert` no sirve
    porque desaparece con `python -O`.
    """
    row = await transactions_repo.get(conn, transaction_id)
    if row is None:
        raise ReviewError("la transaccion desaparecio al releerla", code="internal")
    return row


async def edit_transaction(
    conn: AsyncConnection, transaction_id: str, raw: dict[str, Any]
) -> transactions_repo.Transaction:
    """Aplica una correccion manual y la registra en la bitacora.

    `raw` viene de un formulario, asi que todo llega como texto y todo se valida.
    Los campos que no aparecen quedan como estaban: la pantalla manda una fila
    entera, pero un cliente podria mandar solo el campo que cambio.
    """
    current = await transactions_repo.get(conn, transaction_id)
    if current is None:
        raise ReviewError("esa transacción no existe", code="not_found")

    statement = await statements_repo.get(conn, current.statement_id)
    if statement is not None and statement.is_confirmed:
        raise ReviewError(
            "el resumen está confirmado: reabrilo antes de editarlo", code="confirmed"
        )

    closing = statement.closing_date if statement else None
    values = _coerce(raw, current=current, closing=closing)
    changes = _diff(current, values)

    if not changes:
        return current

    # La `dedupe_key` NO se recalcula, y esto es lo contrario de lo que parece
    # correcto. Ver el comentario de arriba del modulo: la clave de una fila
    # extraida identifica **de que linea del modelo salio**, no que contiene
    # ahora. Recalcularla al corregir el monto haria que el proximo reparse no
    # reconozca la fila: insertaria de nuevo la version mal leida y dejaria la
    # corregida como huerfana. El mes quedaria inflado, con las dos.
    #
    # Manteniendola, el reparse matchea la fila, ve que esta `edited`, no la
    # toca y anota el conflicto. Que es exactamente lo que tiene que pasar.
    await transactions_repo.apply_edit(
        conn,
        transaction_id,
        posted_date=values["posted_date"],
        description_raw=values["description_raw"],
        amount=values["amount"],
        direction=values["direction"],
        kind=values["kind"],
        installment_number=values["installment_number"],
        installment_total=values["installment_total"],
        notes=values["notes"],
        dedupe_key=current.dedupe_key,
        occurrence_index=current.occurrence_index,
    )
    await revisions_repo.record(conn, transaction_id=transaction_id, changes=changes)

    updated = await _reload(conn, transaction_id)

    log.info(
        "transaccion editada",
        transaction_id=transaction_id,
        fields=[change.field for change in changes],
    )
    return updated


def _coerce(
    raw: dict[str, Any], *, current: transactions_repo.Transaction, closing: date | None
) -> dict[str, Any]:
    """Texto del formulario -> valores tipados y validados."""
    values: dict[str, Any] = {
        "posted_date": current.posted_date,
        "description_raw": current.description_raw,
        "amount": current.amount,
        "direction": current.direction,
        "kind": current.kind,
        "installment_number": current.installment_number,
        "installment_total": current.installment_total,
        "notes": current.notes,
    }

    if "posted_date" in raw:
        values["posted_date"] = _parse_date(raw["posted_date"], closing=closing)

    if "description_raw" in raw:
        description = str(raw["description_raw"]).strip()
        if not description:
            raise ReviewError("la descripción no puede quedar vacía")
        values["description_raw"] = description

    if "amount" in raw:
        values["amount"] = _parse_amount(raw["amount"])

    if "direction" in raw:
        values["direction"] = _parse_direction(raw["direction"])

    if "kind" in raw:
        kind = str(raw["kind"]).strip()
        if kind not in KINDS:
            raise ReviewError(f"tipo de transacción desconocido: {kind!r}")
        values["kind"] = kind

    if "installment_number" in raw or "installment_total" in raw:
        number = _parse_optional_int(raw.get("installment_number", current.installment_number))
        total = _parse_optional_int(raw.get("installment_total", current.installment_total))
        values["installment_number"], values["installment_total"] = _check_installments(
            number, total
        )

    if "notes" in raw:
        notes = str(raw["notes"]).strip()
        values["notes"] = notes or None

    return values


def _diff(
    current: transactions_repo.Transaction, values: dict[str, Any]
) -> list[revisions_repo.FieldChange]:
    """Que cambio de verdad. Un campo reenviado igual no es una revision."""
    changes: list[revisions_repo.FieldChange] = []
    for field in sorted(EDITABLE_FIELDS):
        old = getattr(current, field)
        new = values[field]
        if old == new:
            continue
        changes.append(revisions_repo.FieldChange(field=field, old=old, new=new))
    return changes


# ─────────────────────────────────────────────────────────────────────────────
# Acciones masivas
# ─────────────────────────────────────────────────────────────────────────────
async def bulk_review(
    conn: AsyncConnection, statement_id: str, *, transaction_ids: Sequence[str], action: str
) -> int:
    """Confirma, rechaza o reabre varias transacciones de una.

    Revisar 200 lineas de a una es lo que hace que nadie revise. La accion masiva
    es lo que vuelve realista el requisito de que nada se confirme sin mirarse:
    se corrigen las pocas que estan mal y se confirma el resto en bloque.
    """
    status = REVIEW_ACTIONS.get(action)
    if status is None:
        raise ReviewError(f"acción de revisión desconocida: {action!r}")

    statement = await statements_repo.get(conn, statement_id)
    if statement is None:
        raise ReviewError("ese resumen no existe", code="not_found")
    if statement.is_confirmed:
        raise ReviewError("el resumen está confirmado: reabrilo primero", code="confirmed")

    if not transaction_ids:
        # "Confirmar todo lo pendiente" sin seleccion explicita.
        if status != "confirmed":
            return 0
        return await transactions_repo.confirm_all_pending(conn, statement_id)

    # Solo se tocan filas de este resumen: un id de otro no hace nada en vez de
    # editar algo que la pantalla no estaba mostrando. (RLS ya impide que sea de
    # otro usuario; esto impide que sea de otro resumen del mismo usuario.)
    own = {row.id for row in await transactions_repo.list_for_statement(conn, statement_id)}
    targets = [tx_id for tx_id in transaction_ids if tx_id in own]

    return await transactions_repo.set_review_status(conn, targets, status)


# ─────────────────────────────────────────────────────────────────────────────
# Agregar a mano lo que el modelo no vio
# ─────────────────────────────────────────────────────────────────────────────
async def add_transaction(
    conn: AsyncConnection, statement_id: str, raw: dict[str, Any]
) -> transactions_repo.Transaction:
    """La salida cuando falta una transaccion y el reparse no la encuentra.

    Es lo que convierte un resumen que no cuadra en uno que cuadra sin tener que
    mentirle al sistema: la fila entra con `source_line` nula, asi que queda
    visible que no salio del PDF.
    """
    statement = await statements_repo.get(conn, statement_id)
    if statement is None:
        raise ReviewError("ese resumen no existe", code="not_found")
    if statement.is_confirmed:
        raise ReviewError("el resumen está confirmado: reabrilo primero", code="confirmed")

    description = str(raw.get("description_raw", "")).strip()
    if not description:
        raise ReviewError("la descripción no puede quedar vacía")

    posted = _parse_date(raw.get("posted_date", ""), closing=statement.closing_date)
    amount = _parse_amount(raw.get("amount", ""))
    direction = _parse_direction(raw.get("direction", 1))
    kind = str(raw.get("kind", "charge")).strip() or "charge"
    if kind not in KINDS:
        raise ReviewError(f"tipo de transacción desconocido: {kind!r}")

    currency = str(raw.get("currency", statement.currency)).strip().upper() or statement.currency

    key = dedupe_mod.dedupe_key(
        description=description,
        posted_date=posted,
        amount=amount,
        currency=currency,
        installment_number=None,
        installment_total=None,
    )
    occurrence = await transactions_repo.next_occurrence(
        conn, statement_id=statement_id, dedupe_key=key
    )

    new_id = await transactions_repo.insert_manual(
        conn,
        statement_id=statement_id,
        card_id=statement.card_id,
        posted_date=posted,
        description_raw=description,
        amount=amount,
        currency=currency,
        direction=direction,
        kind=kind,
        dedupe_key=key,
        occurrence_index=occurrence,
        notes=str(raw.get("notes", "")).strip() or None,
    )

    created = await _reload(conn, new_id)

    log.info("transaccion agregada a mano", statement_id=statement_id, transaction_id=new_id)
    return created


# ─────────────────────────────────────────────────────────────────────────────
# Dividir una transaccion
# ─────────────────────────────────────────────────────────────────────────────
async def split_transaction(
    conn: AsyncConnection, transaction_id: str, parts: Sequence[dict[str, Any]]
) -> list[transactions_repo.Transaction]:
    """Parte una linea en varias, conservando el total.

    Es para el caso real de una compra que agrupa cosas de categorias distintas
    (el super donde tambien cargaste nafta). La original no se borra: pasa a
    `rejected`, que la saca del total y la deja visible con su `source_line`
    original, de modo que se puede ver de donde salieron las partes.

    **Las partes tienen que sumar exactamente el original.** Un split que cambia
    el total mueve el cuadre sin que nadie lo haya pedido y sin dejar rastro de
    cual fue el numero que cambio.
    """
    current = await transactions_repo.get(conn, transaction_id)
    if current is None:
        raise ReviewError("esa transacción no existe", code="not_found")

    statement = await statements_repo.get(conn, current.statement_id)
    if statement is not None and statement.is_confirmed:
        raise ReviewError("el resumen está confirmado: reabrilo primero", code="confirmed")

    if len(parts) < 2:
        raise ReviewError("dividir requiere al menos dos partes")

    amounts = [_parse_amount(part.get("amount", "")) for part in parts]
    descriptions = [
        str(part.get("description_raw", "")).strip() or current.description_raw for part in parts
    ]

    total = quantize(sum(amounts, Decimal("0")))
    if total != current.amount:
        raise ReviewError(
            f"las partes suman {total} y la transacción original es {current.amount}: "
            "dividir no puede cambiar el total"
        )

    created: list[transactions_repo.Transaction] = []
    for amount, description in zip(amounts, descriptions, strict=True):
        key = dedupe_mod.dedupe_key(
            description=description,
            posted_date=current.posted_date,
            amount=amount,
            currency=current.currency,
            installment_number=None,
            installment_total=None,
        )
        occurrence = await transactions_repo.next_occurrence(
            conn, statement_id=current.statement_id, dedupe_key=key
        )
        new_id = await transactions_repo.insert_manual(
            conn,
            statement_id=current.statement_id,
            card_id=current.card_id,
            posted_date=current.posted_date,
            description_raw=description,
            amount=amount,
            currency=current.currency,
            direction=current.direction,
            kind=current.kind,
            dedupe_key=key,
            occurrence_index=occurrence,
            notes=f"parte de: {current.description_raw[:60]}",
        )
        created.append(await _reload(conn, new_id))

    await transactions_repo.set_review_status(conn, [transaction_id], "rejected")
    await revisions_repo.record(
        conn,
        transaction_id=transaction_id,
        changes=[
            revisions_repo.FieldChange(
                field="split",
                old=str(current.amount),
                new=[str(amount) for amount in amounts],
            )
        ],
    )

    log.info("transaccion dividida", transaction_id=transaction_id, parts=len(created))
    return created


# ─────────────────────────────────────────────────────────────────────────────
# Confirmar el resumen
# ─────────────────────────────────────────────────────────────────────────────
async def confirm_statement(
    conn: AsyncConnection, statement_id: str, settings: Settings, *, accept_delta: bool = False
) -> statements_repo.Statement:
    """Marca el resumen como confirmado. Es la unica puerta al dashboard.

    Se exigen dos cosas, y la segunda se puede levantar a mano:

    1. **Ninguna transaccion pendiente.** Confirmar un resumen es afirmar que lo
       miraste; si quedan lineas sin revisar, la afirmacion es falsa.
    2. **Que cuadre**, salvo que se acepte la diferencia explicitamente. Aceptarla
       la deja registrada en `accepted_delta`, que es distinto de que no exista:
       dentro de tres meses se puede saber que ese mes cerro con 340 pesos sin
       explicar y que fue una decision, no un error.
    """
    state = await load(conn, statement_id, settings)
    if state is None:
        raise ReviewError("ese resumen no existe", code="not_found")
    if state.statement.is_confirmed:
        return state.statement

    if state.pending:
        raise ReviewError(
            f"quedan {state.pending} transacciones sin revisar", code="pending_transactions"
        )

    unbalanced = [b for b in state.balances if not b.balanced]
    if unbalanced and not accept_delta:
        detail = ", ".join(
            f"{b.currency}: {b.delta if b.delta is not None else 'sin total declarado'}"
            for b in unbalanced
        )
        raise ReviewError(
            f"el resumen no cuadra ({detail}). Corregí las transacciones o aceptá "
            "la diferencia explícitamente.",
            code="unbalanced",
        )

    accepted = next((b.delta for b in unbalanced if b.currency == state.statement.currency), None)

    try:
        await statements_repo.set_status(conn, statement_id, "confirmed", accepted_delta=accepted)
    except IntegrityError as exc:
        # `statements_period_uq` es parcial sobre 'confirmed': impide confirmar
        # dos resumenes de la misma tarjeta, moneda y mes, que en el dashboard
        # seria doble conteo.
        raise ReviewError(
            "ya hay otro resumen confirmado para esa tarjeta en el mismo mes y moneda",
            code="duplicate_period",
        ) from exc

    confirmed = await statements_repo.get(conn, statement_id)
    if confirmed is None:
        raise ReviewError("el resumen desaparecio al releerlo", code="internal")

    log.info(
        "resumen confirmado",
        statement_id=statement_id,
        accepted_delta=str(accepted) if accepted is not None else None,
    )
    return confirmed


async def reopen_statement(
    conn: AsyncConnection, statement_id: str, settings: Settings
) -> statements_repo.Statement:
    """Devuelve un resumen confirmado a revision.

    Existe porque confirmar es facil de hacer de mas. El estado vuelve segun
    cuadre o no; las transacciones **no** se tocan: lo que confirmaste sigue
    confirmado, que es justamente lo que hace que reabrir sea barato.
    """
    state = await load(conn, statement_id, settings)
    if state is None:
        raise ReviewError("ese resumen no existe", code="not_found")

    await statements_repo.set_status(
        conn, statement_id, "draft" if state.balanced else "needs_review"
    )
    reopened = await statements_repo.get(conn, statement_id)
    if reopened is None:
        raise ReviewError("el resumen desaparecio al releerlo", code="internal")

    log.info("resumen reabierto", statement_id=statement_id, status=reopened.status)
    return reopened


# ─────────────────────────────────────────────────────────────────────────────
# Parseo de lo que llega del formulario
# ─────────────────────────────────────────────────────────────────────────────
def _parse_date(raw: Any, *, closing: date | None) -> date:
    if isinstance(raw, date):
        return raw
    try:
        return parse_date(str(raw), closing=closing)
    except DateParseError as exc:
        raise ReviewError(f"fecha inválida: {exc}") from exc


def _parse_amount(raw: Any) -> Decimal:
    try:
        value = parse_amount(raw)
    except AmountParseError as exc:
        raise ReviewError(f"monto inválido: {exc}") from exc

    if value < 0:
        # El signo va en `direction`, y la base lo exige con un check. Un monto
        # negativo escrito a mano casi siempre quiere decir "esto es un pago".
        raise ReviewError(
            "el monto va siempre positivo; para un pago o una devolución "
            "cambiá el sentido a 'crédito'"
        )
    return quantize(value)


def _parse_direction(raw: Any) -> int:
    try:
        value = int(str(raw).strip())
    except ValueError as exc:
        raise ReviewError(f"sentido inválido: {raw!r}") from exc
    if value not in (1, -1):
        raise ReviewError(f"sentido inválido: {raw!r}")
    return value


def _parse_optional_int(raw: Any) -> int | None:
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    try:
        return int(text)
    except ValueError as exc:
        raise ReviewError(f"número de cuota inválido: {raw!r}") from exc


def _check_installments(number: int | None, total: int | None) -> tuple[int | None, int | None]:
    """Una cuota tiene numero y total, o ninguno de los dos. Lo exige la base."""
    if number is None and total is None:
        return None, None
    if number is None or total is None:
        raise ReviewError("una cuota necesita el número y el total (por ejemplo, 3 de 12)")
    if not 1 <= number <= total <= 120:
        raise ReviewError(f"cuota {number} de {total} no es coherente")
    return number, total
