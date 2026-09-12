"""Accuracy de la extraccion por emisor, sin red y sin base.

# Que pregunta contesta

"¿En que emisor se equivoca el modelo?" — que no es lo mismo que "¿cuadra el
resumen?". Un resumen puede cuadrar con dos transacciones mal leidas que se
compensan, y puede no cuadrar por una linea que el PDF trae mal. Medir por emisor
es lo que permite decidir donde poner el trabajo de prompt: si el 12% de las
lineas de un emisor hay que corregirlas y las de otro el 1%, el proximo `_v2` es
para el primero.

# Por que no le pega al modelo

La comparacion corre sobre la **respuesta grabada** del modelo que trae cada caso
y sobre el codigo de normalizacion real (`app.domain.models.normalize_all`). Eso
es lo que se quiere medir aca: que tan bien se convierte lo que el modelo dijo en
lo que el sistema guarda, de forma deterministica, gratis y repetible en CI.

Para medir al modelo en vivo —que cuesta plata y no es deterministico— esta
`python -m app.cli parse <document_id> --user <uuid> --dry-run`, que llama a
Gemini y muestra el cuadre sin escribir nada.

# El formato de los casos

Un JSON por caso, en `tests/fixtures/expected/<emisor>/<caso>.json`:

    {
      "issuer": "galicia",
      "closing_date": "2026-01-15",
      "transactions": [ ... ],
      "expected": [
        {
          "posted_date": "2026-01-03",
          "description_raw": "MERPAGO*SPOTIFY 1234",
          "amount": "4999.00",
          "direction": 1,
          "kind": "charge"
        }
      ]
    }

`transactions` es la respuesta cruda del modelo tal como la devolvio (el mismo
contenido que un cassette de `app/llm/fake.py`); `expected` es lo que una persona
verifico contra el PDF. `closing_date` es la fecha de cierre del resumen, que es
lo que permite resolver un `03/01` sin año.

Los casos **no estan en el repo**: se arman a partir de resumenes reales, que
nunca se commitean (ver `scripts/check_no_pdfs.py`). Cada quien genera los suyos
en `tests/fixtures/expected/`, que esta fuera del control de versiones.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

import typer

from app.domain.models import ParsedTransaction, normalize_all
from app.schemas.llm import TransactionsPayload

DEFAULT_FIXTURES = Path("tests/fixtures/expected")


@dataclass
class IssuerScore:
    """Lo que salio bien y lo que no, para un emisor."""

    issuer: str
    cases: int = 0
    expected: int = 0
    matched: int = 0
    missing: list[str] = field(default_factory=list)
    extra: list[str] = field(default_factory=list)
    wrong_description: list[str] = field(default_factory=list)
    discarded: list[str] = field(default_factory=list)

    @property
    def accuracy(self) -> float:
        if not self.expected:
            return 1.0
        return self.matched / self.expected


def register(cli: typer.Typer) -> None:
    @cli.command()
    def accuracy(
        fixtures: Path = typer.Option(
            DEFAULT_FIXTURES, "--fixtures", help="Directorio con los casos por emisor."
        ),
        issuer: str = typer.Option(None, "--issuer", help="Medir un solo emisor."),
    ) -> None:
        """Compara la extraccion contra los casos verificados a mano, por emisor."""
        cases = sorted(fixtures.glob("*/*.json"))
        if issuer:
            cases = [case for case in cases if case.parent.name == issuer]

        if not cases:
            typer.secho(
                f"no hay casos en {fixtures}/. Ver el docstring de app/cli/accuracy.py "
                "para el formato: son datos de resúmenes reales y no se commitean.",
                fg=typer.colors.YELLOW,
            )
            raise typer.Exit(code=1)

        scores = _score_all(cases)
        _print(scores)

        if any(score.accuracy < 1.0 for score in scores.values()):
            raise typer.Exit(code=2)


def _score_all(cases: list[Path]) -> dict[str, IssuerScore]:
    scores: dict[str, IssuerScore] = {}
    for path in cases:
        case = json.loads(path.read_text(encoding="utf-8"))
        name = str(case.get("issuer") or path.parent.name)
        score = scores.setdefault(name, IssuerScore(issuer=name))
        _score_case(score, case, label=path.stem)
    return scores


def _score_case(score: IssuerScore, case: dict[str, Any], *, label: str) -> None:
    score.cases += 1

    closing = case.get("closing_date")
    payload = TransactionsPayload.model_validate({"transactions": case.get("transactions", [])})
    parsed, discarded = normalize_all(
        payload.transactions,
        closing=date.fromisoformat(str(closing)) if closing else None,
    )
    score.discarded.extend(f"{label}: {item.reason}" for item in discarded)

    got = {_key(tx.posted_date, str(tx.amount), tx.direction): tx for tx in parsed}
    seen: set[tuple[str, str, int]] = set()

    for item in case.get("expected", []):
        score.expected += 1
        key = _key(
            date.fromisoformat(str(item["posted_date"])),
            str(item["amount"]),
            int(item.get("direction", 1)),
        )
        found = got.get(key)
        if found is None:
            score.missing.append(f"{label}: {item['description_raw']} ({item['amount']})")
            continue

        seen.add(key)
        score.matched += 1
        if not _same_description(found, str(item["description_raw"])):
            score.wrong_description.append(
                f"{label}: leyó {found.description_raw!r}, esperado {item['description_raw']!r}"
            )

    score.extra.extend(
        f"{label}: {tx.description_raw} ({tx.amount})" for key, tx in got.items() if key not in seen
    )


def _key(posted: date, amount: str, direction: int) -> tuple[str, str, int]:
    """La identidad con la que se aparea una linea leida con una verificada.

    Se aparea por fecha, monto y signo y **no** por descripcion: una descripcion
    mal transcripta es justamente lo que se quiere medir, y usarla para aparear
    convertiria cada error de transcripcion en un "falta una transaccion y sobra
    otra", que no distingue un error de lectura de una linea perdida.
    """
    return posted.isoformat(), amount, direction


def _same_description(parsed: ParsedTransaction, expected: str) -> bool:
    return " ".join(parsed.description_raw.split()).upper() == " ".join(expected.split()).upper()


def _print(scores: dict[str, IssuerScore]) -> None:
    typer.echo("")
    for name in sorted(scores):
        score = scores[name]
        color = typer.colors.GREEN if score.accuracy == 1.0 else typer.colors.RED
        typer.secho(
            f"  {name:<16} {score.accuracy:6.1%}  "
            f"({score.matched}/{score.expected} líneas, {score.cases} casos)",
            fg=color,
        )
        _print_items("faltan", score.missing)
        _print_items("sobran", score.extra)
        _print_items("descripción", score.wrong_description)
        _print_items("descartadas", score.discarded)
    typer.echo("")


def _print_items(title: str, items: list[str]) -> None:
    for item in items:
        typer.secho(f"      [{title}] {item}", fg=typer.colors.YELLOW)
