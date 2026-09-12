"""Suscripciones detectadas: `app.subscriptions` y el vinculo con las transacciones.

# Por que la historia se lee desde aca y no desde `transactions.py`

La deteccion necesita algo que ninguna otra pantalla necesita: las transacciones
de un comercio **a traves de todos los resumenes**, no las de uno. Es una lectura
de `app.transactions`, pero la pregunta es de suscripciones, y dejarla aca evita
que el repositorio de transacciones —que ya es el mas grande del proyecto— crezca
con consultas que solo usa un job.

# Por que no hay `on conflict`

`app.subscriptions` solo tiene unique sobre `(user_id, id)`: no hay clave natural
por comercio, porque un comercio puede tener dos suscripciones (el plan personal
y el familiar). El upsert es entonces explicito: buscar, y crear o actualizar.

Y lo que se actualiza es acotado a proposito. `status` **nunca** se toca: la
deteccion lo deja en `suspected` cuando crea la fila, y si el usuario la confirmo
o la dio de baja, esa decision es suya. Una re-corrida del enrich que pisara el
estado desharia la confirmacion todos los meses.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.domain.subscriptions import Occurrence, SubscriptionCandidate

# Mas atras no aporta: una serie que se corto hace dos años no es una suscripcion
# vigente, y el costo de traerla es una tabla mas grande para nada.
HISTORY_MONTHS = 18

_HISTORY = text(
    """
    select t.id::text          as transaction_id,
           t.merchant_id::text as merchant_id,
           t.posted_date,
           t.amount,
           t.currency,
           t.kind,
           t.installment_total,
           t.direction
      from app.transactions t
     where t.merchant_id = any(cast(:merchant_ids as uuid[]))
       and t.review_status <> 'rejected'
       and t.posted_date >= :since
     order by t.posted_date
    """
)

_SELECT = """
select id::text          as id,
       merchant_id::text as merchant_id,
       label,
       nominal_amount,
       currency,
       cadence,
       first_seen,
       last_seen,
       status
  from app.subscriptions
"""

_BY_MERCHANT = text(_SELECT + " where merchant_id = :merchant_id order by created_at limit 1")

_INSERT = text(
    """
    insert into app.subscriptions (
        merchant_id, label, nominal_amount, currency, cadence, first_seen, last_seen
    )
    values (
        :merchant_id, :label, :nominal_amount, :currency, :cadence, :first_seen, :last_seen
    )
    returning id::text as id
    """
)

_UPDATE = text(
    """
    update app.subscriptions
       set label          = :label,
           nominal_amount = :nominal_amount,
           currency       = :currency,
           cadence        = :cadence,
           first_seen     = least(first_seen, :first_seen),
           last_seen      = greatest(last_seen, :last_seen)
     where id = :id
    """
)

_LINK = text(
    """
    update app.transactions
       set is_recurring    = true,
           subscription_id = cast(:subscription_id as uuid)
     where id = any(cast(:ids as uuid[]))
    """
)


@dataclass(frozen=True)
class Subscription:
    id: str
    merchant_id: str
    label: str
    nominal_amount: Decimal | None
    currency: str | None
    cadence: str
    first_seen: date | None
    last_seen: date | None
    status: str


async def history(
    conn: AsyncConnection, merchant_ids: Sequence[str], *, since: date
) -> dict[str, list[Occurrence]]:
    """Las transacciones vivas de cada comercio, agrupadas por comercio."""
    if not merchant_ids:
        return {}

    rows = (
        await conn.execute(_HISTORY, {"merchant_ids": list(merchant_ids), "since": since})
    ).mappings()

    grouped: dict[str, list[Occurrence]] = {}
    for row in rows:
        grouped.setdefault(row["merchant_id"], []).append(
            Occurrence(
                transaction_id=row["transaction_id"],
                posted_date=row["posted_date"],
                amount=Decimal(str(row["amount"])),
                currency=row["currency"],
                kind=row["kind"],
                installment_total=row["installment_total"],
                direction=int(row["direction"]),
            )
        )
    return grouped


async def get_by_merchant(conn: AsyncConnection, merchant_id: str) -> Subscription | None:
    row = (await conn.execute(_BY_MERCHANT, {"merchant_id": merchant_id})).mappings().one_or_none()
    return Subscription(**row) if row else None


async def save_detected(conn: AsyncConnection, candidate: SubscriptionCandidate) -> str:
    """Crea la suscripcion o refresca la que ya existia. Devuelve su id."""
    params = {
        "merchant_id": candidate.merchant_id,
        "label": candidate.label,
        "nominal_amount": candidate.nominal_amount,
        "currency": candidate.currency,
        "cadence": candidate.cadence,
        "first_seen": candidate.first_seen,
        "last_seen": candidate.last_seen,
    }

    existing = await get_by_merchant(conn, candidate.merchant_id)
    if existing is None:
        row = (await conn.execute(_INSERT, params)).mappings().one()
        return str(row["id"])

    await conn.execute(_UPDATE, {**params, "id": existing.id})
    return existing.id


async def link_transactions(
    conn: AsyncConnection, *, subscription_id: str, transaction_ids: Sequence[str]
) -> None:
    if transaction_ids:
        await conn.execute(
            _LINK, {"subscription_id": subscription_id, "ids": list(transaction_ids)}
        )
