"""Tests de fechas. El caso que importa es el cruce diciembre/enero."""

from __future__ import annotations

from datetime import date

import pytest

from app.domain.dates import (
    DateParseError,
    Period,
    infer_year,
    is_plausible_for_statement,
    parse_date,
    period_from_closing,
)


class TestPeriod:
    def test_primer_y_ultimo_dia(self) -> None:
        p = Period(2026, 2)
        assert p.first_day == date(2026, 2, 1)
        assert p.last_day == date(2026, 2, 28)

    def test_febrero_bisiesto(self) -> None:
        assert Period(2028, 2).last_day == date(2028, 2, 29)

    @pytest.mark.parametrize(
        ("start", "months", "expected"),
        [
            ((2026, 3), 1, (2026, 4)),
            ((2026, 12), 1, (2027, 1)),  # cruce de año hacia adelante
            ((2026, 1), -1, (2025, 12)),  # y hacia atras
            ((2026, 3), 12, (2027, 3)),
            ((2026, 6), -11, (2025, 7)),
            ((2026, 1), -12, (2025, 1)),
        ],
    )
    def test_shift(self, start: tuple[int, int], months: int, expected: tuple[int, int]) -> None:
        assert Period(*start).shift(months) == Period(*expected)

    def test_str(self) -> None:
        assert str(Period(2026, 3)) == "2026-03"

    @pytest.mark.parametrize(("year", "month"), [(1999, 1), (2101, 1), (2026, 0), (2026, 13)])
    def test_rangos_invalidos(self, year: int, month: int) -> None:
        with pytest.raises(ValueError, match="rango"):
            Period(year, month)


class TestPeriodFromClosing:
    def test_usa_el_mes_del_cierre(self) -> None:
        assert period_from_closing(date(2026, 3, 28)) == Period(2026, 3)

    def test_un_cierre_a_principio_de_enero_es_el_resumen_de_enero(self) -> None:
        """Aunque casi todos sus consumos sean de diciembre.

        El criterio es flujo de caja: el mes en que se paga.
        """
        assert period_from_closing(date(2026, 1, 5)) == Period(2026, 1)


class TestInferYear:
    def test_caso_normal_mismo_mes(self) -> None:
        assert infer_year(15, 3, closing=date(2026, 3, 28)) == 2026

    def test_mes_anterior_al_cierre(self) -> None:
        assert infer_year(20, 2, closing=date(2026, 3, 10)) == 2026

    def test_cruce_de_diciembre_a_enero(self) -> None:
        """El bug clasico: sin esto, un consumo de diciembre va a un año al futuro.

        Un resumen que cierra el 5/1/2026 lista consumos del 20/12. Si se les
        asignara el año del cierre, irian a diciembre de 2026 y desaparecerian del
        dashboard hasta el año siguiente.
        """
        assert infer_year(20, 12, closing=date(2026, 1, 5)) == 2025
        assert infer_year(28, 12, closing=date(2026, 1, 10)) == 2025
        assert infer_year(31, 12, closing=date(2026, 1, 3)) == 2025

    def test_enero_con_cierre_en_enero_es_el_mismo_año(self) -> None:
        assert infer_year(3, 1, closing=date(2026, 1, 20)) == 2026

    def test_29_de_febrero_elige_el_año_bisiesto(self) -> None:
        """2027 no es bisiesto; 2028 si. La fecha solo existe en uno."""
        assert infer_year(29, 2, closing=date(2028, 3, 5)) == 2028

    def test_fuera_de_la_ventana_devuelve_el_mas_cercano(self) -> None:
        """No se descarta la transaccion: se persiste y el validador la marca.

        Descartarla perderia un gasto en silencio, que es peor que mostrarlo con una
        fecha sospechosa y pedir revision.
        """
        assert infer_year(15, 6, closing=date(2026, 1, 10)) in (2025, 2026)


class TestParseDate:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("2026-03-15", date(2026, 3, 15)),
            ("2026-3-5", date(2026, 3, 5)),
            ("15/03/2026", date(2026, 3, 15)),
            ("15-03-2026", date(2026, 3, 15)),
            ("15.03.2026", date(2026, 3, 15)),
            ("15/03/26", date(2026, 3, 15)),
            ("5/3/26", date(2026, 3, 5)),
        ],
    )
    def test_con_año_explicito(self, raw: str, expected: date) -> None:
        assert parse_date(raw) == expected

    def test_sin_año_usa_el_cierre(self) -> None:
        assert parse_date("15/03", closing=date(2026, 3, 28)) == date(2026, 3, 15)
        assert parse_date("20/12", closing=date(2026, 1, 5)) == date(2025, 12, 20)

    def test_sin_año_y_sin_cierre_falla_claro(self) -> None:
        with pytest.raises(DateParseError, match="año"):
            parse_date("15/03")

    @pytest.mark.parametrize("raw", ["", "   ", "abc", "32/13/2026", "2026/03/15/01", "15"])
    def test_entradas_invalidas(self, raw: str) -> None:
        with pytest.raises(DateParseError):
            parse_date(raw)

    def test_el_dia_va_primero_en_formato_es_ar(self) -> None:
        """`03/04` en un resumen argentino es 3 de abril, no 4 de marzo."""
        assert parse_date("03/04/2026") == date(2026, 4, 3)


class TestIsPlausibleForStatement:
    CLOSING = date(2026, 3, 20)

    @pytest.mark.parametrize(
        "value",
        [date(2026, 3, 20), date(2026, 3, 15), date(2026, 2, 20), date(2026, 3, 25)],
    )
    def test_fechas_razonables(self, value: date) -> None:
        assert is_plausible_for_statement(value, closing=self.CLOSING)

    @pytest.mark.parametrize(
        "value",
        [
            date(2026, 1, 1),  # demasiado vieja
            date(2026, 4, 15),  # muy posterior al cierre
            date(2027, 3, 20),  # año mal inferido
            date(2025, 3, 20),
        ],
    )
    def test_fechas_sospechosas(self, value: date) -> None:
        assert not is_plausible_for_statement(value, closing=self.CLOSING)


class TestMesesEnTexto:
    """Galicia emite las líneas de consumo como `24-Ago-26`.

    No es un caso exótico: sin esto, el modelo copia la fecha correctamente —que
    es exactamente lo que se le pide— y la normalización descarta la transacción
    entera. Un resumen real terminó procesándose "sin errores" y con cero
    movimientos por este motivo.
    """

    @pytest.mark.parametrize(
        ("raw", "esperado"),
        [
            ("06-Ago-26", date(2026, 8, 6)),
            ("24-Ago-26", date(2026, 8, 24)),
            ("24-AGO-2026", date(2026, 8, 24)),
            ("24 ago 2026", date(2026, 8, 24)),
            ("1/dic/25", date(2025, 12, 1)),
            ("15.Mar.26", date(2026, 3, 15)),
            ("3-Set-26", date(2026, 9, 3)),  # en Argentina se abrevia así
            ("3-Sep-26", date(2026, 9, 3)),
            ("9-diciembre-2026", date(2026, 12, 9)),
        ],
    )
    def test_con_año(self, raw: str, esperado: date) -> None:
        assert parse_date(raw) == esperado

    def test_sin_año_se_infiere_con_el_cierre(self) -> None:
        assert parse_date("24-Ago", closing=date(2026, 9, 9)) == date(2026, 8, 24)

    def test_el_cruce_de_año_tambien_funciona_con_mes_en_texto(self) -> None:
        assert parse_date("20-Dic", closing=date(2026, 1, 5)) == date(2025, 12, 20)

    def test_una_palabra_que_no_es_un_mes_no_se_acepta(self) -> None:
        with pytest.raises(DateParseError):
            parse_date("24-Xyz-26")

    def test_el_punto_de_la_abreviatura_no_molesta(self) -> None:
        assert parse_date("06-ago.-26") == date(2026, 8, 6)
