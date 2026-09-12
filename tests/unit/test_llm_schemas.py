"""El borde entre lo que dijo el modelo y lo que el sistema guarda.

Estos tests cubren la clase de error mas cara del proyecto: la que no se ve. Un
monto mal interpretado no rompe nada, no tira una excepcion y no aparece en
ningun log — simplemente el mes cierra mal. Por eso se testea cada camino de
conversion con el formato concreto que mandan los emisores argentinos.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.domain.models import (
    NormalizationError,
    ParsedStatementHeader,
    normalize_all,
    normalize_transaction,
)
from app.schemas.llm import (
    LLMStatementHeader,
    LLMTransaction,
    StatementHeaderPayload,
    TransactionsPayload,
)

CLOSING = date(2026, 3, 20)


def _tx(**overrides: object) -> LLMTransaction:
    data: dict[str, object] = {
        "posted_date": "15/03",
        "description_raw": "SUPERMERCADO COTO",
        "source_line": "15/03 SUPERMERCADO COTO 12.345,67",
        "amount": "12345.67",
        "currency": "ARS",
        "direction": 1,
        "kind": "charge",
    }
    data.update(overrides)
    return LLMTransaction.model_validate(data)


class TestMontos:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("1234.56", Decimal("1234.56")),
            ("1.234,56", Decimal("1234.56")),  # es-AR, por si ignora el formato pedido
            ("1,234.56", Decimal("1234.56")),
            ("0.00", Decimal("0.00")),
            ("999999999.99", Decimal("999999999.99")),
        ],
    )
    def test_llegan_como_decimal_exacto(self, raw: str, expected: Decimal) -> None:
        assert _tx(amount=raw).amount == expected

    def test_nunca_hay_un_float_en_el_camino(self) -> None:
        assert isinstance(_tx(amount="0.10").amount, Decimal)
        # El sintoma clasico de haber pasado por float.
        total = _tx(amount="0.10").amount + _tx(amount="0.20").amount
        assert total == Decimal("0.30")

    def test_un_monto_negativo_se_guarda_positivo(self) -> None:
        """La base exige `amount >= 0`: el sentido vive en `direction`."""
        assert _tx(amount="-500.00", direction=-1).amount == Decimal("500.00")

    def test_un_monto_ilegible_es_un_error_de_validacion(self) -> None:
        with pytest.raises(ValidationError):
            _tx(amount="doce mil")


class TestDirection:
    def test_acepta_el_entero(self) -> None:
        assert _tx(direction=-1).direction == -1

    def test_acepta_el_string_que_manda_el_schema_de_gemini(self) -> None:
        assert _tx(direction="-1").direction == -1

    def test_rechaza_cualquier_otro_valor(self) -> None:
        with pytest.raises(ValidationError):
            _tx(direction=0)


class TestCuotas:
    def test_se_conservan_cuando_son_coherentes(self) -> None:
        tx = _tx(installment_number=3, installment_total=12)

        assert tx.has_installments
        assert (tx.installment_number, tx.installment_total) == (3, 12)

    @pytest.mark.parametrize(("number", "total"), [(0, 12), (3, 0), (3, 999), (-1, 6)])
    def test_fuera_de_rango_se_descartan_sin_perder_la_transaccion(
        self, number: int, total: int
    ) -> None:
        tx = _tx(installment_number=number, installment_total=total)

        assert not tx.has_installments
        assert tx.amount == Decimal("12345.67")

    def test_una_cuota_mayor_al_total_no_cuenta_como_cuota(self) -> None:
        assert not _tx(installment_number=13, installment_total=12).has_installments


class TestCamposOpcionales:
    @pytest.mark.parametrize("blank", ["", "  ", "null", "N/A", "-"])
    def test_los_rellenos_del_modelo_se_vuelven_none(self, blank: str) -> None:
        assert _tx(transaction_date=blank).transaction_date is None

    def test_campos_de_mas_se_ignoran(self) -> None:
        tx = _tx(inventado="el modelo agrego esto")

        assert tx.description_raw == "SUPERMERCADO COTO"

    def test_source_line_es_obligatoria(self) -> None:
        with pytest.raises(ValidationError):
            _tx(source_line="")


class TestHeader:
    def test_card_last4_se_queda_con_los_ultimos_cuatro_digitos(self) -> None:
        header = LLMStatementHeader.model_validate(
            {"currency": "ARS", "card_last4": "**** **** **** 4821"}
        )

        assert header.card_last4 == "4821"

    def test_card_last4_invalido_queda_en_none(self) -> None:
        header = LLMStatementHeader.model_validate({"currency": "ARS", "card_last4": "XX"})

        assert header.card_last4 is None

    def test_busca_por_moneda(self) -> None:
        payload = StatementHeaderPayload.model_validate(
            {"statements": [{"currency": "ARS"}, {"currency": "USD", "total_due": "120.00"}]}
        )

        usd = payload.for_currency("usd")
        assert usd is not None
        assert usd.total_due == Decimal("120.00")
        assert payload.for_currency("EUR") is None


class TestNormalizacion:
    def test_infiere_el_año_con_la_fecha_de_cierre(self) -> None:
        parsed = normalize_transaction(_tx(posted_date="15/03"), closing=CLOSING)

        assert parsed.posted_date == date(2026, 3, 15)

    def test_el_cruce_diciembre_enero_no_manda_el_gasto_al_futuro(self) -> None:
        """El error que hace desaparecer un gasto durante doce meses."""
        parsed = normalize_transaction(_tx(posted_date="20/12"), closing=date(2026, 1, 5))

        assert parsed.posted_date == date(2025, 12, 20)

    def test_sin_cierre_y_sin_año_la_transaccion_no_se_inventa(self) -> None:
        with pytest.raises(NormalizationError):
            normalize_transaction(_tx(posted_date="15/03"), closing=None)

    def test_sin_cierre_pero_con_fecha_completa_funciona(self) -> None:
        parsed = normalize_transaction(_tx(posted_date="2026-03-15"), closing=None)

        assert parsed.posted_date == date(2026, 3, 15)

    def test_la_clave_de_dedupe_es_estable_entre_corridas(self) -> None:
        a = normalize_transaction(_tx(), closing=CLOSING)
        b = normalize_transaction(_tx(), closing=CLOSING)

        assert a.dedupe_key == b.dedupe_key

    def test_la_clave_cambia_si_cambia_el_monto(self) -> None:
        a = normalize_transaction(_tx(), closing=CLOSING)
        b = normalize_transaction(_tx(amount="12345.68"), closing=CLOSING)

        assert a.dedupe_key != b.dedupe_key

    def test_el_monto_con_signo_sale_de_direction(self) -> None:
        cargo = normalize_transaction(_tx(direction=1), closing=CLOSING)
        pago = normalize_transaction(_tx(direction=-1), closing=CLOSING)

        assert cargo.signed_amount == Decimal("12345.67")
        assert pago.signed_amount == Decimal("-12345.67")

    def test_las_cuotas_reciben_clave_de_grupo(self) -> None:
        parsed = normalize_transaction(
            _tx(installment_number=3, installment_total=12, purchase_total_amount="120000.00"),
            closing=CLOSING,
        )

        assert parsed.installment_group_key is not None
        assert parsed.is_installment

    def test_una_transaccion_sin_cuotas_no_tiene_clave_de_grupo(self) -> None:
        assert normalize_transaction(_tx(), closing=CLOSING).installment_group_key is None

    def test_una_fecha_secundaria_ilegible_no_pierde_la_transaccion(self) -> None:
        parsed = normalize_transaction(_tx(transaction_date="el martes"), closing=CLOSING)

        assert parsed.transaction_date is None
        assert parsed.amount == Decimal("12345.67")


class TestNormalizeAll:
    def test_una_linea_rota_no_se_lleva_las_demas(self) -> None:
        payload = TransactionsPayload.model_validate(
            {
                "transactions": [
                    _tx().model_dump(),
                    _tx(posted_date="fecha imposible").model_dump(),
                    _tx(description_raw="OTRO COMERCIO").model_dump(),
                ]
            }
        )

        parsed, discarded = normalize_all(list(payload.transactions), closing=CLOSING)

        assert len(parsed) == 2
        assert len(discarded) == 1
        assert discarded[0].index == 1
        assert discarded[0].source_line


class TestParsedStatementHeader:
    def test_el_periodo_sale_del_cierre(self) -> None:
        header = ParsedStatementHeader.from_llm(
            LLMStatementHeader.model_validate({"currency": "ARS", "closing_date": "2026-01-05"})
        )

        assert header.period == (2026, 1)

    def test_sin_cierre_usa_el_fallback(self) -> None:
        header = ParsedStatementHeader.from_llm(
            LLMStatementHeader.model_validate({"currency": "ARS"}),
            fallback_closing=date(2026, 3, 20),
        )

        assert header.closing_date == date(2026, 3, 20)

    def test_sin_cierre_ni_fallback_no_hay_periodo(self) -> None:
        header = ParsedStatementHeader.from_llm(
            LLMStatementHeader.model_validate({"currency": "ARS"})
        )

        assert header.period is None


class TestFechaDeCuotas:
    """Los dos casos reales del corpus, con sus números.

    Galicia y BNA imprimen en la columna de fecha de una cuota la fecha de la
    COMPRA ORIGINAL. Sin corregirlo, una cuota que se paga en agosto aparece como
    gasto de marzo o de mayo, y el dashboard —que es de flujo de caja— muestra el
    mes equivocado. Las dos cuotas del corpus real tenían el problema.
    """

    CIERRE = date(2026, 8, 27)

    def test_puma_abasto_cuota_6_de_6(self) -> None:
        parsed = normalize_transaction(
            _tx(
                posted_date="21/03/2026",
                description_raw="* PUMA ABASTO 06/06 584360",
                installment_number=6,
                installment_total=6,
            ),
            closing=self.CIERRE,
        )

        assert parsed.posted_date == self.CIERRE
        assert parsed.purchase_date == date(2026, 3, 21)

    def test_fravega_cuota_4_de_12(self) -> None:
        parsed = normalize_transaction(
            _tx(
                posted_date="13/05/2026",
                description_raw="WWW.FRAVEGA.COM-BNA C.04/12",
                installment_number=4,
                installment_total=12,
            ),
            closing=self.CIERRE,
        )

        assert parsed.posted_date == self.CIERRE
        assert parsed.purchase_date == date(2026, 5, 13)

    def test_una_cuota_con_la_fecha_del_resumen_no_se_toca(self) -> None:
        """El emisor que sí imprime la fecha correcta queda intacto."""
        parsed = normalize_transaction(
            _tx(posted_date="15/08/2026", installment_number=4, installment_total=12),
            closing=self.CIERRE,
        )

        assert parsed.posted_date == date(2026, 8, 15)
        assert parsed.purchase_date is None

    def test_una_fecha_lejana_que_no_cae_en_el_mes_de_la_compra_no_se_toca(self) -> None:
        """Estar lejos del cierre no alcanza: podría ser un año mal inferido.

        La condición es doble a propósito. Acá la cuota 4/12 implicaría una compra
        en mayo, pero la fecha dice enero: no coincide, así que no se mueve y el
        check de coherencia de fechas la marca para revisión manual.
        """
        parsed = normalize_transaction(
            _tx(posted_date="10/01/2026", installment_number=4, installment_total=12),
            closing=self.CIERRE,
        )

        assert parsed.posted_date == date(2026, 1, 10)
        assert parsed.purchase_date is None

    def test_la_primera_cuota_nunca_se_mueve(self) -> None:
        parsed = normalize_transaction(
            _tx(posted_date="15/08/2026", installment_number=1, installment_total=12),
            closing=self.CIERRE,
        )

        assert parsed.posted_date == date(2026, 8, 15)

    def test_un_consumo_sin_cuotas_no_se_toca(self) -> None:
        parsed = normalize_transaction(_tx(posted_date="21/03/2026"), closing=self.CIERRE)

        assert parsed.posted_date == date(2026, 3, 21)

    def test_si_el_modelo_ya_informo_la_fecha_de_compra_esa_manda(self) -> None:
        parsed = normalize_transaction(
            _tx(
                posted_date="13/05/2026",
                purchase_date="02/05/2026",
                installment_number=4,
                installment_total=12,
            ),
            closing=self.CIERRE,
        )

        assert parsed.posted_date == self.CIERRE
        assert parsed.purchase_date == date(2026, 5, 2)
