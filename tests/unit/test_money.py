"""Tests de parseo y aritmetica de montos.

Los casos vienen de los formatos que realmente emiten los resumenes argentinos.
Un error aca no se ve: produce numeros plausibles y equivocados.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.domain.money import (
    AmountParseError,
    convert,
    format_ars,
    parse_amount,
    quantize,
    signed,
    within_tolerance,
)


class TestParseAmount:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            # ─── es-AR: punto de miles, coma decimal ─────────────────────────
            ("1.234,56", "1234.56"),
            ("1.234.567,89", "1234567.89"),
            ("0,50", "0.50"),
            (",50", "0.50"),
            ("999.999.999,99", "999999999.99"),
            # ─── en-US: coma de miles, punto decimal (algunas fintechs) ──────
            ("1,234.56", "1234.56"),
            ("1,234,567.89", "1234567.89"),
            # ─── sin separador de miles ─────────────────────────────────────
            ("1234.56", "1234.56"),
            ("1234,56", "1234.56"),
            ("1234", "1234.00"),
            ("0", "0.00"),
            # ─── separador de miles sin decimales ───────────────────────────
            # "1.234" son mil doscientos treinta y cuatro, NO 1.234.
            # Interpretarlo como float da un error de tres ordenes de magnitud.
            ("1.234", "1234.00"),
            ("1,234", "1234.00"),
            ("12.345.678", "12345678.00"),
            # ─── con simbolos y espacios ────────────────────────────────────
            ("$ 1.234,56", "1234.56"),
            ("  1.234,56  ", "1234.56"),
            ("ARS 1.234,56", "1234.56"),
            ("US$ 99,90", "99.90"),
            # ─── un decimal ─────────────────────────────────────────────────
            ("1234,5", "1234.50"),
        ],
    )
    def test_formatos_validos(self, raw: str, expected: str) -> None:
        assert parse_amount(raw) == Decimal(expected)

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            # Los emisores marcan credito de formas distintas y ninguna es estandar.
            ("1.234,56 CR", "-1234.56"),
            ("1.234,56CR", "-1234.56"),
            ("1.234,56 H", "-1234.56"),
            ("1.234,56 HABER", "-1234.56"),
            ("-1.234,56", "-1234.56"),
            ("(1.234,56)", "-1234.56"),
        ],
    )
    def test_marcas_de_credito(self, raw: str, expected: str) -> None:
        assert parse_amount(raw) == Decimal(expected)

    @pytest.mark.parametrize("raw", ["", "   ", "abc", "$", "-", "1.2345", "1,23456"])
    def test_entradas_invalidas(self, raw: str) -> None:
        with pytest.raises(AmountParseError):
            parse_amount(raw)

    def test_decimal_pasa_intacto(self) -> None:
        assert parse_amount(Decimal("1234.56")) == Decimal("1234.56")

    def test_int_se_convierte(self) -> None:
        assert parse_amount(1234) == Decimal("1234.00")

    def test_float_no_arrastra_el_error_binario(self) -> None:
        """Aceptar float es una conveniencia, pero no debe introducir basura."""
        assert parse_amount(0.1) == Decimal("0.10")
        assert parse_amount(1234.56) == Decimal("1234.56")


class TestQuantize:
    def test_redondea_para_arriba_en_el_medio(self) -> None:
        """ROUND_HALF_UP, no el default de Python.

        El default de Decimal es ROUND_HALF_EVEN ("redondeo del banquero"), que
        daria 0.12 para 0.125. Un resumen de tarjeta redondea a 0.13, y usar el otro
        criterio hace fallar el cuadre por un centavo.
        """
        assert quantize(Decimal("0.125")) == Decimal("0.13")
        assert quantize(Decimal("0.135")) == Decimal("0.14")
        assert quantize(Decimal("2.675")) == Decimal("2.68")

    def test_no_pierde_precision_sumando(self) -> None:
        """El motivo de existir de todo este modulo."""
        total = sum(
            (parse_amount("0,10") for _ in range(10)),
            start=Decimal("0"),
        )
        assert total == Decimal("1.00")


class TestSigned:
    def test_direccion_positiva_aumenta_la_deuda(self) -> None:
        assert signed(Decimal("100.00"), 1) == Decimal("100.00")

    def test_direccion_negativa_es_un_pago(self) -> None:
        assert signed(Decimal("100.00"), -1) == Decimal("-100.00")

    @pytest.mark.parametrize("direction", [0, 2, -2, 100])
    def test_direccion_invalida(self, direction: int) -> None:
        with pytest.raises(ValueError, match="direction"):
            signed(Decimal("100.00"), direction)


class TestConvert:
    def test_convierte_usd_a_ars(self) -> None:
        assert convert(Decimal("100.00"), Decimal("1050.500000")) == Decimal("105050.00")

    def test_redondea_a_centavos(self) -> None:
        assert convert(Decimal("1.00"), Decimal("1050.555")) == Decimal("1050.56")

    @pytest.mark.parametrize("rate", ["0", "-1"])
    def test_cotizacion_invalida(self, rate: str) -> None:
        with pytest.raises(ValueError, match="positiva"):
            convert(Decimal("100.00"), Decimal(rate))


class TestWithinTolerance:
    PCT = Decimal("0.005")
    FLOOR = Decimal("50.00")

    def test_el_piso_absorbe_diferencias_chicas(self) -> None:
        """En un resumen chico manda el piso absoluto, no el porcentaje."""
        assert within_tolerance(
            Decimal("1000.00"), Decimal("1040.00"), pct=self.PCT, floor=self.FLOOR
        )

    def test_rechaza_una_diferencia_mayor_al_piso(self) -> None:
        assert not within_tolerance(
            Decimal("1000.00"), Decimal("1051.00"), pct=self.PCT, floor=self.FLOOR
        )

    def test_el_porcentaje_manda_en_resumenes_grandes(self) -> None:
        # 0.5% de 1.000.000 = 5.000, que es mucho mas que el piso de 50.
        assert within_tolerance(
            Decimal("1000000.00"), Decimal("996000.00"), pct=self.PCT, floor=self.FLOOR
        )
        assert not within_tolerance(
            Decimal("1000000.00"), Decimal("994000.00"), pct=self.PCT, floor=self.FLOOR
        )

    def test_cuadre_exacto(self) -> None:
        assert within_tolerance(
            Decimal("1234.56"), Decimal("1234.56"), pct=Decimal("0"), floor=Decimal("0")
        )


class TestFormatArs:
    @pytest.mark.parametrize(
        ("amount", "expected"),
        [
            ("1234.56", "1.234,56"),
            ("1234567.89", "1.234.567,89"),
            ("0.50", "0,50"),
            ("100.00", "100,00"),
            ("-1234.56", "-1.234,56"),
            ("999.99", "999,99"),
            ("1000.00", "1.000,00"),
        ],
    )
    def test_formatea_en_es_ar(self, amount: str, expected: str) -> None:
        assert format_ars(Decimal(amount)) == expected

    def test_ida_y_vuelta(self) -> None:
        """Lo que se formatea se tiene que poder volver a parsear."""
        for raw in ("1234.56", "1234567.89", "0.50", "-9876.54"):
            value = Decimal(raw)
            assert parse_amount(format_ars(value)) == value
