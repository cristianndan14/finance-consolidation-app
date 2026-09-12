"""Implementacion del puerto contra Gemini (`google-genai`).

# Los dos parametros que no son por defecto y por que

- **`temperature = 0`.** Extraer un resumen es transcribir, no redactar. Con
  temperatura, dos corridas del mismo PDF dan listas distintas y el merge de un
  reparse ve transacciones "nuevas" que son las mismas leidas distinto.
- **`thinking_budget = 0`.** El razonamiento extendido se paga por token y no
  ayuda a copiar una tabla: la respuesta correcta esta en el texto de entrada.
  Se deja prendido para el enriquecimiento (Fase 5), que si es una decision.

# `response_schema` en vez de "devolveme JSON"

Con `response_mime_type="application/json"` mas `response_schema`, el API
restringe la decodificacion al schema. No hay que parsear markdown con un bloque
de codigo adentro ni manejar el caso "el modelo explico lo que iba a hacer antes
del JSON". Lo que llega siempre parsea; lo que puede fallar es el contenido, y de
eso se encarga Pydantic.

# El mapeo de errores

Un 429 o un 503 son pasajeros y el runner los reintenta con backoff. Una API key
invalida no: reintentar tres veces es garantia de fallar tres veces. La
distincion la hace `_translate`.
"""

from __future__ import annotations

import json
import time
from typing import Any, Final

from app.llm import cost, prompts, response_schemas
from app.llm.ports import (
    HeaderHints,
    LLMConfigError,
    LLMResponseError,
    LLMResult,
    LLMTransientError,
    LLMUsage,
)
from app.logging_config import get_logger
from app.schemas.llm import MerchantsPayload, StatementHeaderPayload, TransactionsPayload
from app.settings import Settings

log = get_logger(__name__)

PROVIDER: Final = "gemini"

# Codigos HTTP que vale la pena reintentar.
_TRANSIENT_CODES: Final = frozenset({408, 429, 500, 502, 503, 504})


class GeminiExtractor:
    """Adapter concreto. Lo instancia `factory.build()`, no se importa directo."""

    provider = PROVIDER

    def __init__(self, settings: Settings) -> None:
        api_key = settings.gemini_api_key.get_secret_value()
        if not api_key:
            raise LLMConfigError("falta GEMINI_API_KEY")

        # El import va adentro para que `app.llm.ports` se pueda importar (y los
        # tests con el fake correr) sin tener el SDK instalado ni configurado.
        try:
            from google import genai
        except ImportError as exc:  # pragma: no cover - dependencia declarada
            raise LLMConfigError("falta la dependencia google-genai") from exc

        self.model = settings.gemini_model
        self._client = genai.Client(api_key=api_key)

    # ─── Llamadas del puerto ────────────────────────────────────────────────
    async def extract_statement_header(
        self, *, text: str, hints: HeaderHints, prompt_version: str = prompts.PARSE_HEADER
    ) -> LLMResult[StatementHeaderPayload]:
        prompt = prompts.render(prompt_version, text=text, hints=_format_hints(hints))
        raw, usage, latency = await self._generate(prompt, response_schemas.STATEMENT_HEADER_SCHEMA)
        return LLMResult(
            data=_parse(StatementHeaderPayload, raw),
            raw=raw,
            provider=PROVIDER,
            model=self.model,
            prompt_version=prompt_version,
            usage=usage,
            latency_ms=latency,
        )

    async def extract_transactions(
        self,
        *,
        text_chunk: str,
        chunk_index: int,
        header: StatementHeaderPayload,
        prompt_version: str = prompts.PARSE_TRANSACTIONS,
    ) -> LLMResult[TransactionsPayload]:
        prompt = prompts.render(
            prompt_version,
            text_chunk=text_chunk,
            chunk_index=chunk_index,
            header=_format_header(header),
        )
        raw, usage, latency = await self._generate(prompt, response_schemas.TRANSACTIONS_SCHEMA)
        return LLMResult(
            data=_parse(TransactionsPayload, raw),
            raw=raw,
            provider=PROVIDER,
            model=self.model,
            prompt_version=prompt_version,
            usage=usage,
            latency_ms=latency,
        )

    async def repair_transactions(
        self,
        *,
        text: str,
        extracted_summary: str,
        delta_description: str,
        prompt_version: str = prompts.REPAIR_TRANSACTIONS,
    ) -> LLMResult[TransactionsPayload]:
        prompt = prompts.render(
            prompt_version,
            text=text,
            extracted_summary=extracted_summary,
            delta_description=delta_description,
        )
        raw, usage, latency = await self._generate(prompt, response_schemas.TRANSACTIONS_SCHEMA)
        return LLMResult(
            data=_parse(TransactionsPayload, raw),
            raw=raw,
            provider=PROVIDER,
            model=self.model,
            prompt_version=prompt_version,
            usage=usage,
            latency_ms=latency,
        )

    async def normalize_merchants(
        self,
        *,
        raw_keys: list[str],
        category_slugs: list[str],
        prompt_version: str = prompts.ENRICH_MERCHANTS,
    ) -> LLMResult[MerchantsPayload]:
        prompt = prompts.render(
            prompt_version,
            keys="\n".join(f"- {key}" for key in raw_keys),
            categories="\n".join(f"- {slug}" for slug in category_slugs),
        )
        # Aca si se deja pensar: decidir que "MERPAGO*MAMACHARESTO" es un
        # restaurante es una inferencia, no una transcripcion. Es la diferencia
        # con las llamadas de la etapa 2, donde el razonamiento solo agrega costo.
        raw, usage, latency = await self._generate(
            prompt, response_schemas.merchants_schema(category_slugs), thinking=True
        )
        return LLMResult(
            data=_parse(MerchantsPayload, raw),
            raw=raw,
            provider=PROVIDER,
            model=self.model,
            prompt_version=prompt_version,
            usage=usage,
            latency_ms=latency,
        )

    async def aclose(self) -> None:
        """El cliente de google-genai no expone cierre explicito."""
        return None

    # ─── Interno ────────────────────────────────────────────────────────────
    async def _generate(
        self, prompt: str, schema: dict[str, Any], *, thinking: bool = False
    ) -> tuple[dict[str, Any], LLMUsage, int]:
        from google.genai import types

        config = types.GenerateContentConfig(
            temperature=0,
            response_mime_type="application/json",
            response_schema=schema,
            # Transcribir no necesita razonamiento y el razonamiento se paga. El
            # enriquecimiento si lo necesita: decidir que comercio hay detras de
            # una abreviatura es inferencia, no copia.
            thinking_config=types.ThinkingConfig(thinking_budget=-1 if thinking else 0),
        )

        started = time.perf_counter()
        try:
            response = await self._client.aio.models.generate_content(
                model=self.model, contents=prompt, config=config
            )
        except Exception as exc:
            raise _translate(exc) from exc
        latency_ms = int((time.perf_counter() - started) * 1000)

        payload = _decode(response)
        usage = _usage(response, self.model)

        log.info(
            "llamada a Gemini",
            model=self.model,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cost_usd=str(usage.cost_usd),
            latency_ms=latency_ms,
        )
        return payload, usage, latency_ms


# ─── Helpers de modulo (testeables sin cliente) ─────────────────────────────
def _decode(response: Any) -> dict[str, Any]:
    """El JSON de la respuesta, ya como dict.

    Con `response_schema` el texto siempre deberia parsear. "Siempre deberia" no
    es "siempre": si el modelo corta por limite de tokens, llega un JSON
    truncado, y eso tiene que ser un error legible y no un `JSONDecodeError`
    suelto en el log de un job.
    """
    text = getattr(response, "text", None)
    if not text:
        reason = _finish_reason(response)
        raise LLMResponseError(f"el modelo no devolvio contenido (finish_reason={reason})")

    try:
        decoded = json.loads(text)
    except json.JSONDecodeError as exc:
        raise LLMResponseError(f"la respuesta no es JSON valido: {exc}", raw=text) from exc

    if not isinstance(decoded, dict):
        raise LLMResponseError(f"se esperaba un objeto JSON, llego {type(decoded).__name__}")
    return decoded


def _finish_reason(response: Any) -> str:
    candidates = getattr(response, "candidates", None) or []
    if not candidates:
        return "sin candidatos"
    return str(getattr(candidates[0], "finish_reason", "desconocido"))


def _usage(response: Any, model: str) -> LLMUsage:
    meta = getattr(response, "usage_metadata", None)
    input_tokens = int(getattr(meta, "prompt_token_count", 0) or 0)
    output_tokens = int(getattr(meta, "candidates_token_count", 0) or 0)
    return LLMUsage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cost_usd=cost.estimate(model, input_tokens=input_tokens, output_tokens=output_tokens),
    )


def _translate(exc: Exception) -> Exception:
    """Decide si el fallo se reintenta o no."""
    code = getattr(exc, "code", None) or getattr(exc, "status_code", None)
    if isinstance(code, int) and code in _TRANSIENT_CODES:
        return LLMTransientError(f"Gemini respondio {code}: {exc}")

    message = str(exc).lower()
    if any(word in message for word in ("timeout", "deadline", "unavailable", "rate limit")):
        return LLMTransientError(f"fallo pasajero llamando a Gemini: {exc}")
    if any(word in message for word in ("api key", "permission", "unauthorized", "not found")):
        return LLMConfigError(f"configuracion invalida de Gemini: {exc}")
    return LLMTransientError(f"error llamando a Gemini: {type(exc).__name__}: {exc}")


def _parse[T](model: type[T], raw: dict[str, Any]) -> T:
    from pydantic import ValidationError

    try:
        return model.model_validate(raw)  # type: ignore[attr-defined,no-any-return]
    except ValidationError as exc:
        raise LLMResponseError(
            f"la respuesta no encaja en el schema: {exc.error_count()} errores",
            raw=json.dumps(raw, ensure_ascii=False)[:2000],
        ) from exc


def _format_hints(hints: HeaderHints) -> str:
    lines = []
    if hints.issuer_name:
        lines.append(f"- Emisor esperado: {hints.issuer_name}")
    if hints.card_last4:
        lines.append(f"- Últimos 4 dígitos de la tarjeta: {hints.card_last4}")
    if hints.uploaded_on:
        lines.append(f"- El documento se subió el {hints.uploaded_on.isoformat()}")
    return "\n".join(lines) if lines else "- (sin datos previos del documento)"


def _format_header(header: StatementHeaderPayload) -> str:
    """La cabecera resumida, para darle contexto a la llamada de transacciones.

    Va en texto y no en JSON porque lo unico que tiene que hacer es orientar al
    modelo sobre moneda y periodo; un JSON invita a que lo copie en la salida.
    """
    if not header.statements:
        return "- (no se pudo leer la cabecera)"

    lines = []
    for statement in header.statements:
        parts = [f"- Moneda {statement.currency}"]
        if statement.closing_date:
            parts.append(f"cierre {statement.closing_date}")
        if statement.new_charges is not None:
            parts.append(f"consumos del período {statement.new_charges}")
        if statement.total_due is not None:
            parts.append(f"total a pagar {statement.total_due}")
        lines.append(", ".join(parts))
    return "\n".join(lines)
