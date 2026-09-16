"""Deteccion de suscripciones: los casos que decidieron el criterio."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from app.domain import subscriptions
from app.domain.subscriptions import Occurrence


def occ(
    month: int,
    amount: str,
    *,
    year: int = 2026,
    day: int = 5,
    kind: str = "charge",
    currency: str = "ARS",
    installment_total: int | None = None,
    direction: int = 1,
) -> Occurrence:
    return Occurrence(
        transaction_id=f"tx-{year}-{month:02d}-{amount}",
        posted_date=date(year, month, day),
        amount=Decimal(amount),
        currency=currency,
        kind=kind,
        installment_total=installment_total,
        direction=direction,
    )


def detect(*occurrences: Occurrence) -> subscriptions.SubscriptionCandidate | None:
    return subscriptions.detect(label="Spotify", merchant_id="m1", occurrences=occurrences)


# ─── El caso feliz ───────────────────────────────────────────────────────────
def test_tres_meses_seguidos_al_mismo_precio_es_suscripcion() -> None:
    found = detect(occ(1, "4999.00"), occ(2, "4999.00"), occ(3, "4999.00"))

    assert found is not None
    assert found.cadence == "monthly"
    assert found.currency == "ARS"
    assert found.first_seen == date(2026, 1, 5)
    assert found.last_seen == date(2026, 3, 5)
    assert found.occurrences == 3


def test_el_monto_nominal_es_el_ultimo_y_no_el_promedio() -> None:
    """Lo que importa es lo que se va a pagar el mes que viene, no lo que se pago."""
    found = detect(occ(1, "4000.00"), occ(2, "4600.00"), occ(3, "5200.00"))

    assert found is not None
    assert found.nominal_amount == Decimal("5200.00")


def test_el_orden_de_entrada_no_importa() -> None:
    found = detect(occ(3, "4999.00"), occ(1, "4999.00"), occ(2, "4999.00"))

    assert found is not None
    assert found.first_seen == date(2026, 1, 5)


# ─── El limite del criterio ──────────────────────────────────────────────────
def test_dos_apariciones_no_alcanzan() -> None:
    assert detect(occ(1, "4999.00"), occ(2, "4999.00")) is None


def test_un_mes_salteado_se_tolera() -> None:
    """El cargo cayo despues del cierre y entro en el resumen siguiente."""
    found = detect(occ(1, "4999.00"), occ(3, "4999.00"), occ(4, "4999.00"))

    assert found is not None
    assert found.occurrences == 3


def test_dos_meses_salteados_cortan_la_serie() -> None:
    assert detect(occ(1, "4999.00"), occ(4, "4999.00"), occ(7, "4999.00")) is None


def test_dos_cargos_en_el_mismo_mes_cuentan_una_vez() -> None:
    """Una compra suelta en el mes de la suscripcion no invalida la serie."""
    found = detect(
        occ(1, "4999.00"),
        occ(2, "4999.00"),
        occ(2, "5100.00", day=20),
        occ(3, "4999.00"),
    )

    assert found is not None
    assert found.occurrences == 3


# ─── Monto ───────────────────────────────────────────────────────────────────
def test_un_aumento_de_precio_parejo_sigue_siendo_la_misma_suscripcion() -> None:
    """Cada paso entra en la tolerancia aunque el total duplique: es inflacion."""
    found = detect(occ(1, "4000.00"), occ(2, "5000.00"), occ(3, "6200.00"), occ(4, "7700.00"))

    assert found is not None
    assert found.occurrences == 4


def test_un_salto_de_monto_fuera_de_tolerancia_descarta() -> None:
    assert detect(occ(1, "4000.00"), occ(2, "12000.00"), occ(3, "4100.00")) is None


def test_justo_en_el_borde_de_la_tolerancia_cuenta() -> None:
    borde = Decimal("1000") * (1 + subscriptions.AMOUNT_STEP_TOLERANCE)
    found = detect(occ(1, "1000.00"), occ(2, str(borde)), occ(3, str(borde)))

    assert found is not None


def test_un_peso_mas_que_el_borde_no_cuenta() -> None:
    pasado = Decimal("1000") * (1 + subscriptions.AMOUNT_STEP_TOLERANCE) + 1
    assert detect(occ(1, "1000.00"), occ(2, str(pasado)), occ(3, str(pasado))) is None


def test_monto_cero_no_es_suscripcion() -> None:
    assert detect(occ(1, "0.00"), occ(2, "0.00"), occ(3, "0.00")) is None


# ─── Los falsos positivos que el criterio existe para evitar ─────────────────
def test_las_cuotas_no_son_una_suscripcion() -> None:
    """Una compra en 12 cuotas es identica a una suscripcion salvo por este campo."""
    assert (
        detect(
            occ(1, "9000.00", installment_total=12),
            occ(2, "9000.00", installment_total=12),
            occ(3, "9000.00", installment_total=12),
        )
        is None
    )


@pytest.mark.parametrize("kind", sorted(subscriptions.EXCLUDED_KINDS))
def test_los_cargos_del_banco_no_son_suscripciones(kind: str) -> None:
    assert (
        detect(
            occ(1, "800.00", kind=kind), occ(2, "820.00", kind=kind), occ(3, "840.00", kind=kind)
        )
        is None
    )


def test_los_creditos_no_son_suscripciones() -> None:
    assert (
        detect(
            occ(1, "500.00", direction=-1),
            occ(2, "500.00", direction=-1),
            occ(3, "500.00", direction=-1),
        )
        is None
    )


def test_no_se_mezclan_monedas() -> None:
    """Tres cargos en pesos y tres en dolares son dos series, no una de seis."""
    found = detect(
        occ(1, "1000.00"),
        occ(2, "1000.00"),
        occ(1, "10.00", currency="USD"),
        occ(2, "10.00", currency="USD"),
        occ(3, "10.00", currency="USD"),
    )

    assert found is not None
    assert found.currency == "USD"
    assert found.occurrences == 3


def test_una_compra_suelta_el_mismo_mes_no_rompe_la_serie() -> None:
    """El representante del mes es el mas parecido al mes anterior, no el ultimo por fecha.

    Si el comercio ademas vende otra cosa (un gimnasio con indumentaria, un
    supermercado con plan) y esa compra cae despues del cargo de la
    suscripcion en el mismo mes, elegir "la ultima del mes" la convertiria en
    el representante, rompería la tolerancia de precio contra el mes anterior
    y tiraria abajo la deteccion de los otros meses, que si son consistentes.
    """
    found = detect(
        occ(1, "4999.00"),
        occ(2, "4999.00"),
        occ(3, "4999.00", day=3),
        occ(3, "35000.00", day=20),  # compra suelta, mas tarde en el mismo mes
        occ(4, "4999.00"),
        occ(5, "4999.00"),
    )

    assert found is not None
    assert found.occurrences == 5
    assert found.first_seen == date(2026, 1, 5)
    assert found.last_seen == date(2026, 5, 5)
    assert found.nominal_amount == Decimal("4999.00")


def test_un_mes_atipico_al_inicio_no_mata_la_racha_posterior() -> None:
    """Un mes fuera de tolerancia corta la racha en ese punto, no toda la serie."""
    found = detect(
        occ(1, "12000.00"),  # no es la suscripcion: un mes suelto, aislado
        occ(2, "4999.00"),
        occ(3, "4999.00"),
        occ(4, "4999.00"),
    )

    assert found is not None
    assert found.occurrences == 3
    assert found.first_seen == date(2026, 2, 5)


# ─── detect_all ──────────────────────────────────────────────────────────────
def test_detect_all_devuelve_solo_las_series_que_califican() -> None:
    found = subscriptions.detect_all(
        [
            ("m1", "Spotify", [occ(1, "4999.00"), occ(2, "4999.00"), occ(3, "4999.00")]),
            ("m2", "Coto", [occ(1, "12000.00"), occ(2, "80000.00")]),
        ]
    )

    assert [candidate.merchant_id for candidate in found] == ["m1"]
    assert found[0].label == "Spotify"
