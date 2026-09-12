"""El adapter de LLM: prompts, costo, el fake y los helpers de Gemini.

Nada de esto pega contra la red. Los helpers de `gemini.py` son funciones de
modulo justamente para poder testear la traduccion de errores y la decodificacion
de respuestas sin un cliente ni una API key — que es donde estan los bugs que
importan: un 429 clasificado como permanente deja un documento sin procesar para
siempre.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.llm import cost, factory, fake, gemini, prompts, response_schemas
from app.llm.ports import LLMConfigError, LLMResponseError, LLMTransientError, LLMUsage
from app.schemas.llm import StatementHeaderPayload
from app.settings import Settings


class TestCosto:
    def test_calcula_con_los_precios_de_la_tabla(self) -> None:
        # gemini-2.5-flash: USD 0,30 por millon de entrada y 2,50 de salida.
        value = cost.estimate("gemini-2.5-flash", input_tokens=1_000_000, output_tokens=1_000_000)

        assert value == Decimal("2.800000")

    def test_un_modelo_desconocido_no_rompe_la_extraccion(self) -> None:
        assert cost.estimate("modelo-que-no-existe", input_tokens=100, output_tokens=100) == 0

    def test_sin_tokens_no_hay_costo(self) -> None:
        assert cost.estimate("gemini-2.5-flash", input_tokens=0, output_tokens=0) == 0


class TestUsage:
    def test_se_suma_entre_llamadas(self) -> None:
        total = LLMUsage(100, 50, Decimal("0.01")) + LLMUsage(200, 25, Decimal("0.02"))

        assert (total.input_tokens, total.output_tokens) == (300, 75)
        assert total.cost_usd == Decimal("0.03")


class TestPrompts:
    def test_las_tres_versiones_del_codigo_existen_en_disco(self) -> None:
        disponibles = prompts.available()

        for version in (
            prompts.PARSE_HEADER_V1,
            prompts.PARSE_TRANSACTIONS_V1,
            prompts.REPAIR_TRANSACTIONS_V1,
        ):
            assert version in disponibles

    def test_rellena_los_huecos(self) -> None:
        rendered = prompts.render(
            prompts.PARSE_TRANSACTIONS_V1,
            text_chunk="15/03 COMERCIO 1.000,00",
            chunk_index=0,
            header="- Moneda ARS",
        )

        assert "15/03 COMERCIO 1.000,00" in rendered
        assert "- Moneda ARS" in rendered
        # Ningun hueco quedo sin rellenar.
        assert "{text_chunk}" not in rendered

    def test_una_version_inexistente_dice_cuales_hay(self) -> None:
        with pytest.raises(prompts.PromptNotFoundError, match="Disponibles"):
            prompts.load("parse_transactions_v99")

    def test_un_nombre_con_path_traversal_se_rechaza(self) -> None:
        with pytest.raises(prompts.PromptNotFoundError):
            prompts.load("../../../etc/passwd")

    def test_falta_un_valor_y_lo_dice(self) -> None:
        with pytest.raises(prompts.PromptNotFoundError, match="falta el valor"):
            prompts.render(prompts.PARSE_HEADER_V1, text="algo")


class TestResponseSchemas:
    def test_los_campos_obligatorios_incluyen_el_ancla_anti_alucinacion(self) -> None:
        required = response_schemas.TRANSACTIONS_SCHEMA["properties"]["transactions"]["items"][
            "required"
        ]

        assert "source_line" in required
        assert "amount" in required
        assert "direction" in required

    def test_los_montos_se_piden_como_string(self) -> None:
        """Un JSON number llegaria como float y arruinaria el cuadre por centavos."""
        properties = response_schemas.TRANSACTIONS_SCHEMA["properties"]["transactions"]["items"][
            "properties"
        ]

        assert properties["amount"]["type"] == "string"
        assert properties["purchase_total_amount"]["type"] == "string"


class TestFakeExtractor:
    async def test_devuelve_lo_grabado_en_el_cassette(self) -> None:
        extractor = fake.FakeExtractor.from_dict(
            {
                "header": {"statements": [{"currency": "ARS", "new_charges": "1000.00"}]},
                "chunks": [
                    {
                        "transactions": [
                            {
                                "posted_date": "15/03",
                                "description_raw": "COMERCIO",
                                "source_line": "15/03 COMERCIO 1.000,00",
                                "amount": "1000.00",
                                "currency": "ARS",
                                "direction": 1,
                                "kind": "charge",
                            }
                        ]
                    }
                ],
            }
        )

        header = await extractor.extract_statement_header(
            text="", hints=fake.HeaderHints(), prompt_version="v1"
        )
        chunk = await extractor.extract_transactions(
            text_chunk="", chunk_index=0, header=header.data, prompt_version="v1"
        )

        assert header.data.statements[0].new_charges == Decimal("1000.00")
        assert chunk.data.transactions[0].amount == Decimal("1000.00")

    async def test_un_fragmento_sin_grabar_devuelve_vacio_y_avisa(self) -> None:
        extractor = fake.FakeExtractor()

        result = await extractor.extract_transactions(
            text_chunk="", chunk_index=7, header=StatementHeaderPayload(), prompt_version="v1"
        )

        assert result.data.transactions == []
        assert result.data.warnings

    async def test_registra_las_llamadas_para_poder_afirmar_sobre_el_chunking(self) -> None:
        extractor = fake.FakeExtractor()

        await extractor.extract_transactions(
            text_chunk="abc", chunk_index=0, header=StatementHeaderPayload(), prompt_version="v1"
        )

        assert extractor.calls == [{"call": "transactions", "chunk": 0, "chars": 3}]


class TestGeminiHelpers:
    class _Response:
        def __init__(self, text: str | None, candidates: list[object] | None = None) -> None:
            self.text = text
            self.candidates = candidates or []
            self.usage_metadata = None

    def test_decodifica_el_json_de_la_respuesta(self) -> None:
        assert gemini._decode(self._Response('{"transactions": []}')) == {"transactions": []}

    def test_una_respuesta_vacia_es_un_error_legible(self) -> None:
        with pytest.raises(LLMResponseError, match="no devolvio contenido"):
            gemini._decode(self._Response(None))

    def test_un_json_truncado_no_se_propaga_como_jsondecodeerror(self) -> None:
        with pytest.raises(LLMResponseError, match="no es JSON valido"):
            gemini._decode(self._Response('{"transactions": [{"amo'))

    def test_un_array_en_la_raiz_se_rechaza(self) -> None:
        with pytest.raises(LLMResponseError, match="se esperaba un objeto"):
            gemini._decode(self._Response("[1, 2, 3]"))

    @pytest.mark.parametrize("code", [429, 500, 503, 504])
    def test_los_codigos_pasajeros_se_reintentan(self, code: int) -> None:
        error = Exception("boom")
        error.code = code  # type: ignore[attr-defined]

        assert isinstance(gemini._translate(error), LLMTransientError)

    def test_una_api_key_invalida_es_permanente(self) -> None:
        translated = gemini._translate(Exception("API key not valid"))

        assert isinstance(translated, LLMConfigError)

    def test_un_timeout_es_pasajero(self) -> None:
        assert isinstance(gemini._translate(Exception("deadline exceeded")), LLMTransientError)

    def test_el_header_se_resume_en_texto_para_el_prompt(self) -> None:
        payload = StatementHeaderPayload.model_validate(
            {
                "statements": [
                    {"currency": "ARS", "closing_date": "20/03/2026", "new_charges": "1000.00"}
                ]
            }
        )

        text = gemini._format_header(payload)

        assert "ARS" in text
        assert "20/03/2026" in text

    def test_sin_header_el_prompt_lo_dice_en_vez_de_quedar_vacio(self) -> None:
        assert "no se pudo leer" in gemini._format_header(StatementHeaderPayload())


class TestFactory:
    def test_gemini_sin_api_key_falla_claro(self) -> None:
        settings = Settings(_env_file=None, llm_provider="gemini", gemini_api_key="")

        with pytest.raises(LLMConfigError, match="GEMINI_API_KEY"):
            factory.build(settings)

    def test_mistral_avisa_que_no_esta_en_vez_de_caer_a_gemini(self) -> None:
        """Caer a otro proveedor en silencio factura contra una cuenta no elegida."""
        settings = Settings(_env_file=None, llm_provider="mistral")

        with pytest.raises(LLMConfigError, match="Mistral"):
            factory.build(settings)

    def test_el_fake_se_construye_sin_credenciales(self) -> None:
        settings = Settings(_env_file=None, llm_provider="fake")

        assert isinstance(factory.build(settings), fake.FakeExtractor)
