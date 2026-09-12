"""Los ocho checks que deciden si una extraccion se puede creer.

Cada test arma el escenario del error que el check existe para atrapar. Si un
check se rompe, el modo de falla no es una excepcion: es un mes que cierra mal y
nadie se entera. Por eso se testean los ocho, incluidos los que parecen obvios.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from app.domain import validation
from app.domain.dedupe import dedupe_key
from app.domain.models import Discarded, ParsedStatementHeader, ParsedTransaction

CLOSING = date(2026, 3, 20)


def tx(
    *,
    amount: str = "1000.00",
    direction: int = 1,
    currency: str = "ARS",
    posted: date = date(2026, 3, 15),
    description: str = "COMERCIO",
    source_line: str | None = None,
    page: int | None = None,
    installment_number: int | None = None,
    installment_total: int | None = None,
    purchase_total: str | None = None,
) -> ParsedTransaction:
    value = Decimal(amount)
    return ParsedTransaction(
        posted_date=posted,
        description_raw=description,
        amount=value,
        currency=currency,
        direction=direction,
        kind="charge",
        source_line=source_line
        if source_line is not None
        else f"{posted:%d/%m} {description} {amount}",
        source_page=page,
        installment_number=installment_number,
        installment_total=installment_total,
        purchase_total_amount=Decimal(purchase_total) if purchase_total else None,
        dedupe_key=dedupe_key(
            description=description,
            posted_date=posted,
            amount=value,
            currency=currency,
            installment_number=installment_number,
            installment_total=installment_total,
        ),
    )


def header(
    *,
    currency: str = "ARS",
    new_charges: str | None = "1000.00",
    previous: str | None = None,
    payments: str | None = None,
    total_due: str | None = None,
    closing: date | None = CLOSING,
) -> ParsedStatementHeader:
    return ParsedStatementHeader(
        currency=currency,
        closing_date=closing,
        new_charges=Decimal(new_charges) if new_charges else None,
        previous_balance=Decimal(previous) if previous else None,
        payments_credits=Decimal(payments) if payments else None,
        total_due=Decimal(total_due) if total_due else None,
    )


def text_of(*transactions: ParsedTransaction, page: int = 1) -> str:
    """El texto del PDF que corresponde a esas transacciones."""
    lines = "\n".join(t.source_line for t in transactions)
    return f"--- pagina {page} ---\n{lines}"


def codes(report: validation.ValidationReport) -> set[str]:
    return {issue.code for issue in report.issues}


# ─── 1. Cuadre de saldo ─────────────────────────────────────────────────────
class TestCuadre:
    def test_cuando_la_suma_coincide_no_hay_observaciones(self) -> None:
        a, b = tx(amount="600.00"), tx(amount="400.00", description="OTRO")

        report = validation.validate(
            [a, b], headers=[header(new_charges="1000.00")], full_text=text_of(a, b)
        )

        assert report.balanced
        assert report.status == "draft"
        assert "balance_mismatch" not in codes(report)

    def test_una_transaccion_faltante_descuadra(self) -> None:
        a = tx(amount="600.00")

        report = validation.validate(
            [a], headers=[header(new_charges="1000.00")], full_text=text_of(a)
        )

        assert not report.balanced
        assert report.status == "needs_review"
        assert "balance_mismatch" in codes(report)
        assert report.reconciliation[0].delta == Decimal("400.00")

    def test_el_cuadre_va_contra_new_charges_y_no_contra_total_due(self) -> None:
        """El error clasico: `total_due` incluye el saldo anterior.

        Si se validara contra el, todo resumen con saldo previo daria una
        diferencia enorme y el usuario terminaria ignorando las alertas.
        """
        a = tx(amount="1000.00")

        report = validation.validate(
            [a],
            headers=[header(new_charges="1000.00", previous="50000.00", total_due="51000.00")],
            full_text=text_of(a),
        )

        assert report.balanced

    def test_los_pagos_restan(self) -> None:
        consumo = tx(amount="1500.00")
        pago = tx(amount="500.00", direction=-1, description="SU PAGO")

        report = validation.validate(
            [consumo, pago],
            headers=[header(new_charges="1000.00")],
            full_text=text_of(consumo, pago),
        )

        assert report.balanced

    def test_cada_moneda_cuadra_por_separado(self) -> None:
        ars = tx(amount="1000.00")
        usd = tx(amount="120.00", currency="USD", description="NETFLIX")

        report = validation.validate(
            [ars, usd],
            headers=[
                header(currency="ARS", new_charges="1000.00"),
                header(currency="USD", new_charges="120.00"),
            ],
            full_text=text_of(ars, usd),
        )

        assert report.balanced
        assert {r.currency for r in report.reconciliation} == {"ARS", "USD"}

    def test_una_diferencia_de_centavos_entra_en_la_tolerancia(self) -> None:
        a = tx(amount="999.60")

        report = validation.validate(
            [a], headers=[header(new_charges="1000.00")], full_text=text_of(a)
        )

        assert report.balanced

    def test_sin_totales_declarados_no_se_finge_que_cuadra(self) -> None:
        a = tx()

        report = validation.validate([a], headers=[header(new_charges=None)], full_text=text_of(a))

        assert not report.balanced
        assert "missing_statement_totals" in codes(report)
        assert report.reconciliation[0].delta is None

    def test_el_cuadre_usa_el_movimiento_neto_del_periodo(self) -> None:
        """El caso real que hizo fallar los tres primeros resúmenes de verdad.

        El emisor declara `new_charges` como los consumos SIN los pagos:

            Saldo anterior      83.998,00
            Su pago            -83.998,00
            Consumos del mes    41.999,00   <- new_charges
            Total a pagar       41.999,00

        La suma de las transacciones incluye el pago en negativo, así que
        compararla contra `new_charges` da una diferencia del tamaño del pago en
        todo resumen donde hayas pagado algo. Contra
        `total_due - previous_balance` cierra exacto.
        """
        consumo = tx(amount="41999.00")
        pago = tx(amount="83998.00", direction=-1, description="SU PAGO")

        report = validation.validate(
            [consumo, pago],
            headers=[header(previous="83998.00", new_charges="41999.00", total_due="41999.00")],
            full_text=text_of(consumo, pago),
        )

        assert report.balanced
        assert report.reconciliation[0].expected == Decimal("-41999.00")
        assert "movimiento neto" in report.reconciliation[0].basis

    def test_sin_saldo_anterior_cae_a_los_consumos_declarados(self) -> None:
        a = tx(amount="1000.00")

        report = validation.validate(
            [a], headers=[header(new_charges="1000.00", total_due=None)], full_text=text_of(a)
        )

        assert report.balanced
        assert report.reconciliation[0].basis == "consumos declarados"

    def test_con_pagos_declarados_aparte_los_resta(self) -> None:
        consumo = tx(amount="1000.00")
        pago = tx(amount="400.00", direction=-1, description="SU PAGO")

        report = validation.validate(
            [consumo, pago],
            headers=[header(new_charges="1000.00", payments="400.00", total_due=None)],
            full_text=text_of(consumo, pago),
        )

        assert report.balanced
        assert report.reconciliation[0].expected == Decimal("600.00")


# ─── 2. Identidad del header ────────────────────────────────────────────────
class TestIdentidadDelHeader:
    def test_los_totales_que_cierran_entre_si_no_avisan(self) -> None:
        a = tx()

        report = validation.validate(
            [a],
            headers=[
                header(
                    previous="5000.00",
                    payments="5000.00",
                    new_charges="1000.00",
                    total_due="1000.00",
                )
            ],
            full_text=text_of(a),
        )

        assert "header_identity_mismatch" not in codes(report)

    def test_un_total_incoherente_apunta_a_la_cabecera_y_no_a_las_transacciones(self) -> None:
        a = tx()

        report = validation.validate(
            [a],
            headers=[
                header(
                    previous="5000.00",
                    payments="5000.00",
                    new_charges="1000.00",
                    total_due="90000.00",
                )
            ],
            full_text=text_of(a),
        )

        issue = next(i for i in report.issues if i.code == "header_identity_mismatch")
        assert issue.severity == "warning"
        assert issue.detail["implied"] == "1000.00"

    def test_con_campos_faltantes_el_check_se_saltea(self) -> None:
        a = tx()

        report = validation.validate(
            [a], headers=[header(previous="5000.00")], full_text=text_of(a)
        )

        assert "header_identity_mismatch" not in codes(report)


# ─── 3. Anclaje en el texto ─────────────────────────────────────────────────
class TestAnclaje:
    def test_una_transaccion_inventada_se_detecta(self) -> None:
        real = tx(amount="600.00")
        inventada = tx(
            amount="400.00",
            description="NO EXISTE",
            source_line="30/03 UN CONSUMO QUE NADIE HIZO 400,00",
        )

        report = validation.validate(
            [real, inventada],
            headers=[header(new_charges="1000.00")],
            full_text=text_of(real),
        )

        assert report.hallucination_suspects == [1]
        assert "hallucination_suspect" in codes(report)

    def test_el_relleno_entre_columnas_no_cuenta_como_alucinacion(self) -> None:
        """El texto `layout` viene con espacios de relleno; el modelo los colapsa."""
        a = tx(source_line="15/03 COMERCIO 1000.00")
        full_text = "--- pagina 1 ---\n15/03      COMERCIO              1000.00"

        report = validation.validate([a], headers=[header()], full_text=full_text)

        assert report.hallucination_suspects == []

    def test_una_diferencia_menor_al_umbral_se_tolera(self) -> None:
        a = tx(source_line="15/03 SUPERMERCADO COTO SUC 12 1000.00")
        full_text = "--- pagina 1 ---\n15/03 SUPERMERCADO COTO SUC 13 1000.00"

        report = validation.validate([a], headers=[header()], full_text=full_text)

        assert report.hallucination_suspects == []

    def test_sin_texto_no_se_acusa_a_nadie(self) -> None:
        report = validation.validate([tx()], headers=[header()], full_text="")

        assert report.hallucination_suspects == []


# ─── 4. Truncamiento ────────────────────────────────────────────────────────
class TestTruncamiento:
    def test_una_extraccion_cortada_a_la_mitad_se_detecta(self) -> None:
        transactions = [tx(amount="100.00", description=f"COMERCIO {n}") for n in range(10)]
        full_text = text_of(*transactions)

        report = validation.validate(
            transactions[:3], headers=[header(new_charges="1000.00")], full_text=full_text
        )

        issue = next(i for i in report.issues if i.code == "possible_truncation")
        assert issue.severity == "error"
        assert issue.detail["candidate_lines"] == 10

    def test_una_extraccion_completa_no_avisa(self) -> None:
        transactions = [tx(amount="100.00", description=f"COMERCIO {n}") for n in range(10)]

        report = validation.validate(
            transactions, headers=[header(new_charges="1000.00")], full_text=text_of(*transactions)
        )

        assert "possible_truncation" not in codes(report)

    def test_el_conteo_de_candidatas_reconoce_las_lineas_de_movimiento(self) -> None:
        text = "\n".join(
            [
                "RESUMEN DE CUENTA",
                "15/03 SUPERMERCADO COTO          12.345,67",
                "16/03 FARMACIA                    1.200,00",
                "TOTAL A PAGAR",  # sin fecha: no cuenta
                "Limite de compra: 500.000",  # sin fecha: no cuenta
            ]
        )

        assert validation.count_candidate_lines(text) == 2


# ─── 5. Coherencia de fechas ────────────────────────────────────────────────
class TestFechas:
    def test_un_año_mal_inferido_se_marca(self) -> None:
        futura = tx(posted=date(2026, 12, 20))

        report = validation.validate(
            [futura], headers=[header(closing=date(2026, 3, 20))], full_text=text_of(futura)
        )

        assert "implausible_date" in codes(report)

    def test_una_fecha_dentro_de_la_ventana_no_avisa(self) -> None:
        a = tx(posted=date(2026, 2, 25))

        report = validation.validate([a], headers=[header()], full_text=text_of(a))

        assert "implausible_date" not in codes(report)

    def test_sin_cierre_el_check_se_saltea(self) -> None:
        a = tx(posted=date(2020, 1, 1))

        report = validation.validate([a], headers=[header(closing=None)], full_text=text_of(a))

        assert "implausible_date" not in codes(report)


# ─── 6. Cuotas ──────────────────────────────────────────────────────────────
class TestCuotas:
    def test_el_total_leido_donde_va_la_cuota_se_detecta(self) -> None:
        """El error mas caro: infla el mes en un orden de magnitud."""
        mala = tx(
            amount="120000.00",
            installment_number=3,
            installment_total=12,
            purchase_total="120000.00",
        )

        report = validation.validate(
            [mala], headers=[header(new_charges="120000.00")], full_text=text_of(mala)
        )

        assert "installment_amount_mismatch" in codes(report)

    def test_una_cuota_con_interes_es_plausible(self) -> None:
        buena = tx(
            amount="12000.00",
            installment_number=3,
            installment_total=12,
            purchase_total="120000.00",
        )

        report = validation.validate(
            [buena], headers=[header(new_charges="12000.00")], full_text=text_of(buena)
        )

        assert "installment_amount_mismatch" not in codes(report)

    def test_sin_total_informado_no_hay_nada_que_comparar(self) -> None:
        a = tx(installment_number=3, installment_total=12)

        report = validation.validate([a], headers=[header()], full_text=text_of(a))

        assert "installment_amount_mismatch" not in codes(report)


# ─── 7. Duplicados ──────────────────────────────────────────────────────────
class TestDuplicados:
    def test_dos_cafes_identicos_son_legitimos(self) -> None:
        cafe = tx(amount="500.00", description="CAFE")

        report = validation.validate(
            [cafe, cafe], headers=[header(new_charges="1000.00")], full_text=text_of(cafe)
        )

        assert "duplicate_dedupe_key" not in codes(report)

    def test_tres_veces_lo_mismo_es_solapamiento_mal_resuelto(self) -> None:
        cafe = tx(amount="500.00", description="CAFE")

        report = validation.validate(
            [cafe, cafe, cafe], headers=[header(new_charges="1500.00")], full_text=text_of(cafe)
        )

        assert "duplicate_dedupe_key" in codes(report)


# ─── 8. Cobertura de paginas ────────────────────────────────────────────────
class TestCoberturaDePaginas:
    def test_una_pagina_entera_sin_extraer_se_detecta(self) -> None:
        p1 = tx(amount="1000.00", description="COMERCIO A", page=1)
        p2 = tx(amount="2000.00", description="COMERCIO B", page=2)
        full_text = f"{text_of(p1, page=1)}\n\n{text_of(p2, page=2)}"

        report = validation.validate(
            [p1], headers=[header(new_charges="1000.00")], full_text=full_text
        )

        issue = next(i for i in report.issues if i.code == "page_not_covered")
        assert issue.detail["pages"] == [2]

    def test_con_todas_las_paginas_cubiertas_no_avisa(self) -> None:
        p1 = tx(amount="1000.00", description="COMERCIO A", page=1)
        p2 = tx(amount="2000.00", description="COMERCIO B", page=2)
        full_text = f"{text_of(p1, page=1)}\n\n{text_of(p2, page=2)}"

        report = validation.validate(
            [p1, p2], headers=[header(new_charges="3000.00")], full_text=full_text
        )

        assert "page_not_covered" not in codes(report)

    def test_si_el_modelo_no_completo_source_page_el_check_se_saltea(self) -> None:
        a = tx(amount="1000.00", description="COMERCIO A")
        full_text = f"{text_of(a, page=1)}\n\n--- pagina 2 ---\n16/03 OTRO 2.000,00"

        report = validation.validate(
            [a], headers=[header(new_charges="1000.00")], full_text=full_text
        )

        assert "page_not_covered" not in codes(report)


# ─── El reporte ─────────────────────────────────────────────────────────────
class TestReporte:
    def test_sin_transacciones_es_un_error(self) -> None:
        report = validation.validate([], headers=[header()], full_text="")

        assert "no_transactions" in codes(report)
        assert report.status == "needs_review"

    def test_los_descartes_de_normalizacion_quedan_registrados(self) -> None:
        a = tx()

        report = validation.validate(
            [a],
            headers=[header()],
            full_text=text_of(a),
            discarded=[Discarded(index=4, reason="fecha ilegible", source_line="???")],
        )

        assert "normalization_discarded" in codes(report)
        assert report.discarded[0]["index"] == 4

    def test_los_avisos_del_modelo_se_conservan(self) -> None:
        a = tx()

        report = validation.validate(
            [a],
            headers=[header()],
            full_text=text_of(a),
            model_warnings=["la pagina 3 parece cortada"],
        )

        assert report.model_warnings == ["la pagina 3 parece cortada"]

    def test_el_delta_para_el_prompt_de_reparacion_lleva_el_numero_exacto(self) -> None:
        a = tx(amount="600.00")

        report = validation.validate(
            [a], headers=[header(new_charges="1000.00")], full_text=text_of(a)
        )

        description = report.delta_description()
        assert "400.00" in description
        assert "ARS" in description
        assert "Faltan" in description

    def test_serializa_a_json_sin_decimales_sueltos(self) -> None:
        import json

        a = tx(amount="600.00")
        report = validation.validate(
            [a], headers=[header(new_charges="1000.00")], full_text=text_of(a)
        )

        # Tiene que entrar en una columna jsonb sin ayuda de un encoder custom.
        payload = json.loads(json.dumps(report.to_json()))
        assert payload["status"] == "needs_review"
        assert payload["reconciliation"][0]["delta"] == "400.00"

    def test_la_tolerancia_se_puede_ajustar(self) -> None:
        a = tx(amount="600.00")

        laxa = validation.validate(
            [a],
            headers=[header(new_charges="1000.00")],
            full_text=text_of(a),
            tolerances=validation.Tolerances(floors={"ARS": Decimal("500.00")}),
        )

        assert laxa.balanced
