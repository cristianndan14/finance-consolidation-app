"""Lectura y escritura de `app.transactions`, incluida la reconciliacion.

# El problema que resuelve `reconcile`

Re-correr la etapa 2 es barato y deseable: se mejora un prompt y se reprocesa el
corpus. Pero entre la corrida vieja y la nueva, el usuario pudo haber corregido a
mano el monto de una transaccion, confirmado otra y rechazado una tercera.

Un `delete + insert` seria trivial de escribir y borraria todo ese trabajo sin
avisar. Un `insert ... on conflict do nothing` haria lo contrario: nunca
mejoraria un dato que el modelo ahora lee bien.

La regla, entonces, es por estado de revision de cada fila:

    no existe                  -> INSERT como 'pending'
    existe y 'pending'         -> UPDATE completo (el modelo manda)
    existe y 'confirmed'       -> NO se toca; va a conflicts[]
    existe y 'edited'          -> NO se toca; va a conflicts[]
    existe y 'rejected'        -> NO se reinserta (el usuario ya dijo que no)

    sobra en la base y es 'pending' -> DELETE
    sobra en la base y no es 'pending' -> se conserva y va a orphans[]

En una palabra: **el modelo puede pisar lo que solo escribio el modelo**. Lo que
tocó una persona sobrevive, y la diferencia queda anotada en el reporte para que
la pantalla de revision la muestre en lugar de esconderla.

Todo pasa en la transaccion que abre el servicio, asi que o se aplica entero o no
se aplica nada.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.domain.dedupe import assign_occurrence_indexes
from app.domain.models import ParsedTransaction

# Estados que el usuario "posee": una re-corrida no los pisa.
USER_OWNED_STATUSES = frozenset({"confirmed", "edited", "rejected"})

# Las consultas se escriben completas en lugar de componerse con f-strings: el
# lint de bandit no puede distinguir una interpolacion constante de una con datos
# del usuario, y tener SQL armado asi en el repo invita a que la proxima si lleve
# datos.
_SELECT = """
select
    id::text              as id,
    statement_id::text    as statement_id,
    card_id::text         as card_id,
    posted_date,
    transaction_date,
    description_raw,
    source_line,
    source_page,
    amount,
    currency,
    direction,
    kind,
    installment_number,
    installment_total,
    purchase_total_amount,
    purchase_date,
    installment_group_key,
    merchant_id::text     as merchant_id,
    category_id::text     as category_id,
    dedupe_key,
    occurrence_index,
    confidence,
    review_status,
    notes,
    created_at,
    updated_at
  from app.transactions
"""

_EXISTING = text(
    """
    select id::text as id, dedupe_key, occurrence_index, review_status,
           amount, currency, description_raw, posted_date
      from app.transactions
     where statement_id = :statement_id
     order by posted_date, created_at
    """
)

_LIST = text(_SELECT + " where statement_id = :statement_id order by posted_date, created_at")

_INSERT = text(
    """
    insert into app.transactions (
        statement_id, card_id, extraction_run_id,
        posted_date, transaction_date, description_raw, source_line, source_page,
        amount, currency, direction, kind,
        installment_number, installment_total, purchase_total_amount, purchase_date,
        installment_group_key, dedupe_key, occurrence_index, confidence
    )
    values (
        :statement_id, :card_id, :extraction_run_id,
        :posted_date, :transaction_date, :description_raw, :source_line, :source_page,
        :amount, :currency, :direction, :kind,
        :installment_number, :installment_total, :purchase_total_amount, :purchase_date,
        :installment_group_key, :dedupe_key, :occurrence_index, :confidence
    )
    returning id::text as id
    """
)

_UPDATE = text(
    """
    update app.transactions
       set extraction_run_id     = :extraction_run_id,
           posted_date           = :posted_date,
           transaction_date      = :transaction_date,
           description_raw       = :description_raw,
           source_line           = :source_line,
           source_page           = :source_page,
           amount                = :amount,
           currency              = :currency,
           direction             = :direction,
           kind                  = :kind,
           installment_number    = :installment_number,
           installment_total     = :installment_total,
           purchase_total_amount = :purchase_total_amount,
           purchase_date         = :purchase_date,
           installment_group_key = :installment_group_key,
           confidence            = :confidence
     where id = :id
    """
)

_DELETE = text("delete from app.transactions where id = any(cast(:ids as uuid[]))")

_COUNT_PENDING = text(
    """
    select count(*) as pending
      from app.transactions
     where statement_id = :statement_id and review_status = 'pending'
    """
)


@dataclass(frozen=True)
class Transaction:
    """Una fila tal como esta en la base. La usa la pantalla de revision."""

    id: str
    statement_id: str
    card_id: str | None
    posted_date: date
    transaction_date: date | None
    description_raw: str
    source_line: str | None
    source_page: int | None
    amount: Decimal
    currency: str
    direction: int
    kind: str
    installment_number: int | None
    installment_total: int | None
    purchase_total_amount: Decimal | None
    purchase_date: date | None
    installment_group_key: str | None
    merchant_id: str | None
    category_id: str | None
    dedupe_key: str
    occurrence_index: int
    confidence: Decimal | None
    review_status: str
    notes: str | None
    created_at: datetime
    updated_at: datetime

    @property
    def signed_amount(self) -> Decimal:
        return self.amount * self.direction


@dataclass
class ReconcileResult:
    """Que hizo la reconciliacion. Va derecho al `ValidationReport`."""

    inserted: int = 0
    updated: int = 0
    deleted: int = 0
    conflicts: list[dict[str, Any]] = field(default_factory=list)
    orphans: list[dict[str, Any]] = field(default_factory=list)

    @property
    def touched(self) -> int:
        return self.inserted + self.updated + self.deleted


async def list_for_statement(conn: AsyncConnection, statement_id: str) -> list[Transaction]:
    rows = (await conn.execute(_LIST, {"statement_id": statement_id})).mappings()
    return [Transaction(**row) for row in rows]


async def count_pending(conn: AsyncConnection, statement_id: str) -> int:
    row = (await conn.execute(_COUNT_PENDING, {"statement_id": statement_id})).mappings().one()
    return int(row["pending"])


async def reconcile(
    conn: AsyncConnection,
    *,
    statement_id: str,
    extraction_run_id: str,
    card_id: str | None,
    incoming: Sequence[ParsedTransaction],
) -> ReconcileResult:
    """Aplica el lote nuevo sin pisar lo que tocó el usuario."""
    result = ReconcileResult()

    existing_rows = (await conn.execute(_EXISTING, {"statement_id": statement_id})).mappings()
    existing = {
        (row["dedupe_key"], int(row["occurrence_index"])): dict(row) for row in existing_rows
    }

    seen: set[tuple[str, int]] = set()

    for tx, occurrence in assign_occurrence_indexes(list(incoming)):
        key = (tx.dedupe_key, occurrence)
        seen.add(key)
        current = existing.get(key)

        if current is None:
            await conn.execute(
                _INSERT,
                _params(
                    tx,
                    statement_id=statement_id,
                    card_id=card_id,
                    extraction_run_id=extraction_run_id,
                    occurrence=occurrence,
                ),
            )
            result.inserted += 1
            continue

        if current["review_status"] in USER_OWNED_STATUSES:
            result.conflicts.append(_conflict(current, tx))
            continue

        await conn.execute(
            _UPDATE,
            _params(
                tx,
                statement_id=statement_id,
                card_id=card_id,
                extraction_run_id=extraction_run_id,
                occurrence=occurrence,
                row_id=str(current["id"]),
            ),
        )
        result.updated += 1

    # Lo que estaba y el run nuevo no produjo.
    stale_ids: list[str] = []
    for key, row in existing.items():
        if key in seen:
            continue
        if row["review_status"] == "pending":
            stale_ids.append(str(row["id"]))
        else:
            result.orphans.append(_orphan(row))

    if stale_ids:
        await conn.execute(_DELETE, {"ids": stale_ids})
        result.deleted = len(stale_ids)

    return result


# ─── Helpers ────────────────────────────────────────────────────────────────
def _params(
    tx: ParsedTransaction,
    *,
    statement_id: str,
    card_id: str | None,
    extraction_run_id: str,
    occurrence: int,
    row_id: str | None = None,
) -> dict[str, Any]:
    params: dict[str, Any] = {
        "statement_id": statement_id,
        "card_id": card_id,
        "extraction_run_id": extraction_run_id,
        "posted_date": tx.posted_date,
        "transaction_date": tx.transaction_date,
        "description_raw": tx.description_raw,
        "source_line": tx.source_line,
        "source_page": tx.source_page,
        "amount": tx.amount,
        "currency": tx.currency,
        "direction": tx.direction,
        "kind": tx.kind,
        "installment_number": tx.installment_number,
        "installment_total": tx.installment_total,
        "purchase_total_amount": tx.purchase_total_amount,
        "purchase_date": tx.purchase_date,
        "installment_group_key": tx.installment_group_key,
        "dedupe_key": tx.dedupe_key,
        "occurrence_index": occurrence,
        "confidence": tx.confidence,
    }
    if row_id is not None:
        params["id"] = row_id
    return params


def _conflict(current: dict[str, Any], incoming: ParsedTransaction) -> dict[str, Any]:
    """Lo que el modelo hubiera escrito y no escribio, y por que."""
    return {
        "transaction_id": str(current["id"]),
        "review_status": current["review_status"],
        "description": str(current["description_raw"])[:80],
        "kept_amount": str(current["amount"]),
        "proposed_amount": str(incoming.amount),
        "changed": str(current["amount"]) != str(incoming.amount),
    }


def _orphan(row: dict[str, Any]) -> dict[str, Any]:
    """Una fila que sobrevivio a la re-corrida porque el usuario la habia tocado."""
    return {
        "transaction_id": str(row["id"]),
        "review_status": row["review_status"],
        "description": str(row["description_raw"])[:80],
        "amount": str(row["amount"]),
        "posted_date": row["posted_date"].isoformat() if row["posted_date"] else None,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Revision manual (Fase 4)
#
# Editar una transaccion no es solo un UPDATE: la `dedupe_key` se calcula sobre
# la descripcion, la fecha, el monto y las cuotas, asi que corregir cualquiera de
# esos campos **cambia la identidad de la fila**. Si no se recalcula, el proximo
# reparse no la reconoce: la ve como huerfana y ademas inserta una nueva. El
# usuario termina con la transaccion duplicada, una corregida y otra no.
#
# Por eso el servicio recalcula la clave y pide el proximo `occurrence_index`
# libre. Sin eso, corregir una transaccion para que quede igual a otra del mismo
# resumen violaria el unique.
# ─────────────────────────────────────────────────────────────────────────────

_GET = text(_SELECT + " where id = :id")

_NEXT_OCCURRENCE = text(
    """
    select coalesce(max(occurrence_index) + 1, 0) as next
      from app.transactions
     where statement_id = :statement_id and dedupe_key = :dedupe_key
    """
)

_APPLY_EDIT = text(
    """
    update app.transactions
       set posted_date        = :posted_date,
           description_raw    = :description_raw,
           amount             = :amount,
           direction          = :direction,
           kind               = :kind,
           installment_number = :installment_number,
           installment_total  = :installment_total,
           notes              = :notes,
           dedupe_key         = :dedupe_key,
           occurrence_index   = :occurrence_index,
           review_status      = 'edited',
           -- Un dato corregido a mano ya no tiene la confianza del modelo:
           -- tiene la del usuario, que es total.
           confidence         = 1.00
     where id = :id
    """
)

_SET_REVIEW_STATUS = text(
    """
    update app.transactions
       set review_status = :status
     where id = any(cast(:ids as uuid[]))
       and review_status <> :status
    returning id::text as id
    """
)

_CONFIRM_PENDING = text(
    """
    update app.transactions
       set review_status = 'confirmed'
     where statement_id = :statement_id and review_status = 'pending'
    returning id::text as id
    """
)

_INSERT_MANUAL = text(
    """
    insert into app.transactions (
        statement_id, card_id, posted_date, description_raw, source_line,
        amount, currency, direction, kind,
        installment_number, installment_total,
        dedupe_key, occurrence_index, confidence, review_status, notes
    )
    values (
        :statement_id, :card_id, :posted_date, :description_raw, :source_line,
        :amount, :currency, :direction, :kind,
        :installment_number, :installment_total,
        :dedupe_key, :occurrence_index, 1.00, 'edited', :notes
    )
    returning id::text as id
    """
)

_TOTALS = text(
    """
    select currency,
           coalesce(
             sum(amount * direction) filter (where review_status <> 'rejected'), 0
           ) as total,
           coalesce(
             sum(amount) filter (where direction = 1 and review_status <> 'rejected'), 0
           ) as charges,
           coalesce(
             sum(amount) filter (where direction = -1 and review_status <> 'rejected'), 0
           ) as credits,
           count(*) as total_count,
           count(*) filter (where review_status = 'pending')  as pending,
           count(*) filter (where review_status = 'rejected') as rejected
      from app.transactions
     where statement_id = :statement_id
     group by currency
    """
)


@dataclass(frozen=True)
class StatementTotals:
    """Lo que suman las transacciones vivas de un resumen, leido de la base."""

    currency: str
    total: Decimal
    # Cargos y creditos por separado. El total neto sirve para verificar; la
    # pantalla necesita las dos patas para poder mostrar la escalera del resumen
    # (saldo anterior, pagos, cargos, total a pagar), que es como el usuario lee
    # su resumen y como puede compararlo contra el PDF.
    charges: Decimal
    credits: Decimal
    count: int
    pending: int
    rejected: int


async def get(conn: AsyncConnection, transaction_id: str) -> Transaction | None:
    row = (await conn.execute(_GET, {"id": transaction_id})).mappings().one_or_none()
    return Transaction(**row) if row else None


async def totals(conn: AsyncConnection, statement_id: str) -> list[StatementTotals]:
    """Totales por moneda para el banner de cuadre de la pantalla de revision.

    Se recalculan leyendo la base y no se guardan: despues de cada edicion el
    numero tiene que reflejar lo que hay ahora, no lo que habia cuando corrio el
    modelo.

    Lo rechazado se cuenta aparte pero **no** suma: el usuario dijo que esa linea
    no es una transaccion, y dejarla en el total haria que el cuadre no cierre
    nunca.
    """
    rows = (await conn.execute(_TOTALS, {"statement_id": statement_id})).mappings()
    return [
        StatementTotals(
            currency=row["currency"],
            total=Decimal(str(row["total"])),
            charges=Decimal(str(row["charges"])),
            credits=Decimal(str(row["credits"])),
            count=int(row["total_count"]),
            pending=int(row["pending"]),
            rejected=int(row["rejected"]),
        )
        for row in rows
    ]


async def next_occurrence(conn: AsyncConnection, *, statement_id: str, dedupe_key: str) -> int:
    row = (
        (
            await conn.execute(
                _NEXT_OCCURRENCE, {"statement_id": statement_id, "dedupe_key": dedupe_key}
            )
        )
        .mappings()
        .one()
    )
    return int(row["next"])


async def apply_edit(
    conn: AsyncConnection,
    transaction_id: str,
    *,
    posted_date: date,
    description_raw: str,
    amount: Decimal,
    direction: int,
    kind: str,
    installment_number: int | None,
    installment_total: int | None,
    notes: str | None,
    dedupe_key: str,
    occurrence_index: int,
) -> None:
    await conn.execute(
        _APPLY_EDIT,
        {
            "id": transaction_id,
            "posted_date": posted_date,
            "description_raw": description_raw,
            "amount": amount,
            "direction": direction,
            "kind": kind,
            "installment_number": installment_number,
            "installment_total": installment_total,
            "notes": notes,
            "dedupe_key": dedupe_key,
            "occurrence_index": occurrence_index,
        },
    )


async def set_review_status(
    conn: AsyncConnection, transaction_ids: Sequence[str], status: str
) -> int:
    """Cambia el estado de revision de varias filas. Devuelve cuantas cambiaron."""
    if not transaction_ids:
        return 0
    rows = (
        await conn.execute(_SET_REVIEW_STATUS, {"ids": list(transaction_ids), "status": status})
    ).all()
    return len(rows)


async def confirm_all_pending(conn: AsyncConnection, statement_id: str) -> int:
    """Confirma de una todo lo que quedo pendiente. Devuelve cuantas."""
    rows = (await conn.execute(_CONFIRM_PENDING, {"statement_id": statement_id})).all()
    return len(rows)


async def insert_manual(
    conn: AsyncConnection,
    *,
    statement_id: str,
    card_id: str | None,
    posted_date: date,
    description_raw: str,
    amount: Decimal,
    currency: str,
    direction: int,
    kind: str,
    dedupe_key: str,
    occurrence_index: int,
    installment_number: int | None = None,
    installment_total: int | None = None,
    notes: str | None = None,
) -> str:
    """Agrega una transaccion que el modelo no vio.

    `source_line` queda en null a proposito: no salio de ninguna linea del PDF, y
    fingir una haria que el check de anclaje la tome por buena.
    """
    row = (
        (
            await conn.execute(
                _INSERT_MANUAL,
                {
                    "statement_id": statement_id,
                    "card_id": card_id,
                    "posted_date": posted_date,
                    "description_raw": description_raw,
                    "source_line": None,
                    "amount": amount,
                    "currency": currency.upper(),
                    "direction": direction,
                    "kind": kind,
                    "installment_number": installment_number,
                    "installment_total": installment_total,
                    "dedupe_key": dedupe_key,
                    "occurrence_index": occurrence_index,
                    "notes": notes,
                },
            )
        )
        .mappings()
        .one()
    )
    return str(row["id"])


# ─────────────────────────────────────────────────────────────────────────────
# Enriquecimiento (Fase 5)
# ─────────────────────────────────────────────────────────────────────────────

_PENDING_ENRICHMENT = text(
    """
    select id::text as id, description_raw, kind, currency, amount, posted_date
      from app.transactions
     where statement_id = :statement_id
       and review_status <> 'rejected'
       and (merchant_id is null or category_id is null)
     order by posted_date
    """
)

_APPLY_ENRICHMENT = text(
    """
    update app.transactions
       set merchant_id = coalesce(cast(:merchant_id as uuid), merchant_id),
           category_id = coalesce(cast(:category_id as uuid), category_id)
     where id = :id
       -- Enriquecer no puede pisar una categoria que el usuario eligio a mano.
       and (review_status <> 'edited' or category_id is null)
    """
)

_SET_CATEGORY = text(
    """
    update app.transactions
       set category_id = cast(:category_id as uuid), review_status = 'edited'
     where id = :id
    """
)


@dataclass(frozen=True)
class PendingEnrichment:
    """Una transaccion a la que todavia le falta comercio o categoria."""

    id: str
    description_raw: str
    kind: str
    currency: str
    amount: Decimal
    posted_date: date


async def pending_enrichment(conn: AsyncConnection, statement_id: str) -> list[PendingEnrichment]:
    rows = (await conn.execute(_PENDING_ENRICHMENT, {"statement_id": statement_id})).mappings()
    return [PendingEnrichment(**row) for row in rows]


async def apply_enrichment(
    conn: AsyncConnection,
    transaction_id: str,
    *,
    merchant_id: str | None,
    category_id: str | None,
) -> None:
    await conn.execute(
        _APPLY_ENRICHMENT,
        {"id": transaction_id, "merchant_id": merchant_id, "category_id": category_id},
    )


async def set_category(conn: AsyncConnection, transaction_id: str, category_id: str) -> None:
    """Recategorizacion manual desde la UI. Queda como `edited`."""
    await conn.execute(_SET_CATEGORY, {"id": transaction_id, "category_id": category_id})
