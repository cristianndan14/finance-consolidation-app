"""El comando que hace barata la iteracion de prompts.

# Por que existe un CLI para esto

Mejorar la extraccion es un ciclo de "cambio el prompt, miro que pasa sobre los
resumenes reales, comparo". Hacer ese ciclo por la UI implica subir, esperar el
job, navegar al documento y leer la tabla: un minuto por iteracion y ninguna
comparacion facil entre dos versiones.

Con `--dry-run` el ciclo es un comando: llama al modelo, valida, imprime el
cuadre por moneda y no escribe nada. Se puede correr sobre el mismo documento con
`--transactions-prompt parse_transactions_v2` y comparar los deltas al lado.

Que **no escriba nada** es la parte importante: permite probar prompts contra
documentos que el usuario ya reviso y confirmo, sin riesgo de pisarle el trabajo.
"""

from __future__ import annotations

import asyncio

import typer

from app.domain.validation import ValidationReport
from app.infra import db
from app.llm import factory as llm_factory
from app.llm import prompts
from app.services import parse as parse_service
from app.settings import get_settings


async def _run(
    *,
    user_id: str,
    document_id: str,
    dry_run: bool,
    header_prompt: str,
    transactions_prompt: str,
    repair: bool,
) -> parse_service.ParseOutcome:
    settings = get_settings()
    db.init_engine(settings)
    extractor = llm_factory.build(settings)
    try:
        return await parse_service.parse_document(
            user_id=user_id,
            document_id=document_id,
            extractor=extractor,
            settings=settings,
            dry_run=dry_run,
            allow_repair=repair,
            header_prompt=header_prompt,
            transactions_prompt=transactions_prompt,
        )
    finally:
        await extractor.aclose()
        await db.dispose_engine()


def register(cli: typer.Typer) -> None:
    @cli.command()
    def parse(
        document_id: str = typer.Argument(..., help="UUID del documento ya extraido"),
        user: str = typer.Option(
            ...,
            "--user",
            help=(
                "UUID del dueño del documento. Es obligatorio porque la conexion "
                "corre con RLS: sin identidad no se ve ninguna fila."
            ),
        ),
        dry_run: bool = typer.Option(
            True,
            "--dry-run/--write",
            help="Con --dry-run llama al modelo y valida, pero no escribe en la base.",
        ),
        header_prompt: str = typer.Option(prompts.PARSE_HEADER, "--header-prompt"),
        transactions_prompt: str = typer.Option(
            prompts.PARSE_TRANSACTIONS, "--transactions-prompt"
        ),
        repair: bool = typer.Option(
            True, "--repair/--no-repair", help="Intentar una pasada de correccion si no cuadra."
        ),
    ) -> None:
        """Corre la etapa 2 sobre un documento y muestra el cuadre."""
        try:
            outcome = asyncio.run(
                _run(
                    user_id=user,
                    document_id=document_id,
                    dry_run=dry_run,
                    header_prompt=header_prompt,
                    transactions_prompt=transactions_prompt,
                    repair=repair,
                )
            )
        except parse_service.ParseError as exc:
            typer.secho(f"no se pudo parsear: {exc.message} ({exc.reason})", fg=typer.colors.RED)
            raise typer.Exit(code=1) from exc

        _print(outcome, dry_run=dry_run)
        if not outcome.report.balanced:
            # Salir distinto de 0 permite encadenar el comando en un script que
            # corra el corpus entero y cuente cuantos cuadran.
            raise typer.Exit(code=2)

    @cli.command(name="prompts")
    def list_prompts() -> None:
        """Lista las versiones de prompt disponibles."""
        for name in prompts.available():
            typer.echo(f"  {name}")


def _print(outcome: parse_service.ParseOutcome, *, dry_run: bool) -> None:
    mode = "DRY RUN (no se escribio nada)" if dry_run else "escrito en la base"
    typer.echo(f"\ndocumento {outcome.document_id}  [{mode}]")
    typer.echo(f"  modelo        : {outcome.provider}/{outcome.model}")
    typer.echo(f"  transacciones : {len(outcome.transactions)}")
    typer.echo(f"  costo         : USD {outcome.usage.cost_usd}")
    if outcome.repaired:
        typer.echo("  reparacion    : aplicada")

    typer.echo("\n  cuadre por moneda:")
    for rec in outcome.report.reconciliation:
        mark = "OK  " if rec.balanced else "FALLA"
        expected = rec.expected if rec.expected is not None else "-"
        typer.secho(
            f"    {mark} {rec.currency}: extraido {rec.computed}  declarado {expected}  "
            f"delta {rec.delta}  ({rec.transaction_count} tx)",
            fg=typer.colors.GREEN if rec.balanced else typer.colors.RED,
        )

    _print_issues(outcome.report)

    if not dry_run:
        typer.echo(
            f"\n  persistido: +{outcome.inserted} nuevas, ~{outcome.updated} actualizadas, "
            f"-{outcome.deleted} borradas"
        )
    typer.echo(f"\n  estado del resumen: {outcome.status}\n")


def _print_issues(report: ValidationReport) -> None:
    if not report.issues:
        typer.secho("\n  sin observaciones", fg=typer.colors.GREEN)
        return

    typer.echo("\n  observaciones:")
    colors = {
        "error": typer.colors.RED,
        "warning": typer.colors.YELLOW,
        "info": typer.colors.BLUE,
    }
    for issue in report.issues:
        typer.secho(
            f"    [{issue.severity}] {issue.code}: {issue.message}", fg=colors[issue.severity]
        )

    if report.conflicts:
        typer.echo(f"    {len(report.conflicts)} transacciones editadas por vos se conservaron")
    if report.orphans:
        typer.echo(f"    {len(report.orphans)} transacciones tuyas ya no aparecen en la extraccion")
