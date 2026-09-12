"""Etapa 2: del texto guardado a las transacciones. Nunca toca el PDF.

# Por que parte de `document_texts` y no del archivo

La etapa 1 ya guardo el texto. Releerlo de la base cuesta un query; volver a
bajar el PDF de Storage y reprocesarlo con pdfplumber cuesta segundos y ancho de
banda por cada iteracion de prompt. Como iterar prompts sobre el corpus real es
justamente la actividad principal de esta fase, la diferencia decide si se hace
seguido o no se hace.

# Por que son dos llamadas y no una

La cabecera (seis numeros, schema chico) y las transacciones (una tabla larga,
schema grande) se cumplen con precision muy distinta. Pedirlas juntas empeora las
dos: el modelo reparte atencion entre encontrar el total a pagar y transcribir
300 lineas. Ademas la cabecera sirve de contexto para las transacciones —
sobre todo la fecha de cierre, que es lo que permite resolver un `15/03` — asi
que el orden tampoco es casual.

# El unico reintento automatico

Si la suma no cuadra, se llama **una sola vez** a `repair` con el delta exacto.
Un loop de reparaciones quema tokens sin converger: si el modelo no encontro lo
que falta con el numero en la mano, una segunda pasada tampoco. Lo que sigue es
la pantalla de revision, donde una persona mira el PDF.

# Que pasa con lo que no cuadra

Se persiste igual, con `status = 'needs_review'` y el delta anotado. Descartar una
extraccion que no cuadra haria desaparecer gastos reales en silencio; mostrarla
marcada convierte el problema en una tarea de dos minutos.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from app.domain import dedupe as dedupe_mod
from app.domain import validation
from app.domain.models import (
    Discarded,
    ParsedStatementHeader,
    ParsedTransaction,
    normalize_all,
)
from app.infra import db
from app.llm import prompts
from app.llm.ports import (
    HeaderHints,
    LLMBudgetExceededError,
    LLMExtractor,
    LLMResult,
    LLMUsage,
)
from app.logging_config import get_logger
from app.pdf import chunking
from app.repositories import document_texts as texts_repo
from app.repositories import documents as documents_repo
from app.repositories import extraction_runs as runs_repo
from app.repositories import llm_usage as usage_repo
from app.repositories import statements as statements_repo
from app.repositories import transactions as transactions_repo
from app.schemas.llm import LLMTransaction, StatementHeaderPayload, TransactionsPayload
from app.settings import Settings

log = get_logger(__name__)


class ParseError(Exception):
    """No se puede parsear el documento por como esta, no por un fallo pasajero."""

    def __init__(self, message: str, *, reason: str) -> None:
        super().__init__(message)
        self.message = message
        self.reason = reason


@dataclass
class ParseOutcome:
    """El resultado de una corrida completa de la etapa 2."""

    document_id: str
    report: validation.ValidationReport
    provider: str = ""
    model: str = ""
    # Lo que devolvio el modelo, sin interpretar: la cabecera y un elemento por
    # fragmento. Se persiste entero en `extraction_runs.raw_response`.
    raw: dict[str, Any] = field(default_factory=dict)
    transactions: list[ParsedTransaction] = field(default_factory=list)
    headers: list[ParsedStatementHeader] = field(default_factory=list)
    usage: LLMUsage = field(default_factory=LLMUsage)
    repaired: bool = False
    statement_ids: list[str] = field(default_factory=list)
    inserted: int = 0
    updated: int = 0
    deleted: int = 0

    @property
    def status(self) -> str:
        return self.report.status

    def summary(self) -> dict[str, Any]:
        """Lo que queda en `processing_jobs.result` y sale por el CLI."""
        return {
            "transactions": len(self.transactions),
            "currencies": [h.currency for h in self.headers],
            "status": self.status,
            "balanced": self.report.balanced,
            "repaired": self.repaired,
            "inserted": self.inserted,
            "updated": self.updated,
            "deleted": self.deleted,
            "cost_usd": str(self.usage.cost_usd),
            "issues": [issue.code for issue in self.report.issues],
        }


# ─────────────────────────────────────────────────────────────────────────────
# Entrada principal
# ─────────────────────────────────────────────────────────────────────────────
async def parse_document(
    *,
    user_id: str,
    document_id: str,
    extractor: LLMExtractor,
    settings: Settings,
    dry_run: bool = False,
    allow_repair: bool = True,
    header_prompt: str = prompts.PARSE_HEADER,
    transactions_prompt: str = prompts.PARSE_TRANSACTIONS,
    repair_prompt: str = prompts.REPAIR_TRANSACTIONS,
) -> ParseOutcome:
    """Corre la etapa 2 completa sobre un documento ya extraido.

    Con `dry_run=True` hace todas las llamadas al modelo y toda la validacion,
    pero no escribe nada. Es el modo del CLI: permite comparar prompts contra el
    corpus real sin ensuciar la base ni pisar transacciones que el usuario
    reviso.
    """
    document, doc_text = await _load(user_id, document_id)

    await _check_budget(user_id, settings)

    run_id: str | None = None
    if not dry_run:
        async with db.system_tx(user_id) as conn:
            run_id = await runs_repo.start(
                conn,
                document_id=document_id,
                document_text_id=doc_text.id,
                stage=runs_repo.STAGE_PARSE,
                provider=extractor.provider,
                model=extractor.model,
                prompt_version=transactions_prompt,
            )

    try:
        outcome = await _run(
            document=document,
            doc_text=doc_text,
            extractor=extractor,
            settings=settings,
            allow_repair=allow_repair,
            header_prompt=header_prompt,
            transactions_prompt=transactions_prompt,
            repair_prompt=repair_prompt,
        )
    except Exception as exc:
        if run_id is not None:
            async with db.system_tx(user_id) as conn:
                await runs_repo.finish(
                    conn, run_id, status="failed", error=f"{type(exc).__name__}: {exc}"
                )
        raise

    if dry_run or run_id is None:
        return outcome

    await _persist(user_id=user_id, document=document, run_id=run_id, outcome=outcome)
    return outcome


# ─────────────────────────────────────────────────────────────────────────────
# Las llamadas al modelo
# ─────────────────────────────────────────────────────────────────────────────
async def _run(
    *,
    document: documents_repo.Document,
    doc_text: texts_repo.DocumentText,
    extractor: LLMExtractor,
    settings: Settings,
    allow_repair: bool,
    header_prompt: str,
    transactions_prompt: str,
    repair_prompt: str,
) -> ParseOutcome:
    pages = doc_text.page_texts
    usage = LLMUsage()

    # ─── 1. Cabecera ────────────────────────────────────────────────────────
    header_result = await extractor.extract_statement_header(
        text=chunking.header_text(pages),
        hints=HeaderHints(
            card_last4=None,
            issuer_name=None,
            uploaded_on=document.uploaded_at.date() if document.uploaded_at else None,
        ),
        prompt_version=header_prompt,
    )
    usage = usage + header_result.usage
    headers = _headers_from(header_result.data, fallback_closing=_fallback_closing(document))
    closing = _primary_closing(headers)

    # ─── 2. Transacciones, en paralelo ──────────────────────────────────────
    chunks = chunking.chunk_pages(pages)
    raw_transactions, warnings, chunk_usage, chunk_raw = await _extract_chunks(
        extractor=extractor,
        chunks=chunks,
        header=header_result.data,
        prompt_version=transactions_prompt,
        concurrency=settings.llm_max_concurrency,
    )
    usage = usage + chunk_usage

    parsed, discarded = normalize_all(raw_transactions, closing=closing)
    # El solapamiento entre fragmentos trae repeticiones previstas. Se limpian
    # ANTES de asignar los indices de ocurrencia: al reves, los dos cafes
    # legitimos del mismo dia se perderian.
    parsed = dedupe_mod.drop_overlap_duplicates(parsed)

    report = _validate(parsed, headers, doc_text.full_text, settings, warnings, discarded)

    # ─── 3. Un solo intento de reparacion ───────────────────────────────────
    repaired = False
    if allow_repair and not report.balanced and parsed:
        parsed, report, repair_usage, repaired = await _repair(
            extractor=extractor,
            full_text=doc_text.full_text,
            parsed=parsed,
            headers=headers,
            closing=closing,
            report=report,
            settings=settings,
            warnings=warnings,
            discarded=discarded,
            prompt_version=repair_prompt,
        )
        usage = usage + repair_usage

    parsed = _flag_suspects(parsed, report)

    return ParseOutcome(
        document_id=document.id,
        report=report,
        provider=extractor.provider,
        model=extractor.model,
        raw={
            "header": header_result.raw,
            "chunks": chunk_raw,
            "header_prompt": header_prompt,
            "transactions_prompt": transactions_prompt,
        },
        transactions=parsed,
        headers=headers,
        usage=usage,
        repaired=repaired,
    )


async def _extract_chunks(
    *,
    extractor: LLMExtractor,
    chunks: list[chunking.TextChunk],
    header: StatementHeaderPayload,
    prompt_version: str,
    concurrency: int,
) -> tuple[list[LLMTransaction], list[str], LLMUsage, list[dict[str, Any]]]:
    """Una llamada por fragmento, con un semaforo.

    El semaforo no es por rendimiento sino por el rate limit del proveedor: sin
    el, un resumen de 12 paginas dispara 12 llamadas simultaneas y la mitad
    vuelve con 429, convirtiendo un documento normal en tres reintentos.
    """
    if not chunks:
        return [], [], LLMUsage(), []

    semaphore = asyncio.Semaphore(max(1, concurrency))

    async def one(chunk: chunking.TextChunk) -> LLMResult[TransactionsPayload]:
        async with semaphore:
            return await extractor.extract_transactions(
                text_chunk=chunk.text,
                chunk_index=chunk.index,
                header=header,
                prompt_version=prompt_version,
            )

    results = await asyncio.gather(*(one(chunk) for chunk in chunks))

    transactions: list[LLMTransaction] = []
    warnings: list[str] = []
    usage = LLMUsage()
    raw: list[dict[str, Any]] = []

    for chunk, result in zip(chunks, results, strict=True):
        usage = usage + result.usage
        transactions.extend(result.data.transactions)
        warnings.extend(
            f"[páginas {chunk.first_page}-{chunk.last_page}] {w}" for w in result.data.warnings
        )
        raw.append(
            {
                "chunk": chunk.index,
                "pages": [chunk.first_page, chunk.last_page],
                "response": result.raw,
            }
        )

    return transactions, warnings, usage, raw


async def _repair(
    *,
    extractor: LLMExtractor,
    full_text: str,
    parsed: list[ParsedTransaction],
    headers: list[ParsedStatementHeader],
    closing: date | None,
    report: validation.ValidationReport,
    settings: Settings,
    warnings: list[str],
    discarded: list[Discarded],
    prompt_version: str,
) -> tuple[list[ParsedTransaction], validation.ValidationReport, LLMUsage, bool]:
    """Una pasada de correccion con el delta exacto en la mano.

    Si la reparacion empeora el cuadre — pasa: el modelo agrega algo que ya
    estaba contado de otra forma — se descarta y se conserva el resultado
    original. Es lo unico honesto: la reparacion es una apuesta, y una apuesta
    que sale mal no puede dejar los numeros peor de lo que estaban.
    """
    result = await extractor.repair_transactions(
        text=full_text,
        extracted_summary=_summarize(parsed),
        delta_description=report.delta_description(),
        prompt_version=prompt_version,
    )

    extra, extra_discarded = normalize_all(list(result.data.transactions), closing=closing)
    if not extra:
        return parsed, report, result.usage, False

    merged = dedupe_mod.drop_overlap_duplicates([*parsed, *extra])
    if len(merged) == len(parsed):
        # El modelo devolvio solo transacciones que ya estaban. Es el caso mas
        # comun cuando no encuentra nada: repite lo que ve. Marcarlo como
        # "reparado" mentiria en la UI y en el log sobre algo que no cambio.
        return parsed, report, result.usage, False

    merged_report = _validate(
        merged,
        headers,
        full_text,
        settings,
        [*warnings, *(f"[reparación] {w}" for w in result.data.warnings)],
        [*discarded, *extra_discarded],
    )

    if _worse(merged_report, report):
        report.add(
            "repair_discarded",
            "info",
            "la pasada de reparación dejó el cuadre peor que antes y se descartó",
        )
        return parsed, report, result.usage, False

    return merged, merged_report, result.usage, True


def _worse(new: validation.ValidationReport, old: validation.ValidationReport) -> bool:
    """Compara dos reportes por el unico criterio que importa: el desvio total."""
    return _total_abs_delta(new) > _total_abs_delta(old)


def _total_abs_delta(report: validation.ValidationReport) -> Decimal:
    return sum(
        (abs(rec.delta) for rec in report.reconciliation if rec.delta is not None),
        Decimal("0"),
    )


# ─────────────────────────────────────────────────────────────────────────────
# Persistencia
# ─────────────────────────────────────────────────────────────────────────────
async def _persist(
    *,
    user_id: str,
    document: documents_repo.Document,
    run_id: str,
    outcome: ParseOutcome,
) -> None:
    """Todo en una transaccion: o queda el resumen entero, o no queda nada.

    Un estado intermedio —el statement escrito con los totales nuevos pero las
    transacciones viejas— es indistinguible desde la UI de un resumen que no
    cuadra, y llevaria al usuario a revisar a mano algo que solo hace falta
    reprocesar.
    """
    now = datetime.now(UTC)
    by_currency = _group_by_currency(outcome.transactions)
    currencies = sorted({*by_currency, *(h.currency for h in outcome.headers)})

    async with db.system_tx(user_id) as conn:
        for currency in currencies:
            header = next(
                (h for h in outcome.headers if h.currency == currency),
                ParsedStatementHeader(currency=currency),
            )
            year, month = header.period or (now.year, now.month)

            statement = await statements_repo.upsert(
                conn,
                document_id=document.id,
                card_id=document.card_id,
                currency=currency,
                period_year=year,
                period_month=month,
                closing_date=header.closing_date,
                due_date=header.due_date,
                previous_balance=header.previous_balance,
                payments_credits=header.payments_credits,
                new_charges=header.new_charges,
                total_due=header.total_due,
                minimum_payment=header.minimum_payment,
                status=outcome.report.status,
            )
            outcome.statement_ids.append(statement.id)

            merge = await transactions_repo.reconcile(
                conn,
                statement_id=statement.id,
                extraction_run_id=run_id,
                card_id=document.card_id,
                incoming=by_currency.get(currency, []),
            )
            outcome.inserted += merge.inserted
            outcome.updated += merge.updated
            outcome.deleted += merge.deleted
            outcome.report.conflicts.extend(merge.conflicts)
            outcome.report.orphans.extend(merge.orphans)

        await runs_repo.finish(
            conn,
            run_id,
            status="succeeded",
            input_tokens=outcome.usage.input_tokens,
            output_tokens=outcome.usage.output_tokens,
            cost_usd=outcome.usage.cost_usd,
            # La respuesta cruda del modelo, entera. Es lo que permite re-derivar
            # transacciones con logica de parseo nueva sin volver a llamarlo (ni
            # pagarlo): cuando se arregla el parser de fechas, los documentos ya
            # extraidos se pueden reprocesar gratis desde aca.
            raw_response=outcome.raw,
            validation=outcome.report.to_json(),
        )
        await runs_repo.supersede_previous(
            conn, document_id=document.id, stage=runs_repo.STAGE_PARSE, current_id=run_id
        )

        # El gasto se acumula en el mes CALENDARIO de la corrida, no en el
        # periodo del resumen: el tope existe para proteger la cuenta, y lo que
        # la protege es cuando se gasto, no de que mes era el PDF.
        await usage_repo.record(
            conn,
            year=now.year,
            month=now.month,
            provider=outcome.provider,
            model=outcome.model,
            input_tokens=outcome.usage.input_tokens,
            output_tokens=outcome.usage.output_tokens,
            cost_usd=outcome.usage.cost_usd,
        )

        await documents_repo.set_status(conn, document.id, "parsed", failure_reason=None)

    log.info(
        "documento parseado",
        document_id=document.id,
        transactions=len(outcome.transactions),
        status=outcome.status,
        inserted=outcome.inserted,
        updated=outcome.updated,
        deleted=outcome.deleted,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────
async def _load(
    user_id: str, document_id: str
) -> tuple[documents_repo.Document, texts_repo.DocumentText]:
    async with db.system_tx(user_id) as conn:
        document = await documents_repo.get(conn, document_id)
        if document is None:
            raise ParseError("el documento no existe", reason="document_not_found")

        doc_text = await texts_repo.latest_for_document(conn, document_id)

    if doc_text is None:
        raise ParseError(
            "el documento todavía no tiene texto extraído (etapa 1)", reason="no_document_text"
        )
    if not doc_text.has_text_layer:
        raise ParseError("el PDF no tiene capa de texto usable", reason="no_text_layer")

    return document, doc_text


async def _check_budget(user_id: str, settings: Settings) -> None:
    """Corta antes de llamar si el mes ya se gasto."""
    now = datetime.now(UTC)
    async with db.system_tx(user_id) as conn:
        usage = await usage_repo.month_usage(conn, year=now.year, month=now.month)

    if usage.exhausted(settings.llm_monthly_budget_usd):
        raise LLMBudgetExceededError(usage.spent_usd, settings.llm_monthly_budget_usd)


def _headers_from(
    payload: StatementHeaderPayload, *, fallback_closing: date | None
) -> list[ParsedStatementHeader]:
    return [
        ParsedStatementHeader.from_llm(header, fallback_closing=fallback_closing)
        for header in payload.statements
    ]


def _primary_closing(headers: list[ParsedStatementHeader]) -> date | None:
    """La fecha de cierre que se usa para inferir años.

    Las dos secciones de un resumen (pesos y dolares) cierran el mismo dia, asi
    que la primera que aparezca sirve para las dos.
    """
    for header in headers:
        if header.closing_date is not None:
            return header.closing_date
    return None


def _fallback_closing(document: documents_repo.Document) -> date | None:
    """Sin cierre leido, la fecha de subida es el ancla menos mala.

    Un resumen se sube a los pocos dias de emitirse, asi que la ventana de
    fechas plausibles alrededor de esa fecha suele contener el periodo real. No
    es exacto, y por eso el check de coherencia de fechas marca lo que se aleje.
    """
    return document.uploaded_at.date() if document.uploaded_at else None


def _validate(
    parsed: list[ParsedTransaction],
    headers: list[ParsedStatementHeader],
    full_text: str,
    settings: Settings,
    warnings: list[str],
    discarded: list[Discarded],
) -> validation.ValidationReport:
    return validation.validate(
        parsed,
        headers=headers,
        full_text=full_text,
        tolerances=validation.Tolerances(
            pct=settings.reconciliation_tolerance_pct,
            floors={
                "ARS": settings.reconciliation_floor_ars,
                "USD": settings.reconciliation_floor_usd,
            },
            default_floor=settings.reconciliation_floor_usd,
            source_line_min_ratio=settings.source_line_min_ratio,
        ),
        model_warnings=warnings,
        discarded=discarded,
    )


def _flag_suspects(
    parsed: list[ParsedTransaction], report: validation.ValidationReport
) -> list[ParsedTransaction]:
    """Le baja la confianza a las transacciones que no se anclan en el texto.

    No se borran: una transaccion marcada y visible es un dato que el usuario
    puede confirmar o rechazar en dos clics; una borrada es plata que
    desaparecio sin dejar rastro.
    """
    if not report.hallucination_suspects:
        return parsed

    suspects = set(report.hallucination_suspects)
    return [
        tx.with_confidence(validation.HALLUCINATION_CONFIDENCE) if index in suspects else tx
        for index, tx in enumerate(parsed)
    ]


def _group_by_currency(
    transactions: list[ParsedTransaction],
) -> dict[str, list[ParsedTransaction]]:
    grouped: dict[str, list[ParsedTransaction]] = {}
    for tx in transactions:
        grouped.setdefault(tx.currency, []).append(tx)
    return grouped


def _summarize(transactions: list[ParsedTransaction], limit: int = 400) -> str:
    """Lo ya extraido, compacto, para el prompt de reparacion.

    Una linea por transaccion y sin JSON: el objetivo es que el modelo sepa que
    no repetir, no que reproduzca la estructura.
    """
    if not transactions:
        return "(no se extrajo ninguna transacción)"

    lines = [
        f"- {tx.posted_date.isoformat()} | {tx.description_raw[:60]} | "
        f"{tx.amount} {tx.currency} | {'cargo' if tx.direction == 1 else 'crédito'}"
        for tx in transactions[:limit]
    ]
    if len(transactions) > limit:
        lines.append(f"- ... y {len(transactions) - limit} más")
    return "\n".join(lines)
