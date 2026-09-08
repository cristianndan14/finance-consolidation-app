.DEFAULT_GOAL := help
SUPABASE := npx --yes supabase

.PHONY: help install dev db-up db-down db-reset migrate seed lint fmt type test test-unit test-rls test-llm check css clean

help:  ## Muestra esta ayuda
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "};{printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

install:  ## Instala dependencias con uv
	uv sync --extra dev

dev:  ## Levanta el servidor con reload
	uv run uvicorn app.main:app --reload --port 8000

# ─── Base de datos local ─────────────────────────────────────────────────────
db-up:  ## Arranca Supabase local (necesita Docker)
	$(SUPABASE) start

db-down:  ## Detiene Supabase local
	$(SUPABASE) stop

db-reset:  ## Recrea la base desde cero aplicando todas las migraciones + seed
	$(SUPABASE) db reset

migrate:  ## Aplica las migraciones pendientes
	$(SUPABASE) migration up

seed:  ## Carga categorias del sistema y emisores conocidos
	uv run python -m app.cli seed

# ─── Calidad ─────────────────────────────────────────────────────────────────
lint:  ## ruff check
	uv run ruff check app tests scripts

fmt:  ## ruff format + fix
	uv run ruff format app tests scripts
	uv run ruff check --fix app tests scripts

type:  ## mypy
	uv run mypy app

test-unit:  ## Tests unitarios (sin DB, sin red)
	uv run pytest tests/unit -q

test:  ## Todos los tests menos los que pegan al LLM real
	uv run pytest -q -m "not llm"

test-rls:  ## El test que cierra el aislamiento entre usuarios
	uv run pytest tests/integration/test_rls_isolation.py -v

test-llm:  ## Accuracy real contra Gemini (CUESTA DINERO)
	uv run pytest -q -m llm

check: lint type test  ## Todo lo que corre CI

# ─── Frontend ────────────────────────────────────────────────────────────────
css:  ## Compila Tailwind en modo watch
	npx --yes tailwindcss -i ./static/css/input.css -o ./static/css/tailwind.css --watch

clean:
	rm -rf .mypy_cache .ruff_cache .pytest_cache htmlcov .coverage
