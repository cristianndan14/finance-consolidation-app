"""Implementacion del puerto que replaya respuestas grabadas.

# Para que existe

La etapa 2 tiene la logica mas delicada del proyecto: el merge de chunks, el
dedupe del solapamiento, los ocho checks de validacion y la reconciliacion que no
puede pisar el trabajo manual del usuario. Todo eso hay que testearlo muchas
veces y en muchos casos borde.

Con el adapter real, cada corrida de esos tests seria una llamada paga, lenta y
no deterministica — o sea, tests que no se corren. Con cassettes son gratis,
instantaneos y dan el mismo resultado siempre, que es lo unico que permite
escribir un test para "el modelo devolvio dos veces la misma transaccion porque
cayo en el solapamiento".

# El formato del cassette

Un JSON por caso, con la forma:

    {
      "header": { "statements": [...] },
      "chunks": [ {"transactions": [...]}, ... ],
      "repair": {"transactions": [...]}
    }

`chunks` se consume en orden: la llamada N devuelve el elemento N. Si se piden
mas chunks que los grabados, devuelve vacio — que es exactamente lo que pasa
cuando un documento se corta en mas pedazos de los previstos, y conviene que el
test lo vea en vez de explotar.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Final

from app.llm.ports import HeaderHints, LLMResponseError, LLMResult, LLMUsage
from app.schemas.llm import MerchantsPayload, StatementHeaderPayload, TransactionsPayload

PROVIDER: Final = "fake"


class FakeExtractor:
    """Extractor deterministico. No tiene red ni credenciales."""

    provider = PROVIDER

    def __init__(
        self,
        *,
        header: StatementHeaderPayload | None = None,
        chunks: list[TransactionsPayload] | None = None,
        repair: TransactionsPayload | None = None,
        merchants: MerchantsPayload | None = None,
        model: str = "fake",
        latency_ms: int = 0,
        usage: LLMUsage | None = None,
    ) -> None:
        self.model = model
        self._header = header or StatementHeaderPayload()
        self._chunks = chunks or []
        self._repair = repair or TransactionsPayload()
        self._merchants = merchants or MerchantsPayload()
        self._latency_ms = latency_ms
        self._usage = usage or LLMUsage(input_tokens=1000, output_tokens=500)

        # Lo que se le pidio, para que los tests puedan afirmar sobre el
        # chunking y sobre que el reparse no vuelva a llamar de mas.
        self.calls: list[dict[str, Any]] = []

    # ─── Construccion ───────────────────────────────────────────────────────
    @classmethod
    def from_cassette(cls, path: str | Path, **overrides: Any) -> FakeExtractor:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls.from_dict(data, **overrides)

    @classmethod
    def from_dict(cls, data: dict[str, Any], **overrides: Any) -> FakeExtractor:
        return cls(
            header=StatementHeaderPayload.model_validate(data.get("header", {})),
            chunks=[TransactionsPayload.model_validate(chunk) for chunk in data.get("chunks", [])],
            repair=TransactionsPayload.model_validate(data.get("repair", {})),
            merchants=MerchantsPayload.model_validate(data.get("merchants", {})),
            **overrides,
        )

    # ─── Puerto ─────────────────────────────────────────────────────────────
    async def extract_statement_header(
        self, *, text: str, hints: HeaderHints, prompt_version: str
    ) -> LLMResult[StatementHeaderPayload]:
        self.calls.append({"call": "header", "chars": len(text), "hints": hints})
        return self._result(self._header, prompt_version)

    async def extract_transactions(
        self,
        *,
        text_chunk: str,
        chunk_index: int,
        header: StatementHeaderPayload,
        prompt_version: str,
    ) -> LLMResult[TransactionsPayload]:
        self.calls.append({"call": "transactions", "chunk": chunk_index, "chars": len(text_chunk)})

        if chunk_index < len(self._chunks):
            payload = self._chunks[chunk_index]
        else:
            payload = TransactionsPayload(
                warnings=[f"el cassette no tiene respuesta para el fragmento {chunk_index}"]
            )
        return self._result(payload, prompt_version)

    async def repair_transactions(
        self,
        *,
        text: str,
        extracted_summary: str,
        delta_description: str,
        prompt_version: str,
    ) -> LLMResult[TransactionsPayload]:
        self.calls.append({"call": "repair", "delta": delta_description})
        return self._result(self._repair, prompt_version)

    async def normalize_merchants(
        self, *, raw_keys: list[str], category_slugs: list[str], prompt_version: str
    ) -> LLMResult[MerchantsPayload]:
        self.calls.append({"call": "merchants", "keys": list(raw_keys)})
        return self._result(self._merchants, prompt_version)

    async def aclose(self) -> None:
        return None

    # ─── Interno ────────────────────────────────────────────────────────────
    def _result[T](self, data: T, prompt_version: str) -> LLMResult[T]:
        return LLMResult(
            data=data,
            raw=data.model_dump(mode="json"),  # type: ignore[attr-defined]
            provider=PROVIDER,
            model=self.model,
            prompt_version=prompt_version,
            usage=self._usage,
            latency_ms=self._latency_ms,
        )


class FailingExtractor:
    """Falla siempre. Para testear el camino de error sin mockear el SDK."""

    provider = PROVIDER
    model = "fake-failing"

    def __init__(self, error: Exception | None = None) -> None:
        self._error = error or LLMResponseError("el modelo devolvio basura")

    async def extract_statement_header(self, **_kwargs: Any) -> LLMResult[StatementHeaderPayload]:
        raise self._error

    async def extract_transactions(self, **_kwargs: Any) -> LLMResult[TransactionsPayload]:
        raise self._error

    async def repair_transactions(self, **_kwargs: Any) -> LLMResult[TransactionsPayload]:
        raise self._error

    async def normalize_merchants(self, **_kwargs: Any) -> LLMResult[MerchantsPayload]:
        raise self._error

    async def aclose(self) -> None:
        return None
