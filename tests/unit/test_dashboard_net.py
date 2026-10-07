"""El neto del dashboard, el parseo de `year_month` y el render del signo."""

from __future__ import annotations

import re
from decimal import Decimal

import pytest

from app.web.dashboard import _net, _parse_year_month
from app.web.templates import TEMPLATES_DIR, templates

D = Decimal
MINUS = chr(0x2212)


def test_net_basic() -> None:
    net = _net({"ARS": D("100")}, {"ARS": D("500")}, {"ARS": D("20")}, {"ARS": D("30")}, ["ARS"])
    assert net == {"ARS": D("350")}


def test_net_only_income_only_spending_only_taxes() -> None:
    assert _net({}, {"ARS": D("10")}, {}, {}, ["ARS"]) == {"ARS": D("10")}
    assert _net({"ARS": D("10")}, {}, {}, {}, ["ARS"]) == {"ARS": D("-10")}
    assert _net({}, {}, {"ARS": D("5")}, {}, ["ARS"]) == {"ARS": D("-5")}


def test_net_refunds_raise_the_net() -> None:
    net = _net({"ARS": D("100")}, {"ARS": D("0")}, {"ARS": D("-20")}, {"ARS": D("-5")}, ["ARS"])
    assert net["ARS"] == D("-75")


def test_net_ignores_transfers() -> None:
    # La firma ni siquiera recibe transferencias: no pueden afectar el neto.
    assert _net({"ARS": D("1")}, {"ARS": D("3")}, {}, {}, ["ARS"]) == {"ARS": D("2")}


def test_net_currency_with_partial_keys_and_decimal_type() -> None:
    net = _net({"USD": D("1.50")}, {}, {}, {"ARS": D("2")}, ["ARS", "USD"])
    assert net == {"ARS": D("-2"), "USD": D("-1.50")}
    assert all(isinstance(v, Decimal) for v in net.values())


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("2026-3", (2026, 3)),
        ("2026-03", (2026, 3)),
        ("2026-12", (2026, 12)),
        ("2026-13", None),
        ("2026-0", None),
        ("abc", None),
        ("", None),
        (None, None),
        ("2026", None),
        ("2026-x", None),
        ("-3", None),
    ],
)
def test_parse_year_month(raw: str | None, expected: tuple[int, int] | None) -> None:
    assert _parse_year_month(raw) == expected


def _signed(n: Decimal | int) -> str:
    """Renderiza el macro `signed` tal como esta en el template del dashboard."""
    source = (TEMPLATES_DIR / "dashboard" / "index.html").read_text(encoding="utf-8")
    macro = re.search(r"\{% macro signed.*?\{% endmacro %\}", source, re.S)
    assert macro is not None
    module = templates.env.from_string(macro.group(0)).make_module()
    return str(module.signed(n)).strip()


def test_signed_negative_has_single_sign() -> None:
    out = _signed(D("-1234.50"))
    assert out == MINUS + "1.234,50"
    assert MINUS + "-" not in out
    assert "--" not in out


def test_signed_positive_and_zero() -> None:
    assert _signed(D("1234.50")) == "+1.234,50"
    assert _signed(D("0")) == "0,00"
