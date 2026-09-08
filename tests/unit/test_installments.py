"""Tests de cuotas: flujo de caja, devengado y proyeccion."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from app.domain.dates import Period
from app.domain.installments import (
    Commitment,
    Installment,
    InstallmentError,
    amount_is_plausible,
    expected_installment_amount,
    infer_purchase_period,
    purchase_date_or_estimate,
    remaining_commitment,
)


class TestInstallment:
    def test_cuotas_restantes(self) -> None:
        i = Installment(number=3, total=12, amount=Decimal("10000.00"))
        assert i.remaining_count == 9
        assert i.remaining_amount == Decimal("90000.00")
        assert not i.is_last

    def test_ultima_cuota(self) -> None:
        i = Installment(number=12, total=12, amount=Decimal("10000.00"))
        assert i.is_last
        assert i.remaining_count == 0
        assert i.remaining_amount == Decimal("0.00")

    def test_un_pago_en_una_cuota(self) -> None:
        i = Installment(number=1, total=1, amount=Decimal("5000.00"))
        assert i.is_last
        assert i.remaining_amount == Decimal("0.00")

    def test_devengado_prefiere_el_total_informado(self) -> None:
        """Con interes, `cuota * n` sobrestima el precio de lista."""
        i = Installment(
            number=3,
            total=12,
            amount=Decimal("12000.00"),  # incluye interes
            purchase_total_amount=Decimal("120000.00"),
        )
        assert i.accrued_amount == Decimal("120000.00")

    def test_devengado_estima_si_no_hay_total(self) -> None:
        i = Installment(number=3, total=12, amount=Decimal("10000.00"))
        assert i.accrued_amount == Decimal("120000.00")

    @pytest.mark.parametrize(
        ("number", "total"),
        [(0, 12), (13, 12), (-1, 12), (1, 0), (1, 121), (1, -5)],
    )
    def test_combinaciones_incoherentes(self, number: int, total: int) -> None:
        with pytest.raises(InstallmentError):
            Installment(number=number, total=total, amount=Decimal("100.00"))

    def test_monto_negativo(self) -> None:
        with pytest.raises(InstallmentError, match="negativo"):
            Installment(number=1, total=6, amount=Decimal("-100.00"))


class TestExpectedInstallmentAmount:
    def test_division_exacta(self) -> None:
        assert expected_installment_amount(Decimal("120000.00"), 12) == Decimal("10000.00")

    def test_redondea_a_centavos(self) -> None:
        assert expected_installment_amount(Decimal("100.00"), 3) == Decimal("33.33")

    def test_cero_cuotas(self) -> None:
        with pytest.raises(InstallmentError):
            expected_installment_amount(Decimal("100.00"), 0)


class TestAmountIsPlausible:
    TOTAL = Decimal("120000.00")

    def test_division_exacta(self) -> None:
        assert amount_is_plausible(
            amount=Decimal("10000.00"), purchase_total_amount=self.TOTAL, installments=12
        )

    def test_con_interes_es_valido(self) -> None:
        """Las cuotas argentinas casi nunca son la division exacta del precio."""
        assert amount_is_plausible(
            amount=Decimal("14500.00"), purchase_total_amount=self.TOTAL, installments=12
        )

    def test_el_total_leido_como_cuota_se_detecta(self) -> None:
        """El error tipico del LLM: tomar el monto de la columna equivocada.

        Sin este check, un consumo de $10.000 se registraria como $120.000 e
        inflaria el mes en un orden de magnitud.
        """
        assert not amount_is_plausible(
            amount=Decimal("120000.00"), purchase_total_amount=self.TOTAL, installments=12
        )

    def test_una_cuota_mucho_menor_no_tiene_explicacion(self) -> None:
        assert not amount_is_plausible(
            amount=Decimal("1000.00"), purchase_total_amount=self.TOTAL, installments=12
        )

    @pytest.mark.parametrize(
        ("total", "installments"),
        [(Decimal("0"), 12), (Decimal("-100"), 12), (Decimal("1000"), 0)],
    )
    def test_entradas_invalidas(self, total: Decimal, installments: int) -> None:
        assert not amount_is_plausible(
            amount=Decimal("100.00"), purchase_total_amount=total, installments=installments
        )


class TestRemainingCommitment:
    def _commitment(self, number: int, total: int = 12) -> Commitment | None:
        return remaining_commitment(
            group_key="abc123",
            currency="ARS",
            merchant_name="Tienda X",
            last_installment=Installment(number=number, total=total, amount=Decimal("10000.00")),
            last_period=Period(2026, 3),
        )

    def test_proyecta_lo_que_falta(self) -> None:
        c = self._commitment(3)
        assert c is not None
        assert c.installments_left == 9
        assert c.total_remaining == Decimal("90000.00")
        assert c.next_period == Period(2026, 4)

    def test_una_compra_terminada_no_compromete_nada(self) -> None:
        assert self._commitment(12) is None

    def test_el_cronograma_cruza_el_año(self) -> None:
        """Nueve cuotas desde abril de 2026 llegan a diciembre."""
        c = self._commitment(3)
        assert c is not None
        schedule = c.schedule()
        assert len(schedule) == 9
        assert schedule[0][0] == Period(2026, 4)
        assert schedule[-1][0] == Period(2026, 12)
        assert all(amount == Decimal("10000.00") for _, amount in schedule)

    def test_el_cronograma_pasa_al_año_siguiente(self) -> None:
        c = remaining_commitment(
            group_key="k",
            currency="ARS",
            merchant_name="Tienda",
            last_installment=Installment(number=1, total=6, amount=Decimal("5000.00")),
            last_period=Period(2026, 11),
        )
        assert c is not None
        periods = [p for p, _ in c.schedule()]
        assert periods == [
            Period(2026, 12),
            Period(2027, 1),
            Period(2027, 2),
            Period(2027, 3),
            Period(2027, 4),
        ]

    def test_la_suma_del_cronograma_es_el_total(self) -> None:
        c = self._commitment(3)
        assert c is not None
        assert sum(a for _, a in c.schedule()) == c.total_remaining


class TestInferPurchasePeriod:
    def test_la_primera_cuota_cae_el_mes_de_la_compra(self) -> None:
        i = Installment(number=1, total=12, amount=Decimal("100.00"))
        assert infer_purchase_period(i, Period(2026, 3)) == Period(2026, 3)

    def test_la_cuota_n_cae_n_menos_1_meses_despues(self) -> None:
        i = Installment(number=4, total=12, amount=Decimal("100.00"))
        assert infer_purchase_period(i, Period(2026, 3)) == Period(2025, 12)

    def test_cruza_el_año_hacia_atras(self) -> None:
        i = Installment(number=12, total=12, amount=Decimal("100.00"))
        assert infer_purchase_period(i, Period(2026, 6)) == Period(2025, 7)


class TestPurchaseDateOrEstimate:
    INSTALLMENT = Installment(number=4, total=12, amount=Decimal("100.00"))

    def test_prefiere_la_fecha_informada(self) -> None:
        real = date(2025, 12, 18)
        assert purchase_date_or_estimate(self.INSTALLMENT, Period(2026, 3), real) == real

    def test_estima_el_primer_dia_del_mes_inferido(self) -> None:
        """Aproximacion deliberada: sirve para agrupar, no pretende ser exacta."""
        assert purchase_date_or_estimate(self.INSTALLMENT, Period(2026, 3), None) == date(
            2025, 12, 1
        )
