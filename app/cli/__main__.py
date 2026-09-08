"""CLI de mantenimiento.

uv run python -m app.cli migrate
uv run python -m app.cli seed
"""

from __future__ import annotations

import asyncio
import os

import typer

from app.cli import migrations

cli = typer.Typer(help="Utilidades de Finance Consolidation", no_args_is_help=True)


def _migration_url(explicit: str | None) -> str:
    url = explicit or os.environ.get("MIGRATION_DATABASE_URL") or os.environ.get("DATABASE_URL")
    if not url:
        raise typer.BadParameter(
            "falta la URL de la base: pasar --url o setear MIGRATION_DATABASE_URL"
        )
    return url


@cli.command()
def migrate(
    url: str = typer.Option(None, "--url", help="DSN de Postgres con permisos de owner"),
    seed: bool = typer.Option(True, help="Aplicar tambien supabase/seed.sql"),
    force: bool = typer.Option(False, help="Reaplicar todas, ignorando el registro"),
) -> None:
    """Aplica las migraciones pendientes."""
    target = _migration_url(url)
    password = os.environ.get("APP_RUNTIME_PASSWORD")

    applied = asyncio.run(
        migrations.apply_all(target, seed=seed, app_runtime_password=password, force=force)
    )

    if applied:
        typer.echo(f"aplicadas {len(applied)} migraciones:")
        for name in applied:
            typer.echo(f"  + {name}")
    else:
        typer.echo("sin migraciones pendientes")

    if not password:
        typer.secho(
            "aviso: APP_RUNTIME_PASSWORD no esta seteada; la contraseña del rol "
            "app_runtime queda como estaba",
            fg=typer.colors.YELLOW,
        )


@cli.command(name="seed")
def seed_only(
    url: str = typer.Option(None, "--url", help="DSN de Postgres con permisos de owner"),
) -> None:
    """Carga las categorias del sistema (idempotente)."""
    conn_url = _migration_url(url)
    asyncio.run(migrations.apply_all(conn_url, seed=True))
    typer.echo("seed aplicado")


if __name__ == "__main__":
    cli()
