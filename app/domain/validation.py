"""Los ocho checks que deciden si una extraccion se puede creer.

# La premisa

Un LLM leyendo una tabla de 300 lineas se equivoca de maneras que no avisa: se
saltea una fila, lee el monto de la columna de al lado, inventa una transaccion
que "tendria sentido", manda un consumo de diciembre a diciembre del año que
viene. Ninguna de esas se ve mirando la salida; todas se ven comparandola contra
algo.

Ese algo es el propio resumen, que trae sus totales. Este modulo es la
comparacion.

# Que NO hace este modulo

**Nunca descarta la extraccion.** Un documento que no cuadra se persiste igual,
con `status = 'needs_review'` y el delta anotado. El usuario ve la diferencia y
la resuelve; un documento silenciosamente descartado es un gasto que desaparece.

Y no decide nada por su cuenta sobre lo que el usuario ya toco: los conflictos y
huerfanos de una re-corrida se **registran** aca, pero quien los resuelve es la
reconciliacion de `repositories/transactions.py`.

# Sobre el check estrella

El cuadre compara `sum(amount * direction)` contra el **movimiento neto del
periodo** (`total_due - previous_balance`), no contra `new_charges`. El porque
esta en `expected_movement`, junto con los numeros reales que obligaron a
cambiarlo: comparar contra `new_charges` daba 0% de cuadre sobre resumenes de
tres emisores distintos.
"""

from __future__ import annotations

import difflib
import re
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Final, Literal

from app.domain.dates import is_plausible_for_statement
from app.domain.installments import amount_is_plausible
from app.domain.models import Discarded, ParsedStatementHeader, ParsedTransaction
from app.domain.money import quantize, within_tolerance

Severity = Literal["error", "warning", "info"]

# Confianza que se le deja a una transaccion cuya `source_line` no aparece en el
# texto. No se borra: se hunde al fondo de la pantalla de revision, donde el
# usuario la confirma o la rechaza.
HALLUCINATION_CONFIDENCE: Final = Decimal("0.20")

# Si el modelo devolvio menos de este porcentaje de las lineas que parecen
# transacciones, algo se corto.
MIN_COVERAGE_RATIO: Final = 0.80

# Una clave repetida mas veces que esto no son dos cafes iguales: es el
# solapamiento entre fragmentos mal resuelto.
MAX_LEGITIMATE_REPEATS: Final = 2

# Una linea de movimiento tiene una fecha y un monto. Es deliberadamente barata:
# sobre-contar un poco esta bien (el umbral es 80%), perderse lineas no.
#
# El monto se escribe `\d+(?:[.,]\d{3})*[.,]\d{2}` y no `\d{1,3}(...)` para que
# entre tanto `12.345,67` (con separador de miles) como `2000.00` (sin el, que es
# como emite parte de las fintechs). Con el `{1,3}` inicial, un monto plano de
# mas de tres digitos no matcheaba y el check de truncamiento se volvia ciego
# justo en los resumenes de esos emisores.
_CANDIDATE_LINE: Final = re.compile(
    r"\b\d{1,2}[/\-.]\d{1,2}(?:[/\-.]\d{2,4})?\b.*?\b\d+(?:[.,]\d{3})*[.,]\d{2}\b"
)

_PAGE_MARK: Final = re.compile(r"^---\s*pagina\s+(\d+)\s*---$", re.MULTILINE)

_WHITESPACE: Final = re.compile(r"\s+")


@dataclass(frozen=True)
class Tolerances:
    """Cuanto puede desviarse el cuadre antes de considerarse un problema.

    Sale de `Settings`, pero se pasa como dato para que este modulo siga siendo
    puro: los ocho checks se testean sin cargar configuracion ni entorno.
    """

    pct: Decimal = Decimal("0.005")
    floors: Mapping[str, Decimal] = field(
        default_factory=lambda: {"ARS": Decimal("50.00"), "USD": Decimal("1.00")}
    )
    default_floor: Decimal = Decimal("1.00")
    source_line_min_ratio: float = 0.85

    def floor(self, currency: str) -> Decimal:
        return self.floors.get(currency.upper(), self.default_floor)

    def allowed(self, currency: str, expected: Decimal) -> Decimal:
        return max(abs(expected) * self.pct, self.floor(currency))


@dataclass(frozen=True)
class ValidationIssue:
    code: str
    severity: Severity
    message: str
    detail: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "severity": self.severity,
            "message": self.message,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class Reconciliation:
    """El cuadre de una moneda: lo que suman las transacciones vs. el resumen."""

    currency: str
    computed: Decimal
    expected: Decimal | None
    # De donde salio `expected`. Va al reporte y a la pantalla: "no cuadra" es
    # mucho menos util que "no cuadra contra el movimiento neto del periodo".
    basis: str
    tolerance: Decimal
    transaction_count: int

    @property
    def delta(self) -> Decimal | None:
        """Cuanto falta. Positivo = el resumen declara mas de lo extraido."""
        if self.expected is None:
            return None
        return quantize(self.expected - self.computed)

    @property
    def balanced(self) -> bool:
        """Sin `expected` no hay cuadre posible; no se finge que dio bien."""
        delta = self.delta
        return delta is not None and abs(delta) <= self.tolerance

    def to_json(self) -> dict[str, Any]:
        delta = self.delta
        return {
            "currency": self.currency,
            "computed": str(self.computed),
            "expected": str(self.expected) if self.expected is not None else None,
            "basis": self.basis,
            "delta": str(delta) if delta is not None else None,
            "tolerance": str(self.tolerance),
            "balanced": self.balanced,
            "transaction_count": self.transaction_count,
        }


@dataclass
class ValidationReport:
    """Lo que se guarda en `extraction_runs.validation` y decide el status."""

    reconciliation: list[Reconciliation] = field(default_factory=list)
    issues: list[ValidationIssue] = field(default_factory=list)
    # Indices en la lista de transacciones validadas, no ids: al momento de
    # validar todavia no se persistio nada.
    hallucination_suspects: list[int] = field(default_factory=list)
    discarded: list[dict[str, Any]] = field(default_factory=list)
    model_warnings: list[str] = field(default_factory=list)
    # Los completa la reconciliacion contra lo que ya estaba en la base.
    conflicts: list[dict[str, Any]] = field(default_factory=list)
    orphans: list[dict[str, Any]] = field(default_factory=list)

    def add(self, code: str, severity: Severity, message: str, **detail: Any) -> None:
        self.issues.append(
            ValidationIssue(code=code, severity=severity, message=message, detail=detail)
        )

    @property
    def errors(self) -> list[ValidationIssue]:
        return [issue for issue in self.issues if issue.severity == "error"]

    @property
    def balanced(self) -> bool:
        """Todas las monedas cuadran. Sin monedas, no cuadra nada."""
        return bool(self.reconciliation) and all(r.balanced for r in self.reconciliation)

    @property
    def status(self) -> Literal["draft", "needs_review"]:
        """El estado que le corresponde al statement.

        `draft` no quiere decir "confirmado": la confirmacion es siempre una
        accion explicita del usuario. Quiere decir "no encontramos nada raro".
        """
        return "draft" if self.balanced and not self.errors else "needs_review"

    def unbalanced(self) -> list[Reconciliation]:
        return [r for r in self.reconciliation if not r.balanced]

    def delta_description(self) -> str:
        """El texto que va al prompt de reparacion.

        Se le da el numero exacto y el sentido, no "no cuadra": el modelo trabaja
        mucho mejor buscando "faltan 12.480,50 ARS" que buscando "algo".
        """
        lines: list[str] = []
        for rec in self.unbalanced():
            if rec.expected is None:
                lines.append(
                    f"- {rec.currency}: {rec.basis}, así que no se pudo verificar la "
                    f"suma (lo extraído suma {rec.computed})."
                )
                continue

            delta = rec.delta or Decimal("0")
            verb = "faltan" if delta > 0 else "sobran"
            lines.append(
                f"- {rec.currency}: el resumen implica {rec.expected} de movimiento "
                f"({rec.basis}) y lo extraído suma {rec.computed}. "
                f"{verb.capitalize()} {abs(delta)} {rec.currency}."
            )
        return "\n".join(lines) if lines else "- (sin diferencias de cuadre)"

    def to_json(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "balanced": self.balanced,
            "reconciliation": [r.to_json() for r in self.reconciliation],
            "issues": [i.to_json() for i in self.issues],
            "hallucination_suspects": self.hallucination_suspects,
            "discarded": self.discarded,
            "model_warnings": self.model_warnings,
            "conflicts": self.conflicts,
            "orphans": self.orphans,
        }


# ─────────────────────────────────────────────────────────────────────────────
# Contra que se compara la suma de las transacciones
# ─────────────────────────────────────────────────────────────────────────────
#
# Esto parecia trivial y no lo era. La primera version comparaba contra
# `new_charges` a secas, y con resumenes reales de tres emisores dio 0% de
# cuadre. El motivo:
#
#   Saldo anterior      83.998,00
#   Su pago            -83.998,00
#   Consumos del mes    41.999,00   <- new_charges
#   Total a pagar       41.999,00
#
# `new_charges` son los consumos **sin los pagos**, mientras que
# `sum(amount * direction)` incluye el pago en negativo. Comparar uno contra
# otro da una diferencia del tamaño del pago en todo resumen donde hayas pagado
# algo, que son todos.
#
# Lo que si es una identidad de la cuenta, y no depende de como cada emisor
# reparta la informacion, es el **movimiento neto del periodo**:
#
#   total_due - previous_balance  ==  sum(amount * direction)
#
# Sobre los resumenes reales que motivaron el cambio, esa igualdad dio exacta al
# centavo en cuatro de cinco secciones (y la quinta no traia los dos campos).
#
# El orden de preferencia de abajo va de la referencia mas confiable a la menos.
CurrencyTotals = tuple[Decimal | None, str]


def expected_movement(header: ParsedStatementHeader) -> CurrencyTotals:
    """Cuanto deberian sumar las transacciones, y de donde sale ese numero.

    Devuelve `(None, motivo)` cuando el resumen no trae con que verificar: no se
    finge un cuadre que no se puede calcular.
    """
    net = header.net_movement
    if net is not None:
        return net, "movimiento neto del período (total a pagar menos saldo anterior)"

    if header.new_charges is not None:
        if header.payments_credits is not None:
            return (
                quantize(header.new_charges - header.payments_credits),
                "consumos declarados menos pagos declarados",
            )
        # Sin pagos informados, lo mejor disponible. Si el resumen tuviera pagos
        # y no los declarara aparte, este cuadre va a fallar por el monto del
        # pago — y eso es correcto que se vea, no que se disimule.
        return header.new_charges, "consumos declarados"

    return None, "el resumen no declara totales con los que verificar"


# ─────────────────────────────────────────────────────────────────────────────
# La entrada
# ─────────────────────────────────────────────────────────────────────────────
def validate(
    transactions: Sequence[ParsedTransaction],
    *,
    headers: Sequence[ParsedStatementHeader],
    full_text: str,
    tolerances: Tolerances | None = None,
    model_warnings: Sequence[str] = (),
    discarded: Sequence[Discarded] = (),
) -> ValidationReport:
    """Corre los ocho checks y devuelve el reporte."""
    cfg = tolerances or Tolerances()
    report = ValidationReport(model_warnings=list(model_warnings))

    for item in discarded:
        report.discarded.append(
            {"index": item.index, "reason": item.reason, "source_line": item.source_line[:200]}
        )
    if discarded:
        report.add(
            "normalization_discarded",
            "warning",
            f"{len(discarded)} líneas devueltas por el modelo no se pudieron normalizar",
            count=len(discarded),
        )

    if not transactions:
        report.add("no_transactions", "error", "el modelo no devolvió ninguna transacción usable")

    _check_balance(report, transactions, headers, cfg)
    _check_header_identity(report, headers, cfg)
    _check_source_anchoring(report, transactions, full_text, cfg)
    _check_coverage(report, transactions, full_text)
    _check_dates(report, transactions, headers)
    _check_installments(report, transactions)
    _check_duplicates(report, transactions)
    _check_pages(report, transactions, full_text)

    return report


# ─── 1. Cuadre de saldo ─────────────────────────────────────────────────────
def _check_balance(
    report: ValidationReport,
    transactions: Sequence[ParsedTransaction],
    headers: Sequence[ParsedStatementHeader],
    cfg: Tolerances,
) -> None:
    """`sum(amount * direction)` contra el movimiento del periodo, por moneda.

    Es el check principal: si da, practicamente todo lo demas dio. Ver
    `expected_movement` para por que la referencia no es `new_charges` a secas.
    """
    sums: defaultdict[str, Decimal] = defaultdict(lambda: Decimal("0"))
    counts: Counter[str] = Counter()
    for tx in transactions:
        sums[tx.currency] += tx.signed_amount
        counts[tx.currency] += 1

    currencies = sorted({*sums, *(h.currency for h in headers)})
    by_currency = {h.currency: h for h in headers}

    for currency in currencies:
        header = by_currency.get(currency)
        expected, basis = (
            expected_movement(header)
            if header is not None
            else (None, "no se leyó la cabecera de esta moneda")
        )
        computed = quantize(sums.get(currency, Decimal("0")))

        rec = Reconciliation(
            currency=currency,
            computed=computed,
            expected=expected,
            basis=basis,
            tolerance=cfg.allowed(currency, expected if expected is not None else computed),
            transaction_count=counts.get(currency, 0),
        )
        report.reconciliation.append(rec)

        if expected is None:
            report.add(
                "missing_statement_totals",
                "warning",
                f"{currency}: {basis}; no se puede verificar la suma",
                currency=currency,
                computed=str(computed),
            )
        elif not rec.balanced:
            report.add(
                "balance_mismatch",
                "error",
                f"{currency}: la suma de las transacciones ({computed}) no coincide "
                f"con el {basis} ({expected}); diferencia {rec.delta}",
                currency=currency,
                computed=str(computed),
                expected=str(expected),
                basis=basis,
                delta=str(rec.delta),
            )


# ─── 2. Identidad del header ────────────────────────────────────────────────
def _check_header_identity(
    report: ValidationReport, headers: Sequence[ParsedStatementHeader], cfg: Tolerances
) -> None:
    """`previous_balance - payments + new_charges ≈ total_due`.

    Es lo que distingue "el modelo se comio transacciones" de "el modelo leyo mal
    los totales". Si esta identidad falla, el problema esta en la cabecera y
    reintentar la extraccion de transacciones no lo arregla.
    """
    for header in headers:
        previous = header.previous_balance
        payments = header.payments_credits
        charges = header.new_charges
        total = header.total_due
        if previous is None or payments is None or charges is None or total is None:
            continue

        implied = quantize(previous - payments + charges)
        if within_tolerance(total, implied, pct=cfg.pct, floor=cfg.floor(header.currency)):
            continue

        report.add(
            "header_identity_mismatch",
            "warning",
            f"{header.currency}: los totales de la cabecera no cierran entre sí "
            f"({previous} - {payments} + {charges} = {implied}, pero el total a "
            f"pagar dice {total}); probablemente esté mal leída la cabecera, no "
            "las transacciones",
            currency=header.currency,
            implied=str(implied),
            total_due=str(total),
        )


# ─── 3. Anclaje en el texto ─────────────────────────────────────────────────
def _check_source_anchoring(
    report: ValidationReport,
    transactions: Sequence[ParsedTransaction],
    full_text: str,
    cfg: Tolerances,
) -> None:
    """Cada `source_line` tiene que existir en el texto de entrada.

    Es el check anti-alucinacion, y es casi deterministico: una transaccion
    inventada no puede traer una linea literal que este en el PDF.

    La comparacion es por similitud y no exacta porque el modelo a veces
    normaliza espacios al copiar, sobre todo con el texto `layout` que viene
    lleno de relleno entre columnas.
    """
    lines = [_normalize(line) for line in full_text.splitlines()]
    haystack = {line for line in lines if line}
    if not haystack:
        return

    candidates = sorted(haystack)
    suspects: list[int] = []

    for index, tx in enumerate(transactions):
        needle = _normalize(tx.source_line)
        if not needle:
            suspects.append(index)
            continue

        # Exacto o contenido: cubre la gran mayoria sin pagar difflib.
        if needle in haystack or any(needle in line for line in haystack):
            continue

        close = difflib.get_close_matches(needle, candidates, n=1, cutoff=cfg.source_line_min_ratio)
        if not close:
            suspects.append(index)

    if not suspects:
        return

    report.hallucination_suspects = suspects
    report.add(
        "hallucination_suspect",
        "error",
        f"{len(suspects)} transacciones citan una línea que no aparece en el texto "
        "del PDF: pueden ser inventadas",
        count=len(suspects),
        indexes=suspects[:50],
    )


# ─── 4. Conteo de lineas candidatas ─────────────────────────────────────────
def _check_coverage(
    report: ValidationReport, transactions: Sequence[ParsedTransaction], full_text: str
) -> None:
    """Cuantas lineas parecian transacciones vs. cuantas devolvio el modelo.

    Atrapa el fallo mas comun de los modelos sobre tablas largas: empezar bien y
    dejar de listar a la mitad.
    """
    candidates = count_candidate_lines(full_text)
    if candidates == 0:
        return

    ratio = len(transactions) / candidates
    if ratio >= MIN_COVERAGE_RATIO:
        return

    report.add(
        "possible_truncation",
        "error",
        f"el texto tiene ~{candidates} líneas con fecha y monto pero el modelo "
        f"devolvió {len(transactions)}: la extracción parece cortada",
        candidate_lines=candidates,
        extracted=len(transactions),
        ratio=round(ratio, 3),
    )


def count_candidate_lines(text: str) -> int:
    """Lineas que tienen pinta de movimiento: una fecha y un monto."""
    return sum(1 for line in text.splitlines() if _CANDIDATE_LINE.search(line))


# ─── 5. Coherencia de fechas ────────────────────────────────────────────────
def _check_dates(
    report: ValidationReport,
    transactions: Sequence[ParsedTransaction],
    headers: Sequence[ParsedStatementHeader],
) -> None:
    """Toda fecha cae en la ventana razonable alrededor del cierre.

    Atrapa el año mal inferido, que es el error que mas duele: manda el consumo a
    un mes que el usuario no mira y lo hace reaparecer doce meses despues.
    """
    closings = {h.currency: h.closing_date for h in headers if h.closing_date is not None}
    if not closings:
        return

    offenders: list[dict[str, Any]] = []
    for index, tx in enumerate(transactions):
        closing = closings.get(tx.currency) or next(iter(closings.values()))
        if is_plausible_for_statement(tx.posted_date, closing=closing):
            continue
        offenders.append(
            {
                "index": index,
                "posted_date": tx.posted_date.isoformat(),
                "closing_date": closing.isoformat(),
                "description": tx.description_raw[:80],
            }
        )

    if offenders:
        report.add(
            "implausible_date",
            "warning",
            f"{len(offenders)} transacciones tienen una fecha lejos del cierre del "
            "resumen; puede estar mal inferido el año",
            count=len(offenders),
            samples=offenders[:10],
        )


# ─── 6. Cuotas ──────────────────────────────────────────────────────────────
def _check_installments(
    report: ValidationReport, transactions: Sequence[ParsedTransaction]
) -> None:
    """La cuota tiene que ser coherente con el total informado de la compra.

    Atrapa el error de leer el total de la compra donde va la cuota del mes, que
    infla el gasto en un orden de magnitud.
    """
    offenders: list[dict[str, Any]] = []

    for index, tx in enumerate(transactions):
        if tx.installment_total is None or tx.purchase_total_amount is None:
            continue
        if amount_is_plausible(
            amount=tx.amount,
            purchase_total_amount=tx.purchase_total_amount,
            installments=tx.installment_total,
        ):
            continue
        offenders.append(
            {
                "index": index,
                "amount": str(tx.amount),
                "purchase_total_amount": str(tx.purchase_total_amount),
                "installments": tx.installment_total,
                "description": tx.description_raw[:80],
            }
        )

    if offenders:
        report.add(
            "installment_amount_mismatch",
            "warning",
            f"{len(offenders)} cuotas tienen un monto que no se explica con el total "
            "de la compra informado",
            count=len(offenders),
            samples=offenders[:10],
        )


# ─── 7. Duplicados ──────────────────────────────────────────────────────────
def _check_duplicates(report: ValidationReport, transactions: Sequence[ParsedTransaction]) -> None:
    """Una misma clave mas de dos veces es solapamiento mal resuelto.

    Dos veces puede ser legitimo (dos cafes iguales el mismo dia). Tres ya es
    sospechoso, y la diferencia importa: colapsar los legitimos descuadra el mes
    tanto como duplicar los falsos.
    """
    counts = Counter(tx.dedupe_key for tx in transactions)
    repeated = {key: n for key, n in counts.items() if n > MAX_LEGITIMATE_REPEATS}
    if not repeated:
        return

    samples = [
        {"dedupe_key": key[:12], "count": n, "description": _first_description(transactions, key)}
        for key, n in list(repeated.items())[:10]
    ]
    report.add(
        "duplicate_dedupe_key",
        "warning",
        f"{len(repeated)} transacciones aparecen más de {MAX_LEGITIMATE_REPEATS} veces "
        "idénticas; puede ser el solapamiento entre fragmentos",
        count=len(repeated),
        samples=samples,
    )


def _first_description(transactions: Sequence[ParsedTransaction], key: str) -> str:
    for tx in transactions:
        if tx.dedupe_key == key:
            return tx.description_raw[:80]
    return ""


# ─── 8. Cobertura de paginas ────────────────────────────────────────────────
def _check_pages(
    report: ValidationReport, transactions: Sequence[ParsedTransaction], full_text: str
) -> None:
    """Toda pagina con lineas candidatas tiene que aparecer en algun `source_page`.

    Una pagina entera sin transacciones, teniendo lineas con fecha y monto, es
    una pagina que el modelo no miro.
    """
    pages_with_candidates = _pages_with_candidates(full_text)
    if not pages_with_candidates:
        return

    covered = {tx.source_page for tx in transactions if tx.source_page is not None}
    if not covered:
        # El modelo no completo `source_page` en ninguna: no hay nada que
        # comparar, y no se reporta un problema que no se puede distinguir de
        # que el campo sea opcional.
        return

    missing = sorted(pages_with_candidates - covered)
    if missing:
        report.add(
            "page_not_covered",
            "warning",
            f"las páginas {missing} tienen líneas con fecha y monto pero ninguna "
            "transacción extraída dice venir de ellas",
            pages=missing,
        )


def _pages_with_candidates(full_text: str) -> set[int]:
    """Paginas del texto marcado que tienen al menos una linea de movimiento."""
    pages: set[int] = set()
    current: int | None = None

    for line in full_text.splitlines():
        mark = _PAGE_MARK.match(line.strip())
        if mark:
            current = int(mark.group(1))
            continue
        if current is not None and _CANDIDATE_LINE.search(line):
            pages.add(current)

    return pages


def _normalize(text: str) -> str:
    """Colapsa espacios: el texto `layout` viene con relleno entre columnas."""
    return _WHITESPACE.sub(" ", text).strip().upper()
