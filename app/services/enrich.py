"""Enriquecimiento: de `MERPAGO*SPOTIFY 1234` a Spotify / Suscripciones.

# Por que es un job aparte de la extraccion

Mejorar la categorizacion no tiene por que costar una re-extraccion. Son
preguntas distintas —"que dice el PDF" y "que significa"— y separarlas permite
reprocesar la segunda sobre meses ya extraidos, gratis en lo que ya esta en
cache y barato en lo que no.

# El embudo, que es lo que hace que esto no cueste plata

Cada transaccion pasa por cuatro filtros, en orden de costo creciente. Solo lo
que sobrevive a los tres primeros llega al modelo:

    1. por `kind`        impuestos, intereses, comisiones, pagos   ~1/3 de las lineas
    2. por alias         lo que ya se resolvio alguna vez           crece con el uso
    3. por regla         COTO, YPF, UBER, Netflix...                los habituales
    4. por LLM           el resto, en UN solo batch

El primer mes el modelo ve bastante; el tercero, casi nada. Y lo que el usuario
corrige a mano entra al cache como `source = 'user'` y no se vuelve a preguntar
nunca — es la señal mas confiable que el sistema tiene.

# Que no hace

No pisa una categoria que el usuario eligio. La regla es la misma que en la
reconciliacion de la etapa 2: el automatismo puede escribir donde solo escribio
el automatismo.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncConnection

from app.domain import categorization
from app.infra import db
from app.llm import prompts
from app.llm.ports import LLMBudgetExceededError, LLMExtractor, LLMUsage
from app.logging_config import get_logger
from app.repositories import categories as categories_repo
from app.repositories import llm_usage as usage_repo
from app.repositories import merchants as merchants_repo
from app.repositories import statements as statements_repo
from app.repositories import transactions as transactions_repo
from app.settings import Settings

log = get_logger(__name__)

# Un batch mas grande que esto empieza a perder precision: el modelo se cansa de
# listas largas igual que con las tablas de consumos.
MAX_KEYS_PER_CALL = 60


class EnrichError(Exception):
    def __init__(self, message: str, *, reason: str) -> None:
        super().__init__(message)
        self.message = message
        self.reason = reason


@dataclass
class EnrichOutcome:
    statement_id: str
    total: int = 0
    by_kind: int = 0
    by_alias: int = 0
    by_rule: int = 0
    by_llm: int = 0
    unresolved: int = 0
    usage: LLMUsage = field(default_factory=LLMUsage)

    @property
    def resolved(self) -> int:
        return self.by_kind + self.by_alias + self.by_rule + self.by_llm

    @property
    def cache_hit_ratio(self) -> float:
        """Que proporcion se resolvio sin pagarle al modelo."""
        if not self.total:
            return 1.0
        return (self.total - self.by_llm) / self.total

    def summary(self) -> dict[str, Any]:
        return {
            "transactions": self.total,
            "by_kind": self.by_kind,
            "by_alias": self.by_alias,
            "by_rule": self.by_rule,
            "by_llm": self.by_llm,
            "unresolved": self.unresolved,
            "cache_hit_ratio": round(self.cache_hit_ratio, 3),
            "cost_usd": str(self.usage.cost_usd),
        }


async def enrich_statement(
    *,
    user_id: str,
    statement_id: str,
    extractor: LLMExtractor,
    settings: Settings,
    prompt_version: str = prompts.ENRICH_MERCHANTS,
) -> EnrichOutcome:
    """Resuelve comercio y categoria de las transacciones que aun no los tienen."""
    outcome = EnrichOutcome(statement_id=statement_id)

    async with db.system_tx(user_id) as conn:
        statement = await statements_repo.get(conn, statement_id)
        if statement is None:
            raise EnrichError("ese resumen no existe", reason="statement_not_found")

        pending = await transactions_repo.pending_enrichment(conn, statement_id)
        catalog = await categories_repo.list_active(conn)

    outcome.total = len(pending)
    if not pending:
        return outcome

    by_slug = {category.slug: category for category in catalog}

    # ─── 1 y 3: lo que se resuelve sin red ──────────────────────────────────
    resolved: dict[str, _Resolution] = {}
    unknown_keys: dict[str, str] = {}  # clave -> una descripcion de ejemplo

    for tx in pending:
        key = categorization.merchant_key(tx.description_raw)
        slug = categorization.categorize(kind=tx.kind, description=tx.description_raw)

        if slug is not None and slug in by_slug:
            source = "kind" if tx.kind in categorization.KIND_CATEGORY else "rule"
            resolved[tx.id] = _Resolution(category_id=by_slug[slug].id, source=source)
        else:
            unknown_keys.setdefault(key, tx.description_raw)

    # ─── 2: la memoria de alias ─────────────────────────────────────────────
    async with db.system_tx(user_id) as conn:
        hits = await merchants_repo.lookup_aliases(conn, list(unknown_keys))

    still_unknown = [key for key in unknown_keys if key not in hits]

    # ─── 4: lo que queda, al modelo ─────────────────────────────────────────
    mappings: dict[str, _Mapping] = {}
    if still_unknown:
        await _check_budget(user_id, settings)
        mappings, outcome.usage = await _ask_model(
            extractor=extractor,
            keys=still_unknown,
            category_slugs=sorted(by_slug),
            prompt_version=prompt_version,
        )

    # ─── Persistencia ───────────────────────────────────────────────────────
    async with db.system_tx(user_id) as conn:
        await _persist_new_merchants(conn, mappings=mappings, by_slug=by_slug)
        # Se releen: los alias recien creados ahora tambien son hits.
        hits = await merchants_repo.lookup_aliases(conn, list(unknown_keys))

        for tx in pending:
            decision = resolved.get(tx.id)
            if decision is None:
                key = categorization.merchant_key(tx.description_raw)
                hit = hits.get(key)
                if hit is None:
                    outcome.unresolved += 1
                    continue
                decision = _Resolution(
                    category_id=hit.default_category_id,
                    merchant_id=hit.merchant_id,
                    source="llm" if key in mappings else "alias",
                )

            await transactions_repo.apply_enrichment(
                conn,
                tx.id,
                merchant_id=decision.merchant_id,
                category_id=decision.category_id,
            )
            _count(outcome, decision.source)

        await merchants_repo.touch_aliases(conn, [key for key in unknown_keys if key in hits])

        if outcome.usage.cost_usd:
            now = datetime.now(UTC)
            await usage_repo.record(
                conn,
                year=now.year,
                month=now.month,
                provider=extractor.provider,
                model=extractor.model,
                input_tokens=outcome.usage.input_tokens,
                output_tokens=outcome.usage.output_tokens,
                cost_usd=outcome.usage.cost_usd,
            )

    log.info("resumen enriquecido", statement_id=statement_id, **outcome.summary())
    return outcome


# ─────────────────────────────────────────────────────────────────────────────
# Interno
# ─────────────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class _Resolution:
    category_id: str | None
    merchant_id: str | None = None
    source: str = "kind"


@dataclass(frozen=True)
class _Mapping:
    canonical_name: str
    category_slug: str | None
    confidence: float | None


async def _ask_model(
    *,
    extractor: LLMExtractor,
    keys: list[str],
    category_slugs: list[str],
    prompt_version: str,
) -> tuple[dict[str, _Mapping], LLMUsage]:
    """Un batch por tanda. Devuelve lo que el modelo pudo mapear.

    El apareo es por `raw_key` y no por posicion: el modelo puede reordenar o
    saltear elementos, y aparear por indice le asignaria el comercio de una
    transaccion a otra sin que nada falle.
    """
    mappings: dict[str, _Mapping] = {}
    usage = LLMUsage()

    for start in range(0, len(keys), MAX_KEYS_PER_CALL):
        batch = keys[start : start + MAX_KEYS_PER_CALL]
        result = await extractor.normalize_merchants(
            raw_keys=batch, category_slugs=category_slugs, prompt_version=prompt_version
        )
        usage = usage + result.usage

        wanted = set(batch)
        for item in result.data.merchants:
            if item.raw_key not in wanted:
                # El modelo devolvio una clave que nadie pidio. Se ignora en vez
                # de crear un comercio fantasma.
                log.warning("el modelo devolvio una clave no pedida", raw_key=item.raw_key[:60])
                continue
            mappings[item.raw_key] = _Mapping(
                canonical_name=item.canonical_name,
                category_slug=item.category_slug,
                confidence=item.confidence,
            )

    return mappings, usage


async def _persist_new_merchants(
    conn: AsyncConnection,
    *,
    mappings: dict[str, _Mapping],
    by_slug: dict[str, categories_repo.Category],
) -> None:
    """Crea (o reusa) el comercio y deja el alias para la proxima vez."""
    for raw_key, mapping in mappings.items():
        category = by_slug.get(mapping.category_slug or "")
        merchant = await merchants_repo.upsert(
            conn,
            slug=categorization.merchant_slug(mapping.canonical_name),
            canonical_name=mapping.canonical_name,
            default_category_id=category.id if category else None,
        )
        await merchants_repo.remember_alias(
            conn,
            raw_key=raw_key,
            merchant_id=merchant.id,
            source="llm",
            confidence=mapping.confidence,
        )


def _count(outcome: EnrichOutcome, source: str) -> None:
    if source == "kind":
        outcome.by_kind += 1
    elif source == "rule":
        outcome.by_rule += 1
    elif source == "alias":
        outcome.by_alias += 1
    else:
        outcome.by_llm += 1


async def _check_budget(user_id: str, settings: Settings) -> None:
    now = datetime.now(UTC)
    async with db.system_tx(user_id) as conn:
        usage = await usage_repo.month_usage(conn, year=now.year, month=now.month)

    if usage.exhausted(settings.llm_monthly_budget_usd):
        raise LLMBudgetExceededError(usage.spent_usd, settings.llm_monthly_budget_usd)


async def recategorize(
    conn: AsyncConnection, *, transaction_id: str, category_id: str, remember: bool = True
) -> None:
    """Cambio manual de categoria, y —si se pide— memoria para la proxima.

    `remember` es lo que convierte una correccion puntual en una regla: el alias
    pasa a `source = 'user'` y el modelo no vuelve a opinar sobre ese comercio.
    """
    current = await transactions_repo.get(conn, transaction_id)
    if current is None:
        raise EnrichError("esa transaccion no existe", reason="not_found")

    await transactions_repo.set_category(conn, transaction_id, category_id)

    if not remember or current.merchant_id is None:
        return

    await merchants_repo.remember_alias(
        conn,
        raw_key=categorization.merchant_key(current.description_raw),
        merchant_id=current.merchant_id,
        source="user",
        confidence=1.0,
    )
