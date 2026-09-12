# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Qué es esto

App web privada, multi-usuario, para consolidar finanzas personales a partir de resúmenes de
tarjeta de crédito en PDF. Pipeline en dos etapas deliberadamente separadas:

```
PDF ──[etapa 1: pdfplumber]──> texto crudo en DB ──[etapa 2: Gemini]──> transacciones
      determinista, sin LLM                        re-corrible sin tocar el PDF
```

No usa OCR: los PDFs de homebanking traen capa de texto. El plan completo de arquitectura y
decisiones de diseño está en `docs/plan.md` — consultarlo antes de proponer cambios estructurales
(elección de stack, modelo de jobs, esquema de auth, etc.), ya que casi todo tiene una razón
documentada de por qué se hizo así y qué alternativas se descartaron.

## Comandos

```bash
make install       # uv sync --extra dev
make dev           # servidor con reload en :8000
make db-up         # Supabase local (Docker)
make db-reset      # migraciones + seed desde cero
make check         # lo que corre CI: lint + type + test
make lint          # ruff check
make fmt           # ruff format + fix
make type          # mypy
make test-unit     # tests unitarios (sin DB, sin red)
make test          # todo menos @pytest.mark.llm
make test-rls      # test de aislamiento entre usuarios (necesita Postgres)
make test-llm      # accuracy real contra Gemini — CUESTA DINERO, no correr sin avisar
```

Un solo test: `uv run pytest tests/unit/test_dates.py::test_nombre -v`

Los tests de integración (`tests/integration/`) necesitan un Postgres real corriendo (el
aislamiento por RLS es un comportamiento de Postgres; un mock no probaría nada). Se saltean
solos si no hay `TEST_DATABASE_URL`:

```bash
docker run -d --name fc_test_pg -e POSTGRES_PASSWORD=postgres -p 55432:5432 postgres:15
export TEST_DATABASE_URL=postgresql://postgres:postgres@127.0.0.1:55432/postgres
```

CI corre dos jobs separados: `quality` (lint + mypy + unit) y `integration` (con Postgres de
servicio, aplica las mismas migraciones que producción antes de testear).

## Flujo de trabajo por fase

Al implementar una fase del plan (o cualquier bloque de trabajo equivalente en tamaño):

1. **Rama nueva por fase**, desde `main` — nunca commitear directo a `main` (además está
   protegida contra push directo en GitHub).
2. **El PR de esa rama detalla las tareas** que componen la fase en su descripción (una lista
   clara de qué se hizo, no un resumen genérico) — es lo que permite repartirlas y revisarlas
   por separado.
3. **Asignar cada tarea al modelo que corresponda según su dificultad**, no usar el mismo modelo
   para todo. Guía de referencia:

   | Dificultad | Ejemplos en este repo | Modelo |
   |---|---|---|
   | Baja | Templates HTML, texto de prompts, migraciones aditivas simples (agregar columna/índice), fixtures de test, formateo/lint, CRUD de un repositorio nuevo siguiendo un patrón ya existente | Haiku |
   | Media | Rutas web nuevas, servicios que orquestan repos existentes, tests de casos de uso, refactors dentro de una capa | Sonnet (default) |
   | Alta | Cualquier cosa en `app/domain/validation.py` o `app/domain/models.py` (cuadre financiero), políticas RLS y migraciones de seguridad, `app/security/*`, todo lo que toque el inventario de `service_role`, el algoritmo de merge de reparse, diseño de prompts del LLM | Opus |

   Ante la duda entre dos niveles, asignar el más alto — es más barato revisar una tarea sencilla
   resuelta por un modelo grande que corregir una de seguridad mal resuelta por uno chico.

4. **Antes de abrir el PR**, correr `make check` localmente (lo mismo que corre CI) para no
   quemar ciclos de revisión en fallas mecánicas de lint/type/test.
5. **Cualquier tarea que toque RLS, `app/security/*`, `service_role` o la lógica de cuadre**
   pasa por el skill `security-review` o `/code-review` antes de mergear, sin importar qué modelo
   la resolvió — es la categoría de bug que este proyecto no se puede permitir que pase
   desapercibida.
6. **Checklist de tareas con formato fijo en la descripción del PR**, no prosa libre — una línea
   por tarea: `- [ ] <tarea> — <modelo> — <archivo(s)>`. Es lo que hace que "qué modelo resolvió
   qué" (regla 3) quede trazable y no dependa de que alguien se acuerde de anotarlo.
7. **Scope disjunto entre agentes que trabajan en paralelo sobre la misma fase**: cada uno toca
   un conjunto de archivos que no se pisa con el de otro. Prestar atención especial a los
   archivos "bisagra" que cruzan varias fases (`app/main.py`, `app/jobs/handlers.py`) — si dos
   tareas necesitan tocarlos, coordinarlo explícitamente en vez de dejar que cada agente edite por
   su cuenta.
8. **El agente principal arma los commits finales de la fase**, nunca cada sub-agente por
   separado. Agrupar y validar `make check` sobre el resultado combinado antes de pushear —
   agrupar despues de que el trabajo está hecho es más simple que descoordinar commits en el
   camino.
9. **`make test-llm` cuesta dinero real**: si varias tareas de una fase lo necesitan, fijar de
   antemano cuántas corridas están aprobadas antes de lanzar agentes en paralelo, para que no se
   disparen gastos sin que quede explícitamente autorizado.
10. **Un solo lugar de verdad para el estado de la fase** (el PR, o un archivo de tracking) — el
    estado de qué tarea está pendiente/en curso/hecha no se duplica en la memoria de cada agente,
    para que dos agentes no terminen resolviendo la misma tarea sin saberlo.

## Arquitectura

### Aislamiento por RLS, no por código de aplicación

Esto es el requisito central del proyecto, no un detalle. El backend se conecta a Postgres con
el rol `app_runtime`, que **no tiene BYPASSRLS**, y propaga el JWT del usuario por transacción
con `set_config(..., is_local => true)` (`app.infra.db.user_tx()` / `system_tx()`). Un query que
olvide filtrar por `user_id` devuelve **0 filas**, no las filas de otro usuario. Todas las tablas
tienen `force row level security`.

Consecuencias prácticas para tocar código acá:

- **Nunca** usar `SET SESSION` o `set_config` con `is_local` falso (o el default de 2 argumentos)
  en código ejecutable — sobrevive al `COMMIT` y, con el pool de conexiones reusándose, la
  siguiente request hereda la identidad del usuario anterior. Hay un hook de pre-commit
  (`scripts/check_local_config.py`) que bloquea esto.
- La `service_role` key de Supabase bypassea RLS. Su uso está acotado por un inventario cerrado a
  **un solo módulo**, `app/infra/supabase_admin.py`, y solo para tres casos: invitaciones/alta de
  usuarios, seed de categorías del sistema, y el reaper de jobs huérfanos. Prohibido en cualquier
  camino que toque `documents`, `document_texts`, `statements`, `transactions` o `merchants`. Hay
  un hook de pre-commit (`scripts/check_service_role.py`) que lo hace cumplir.
- Los handlers de jobs corren con `system_tx(job.user_id)`: mismo rol `app_runtime`, RLS activo,
  con la identidad del dueño del job sintetizada. Las únicas dos operaciones que cruzan usuarios
  (tomar el próximo job de la cola, reciclar huérfanos) son funciones `security definer` con
  `execute` otorgado solo a `app_runtime`, nunca a `authenticated`.

### Auth

`cookie httpOnly firmada -> sesión -> access token verificado -> claims -> Postgres`. Dos
verificaciones a propósito: la firma de la cookie prueba que la sesión la emitió este backend; la
verificación del JWT contra el JWKS del proyecto prueba que los claims los emitió Supabase. El
access token nunca es accesible desde JavaScript. CSRF cubierto con `SameSite=Lax` + token firmado
con el id de sesión adentro. No hay registro abierto: las cuentas se crean desde
`/admin/invitations` (`role = admin` solo habilita invitar, no da acceso a datos de nadie — eso lo
decide RLS).

### Etapa 2 (extracción con LLM)

Dos llamadas al modelo, no una: la cabecera (seis números) y las transacciones (tabla larga)
tienen precisión muy distinta, y pedirlas juntas empeora las dos. Las transacciones salen en
fragmentos de una o dos páginas, en paralelo, con solapamiento para no perder líneas partidas
entre páginas.

Los montos viajan como `string` en el JSON del LLM y se convierten a `Decimal` una sola vez, en
el borde (`app/schemas/llm.py`) — un `float` intermedio rompe el cuadre sobre cientos de
transacciones por error de redondeo.

El cuadre se valida contra el **movimiento neto del período** (`total_due − previous_balance`),
no contra `new_charges` (que en la mayoría de los emisores no incluye los pagos). Hay ocho
verificaciones en `app/domain/validation.py`; ver ahí antes de tocar la lógica de cuadre. Lo que
no cuadra nunca se descarta: se guarda con `needs_review` y hay **un solo** reintento automático
con el delta exacto en la mano.

Prompts versionados en `app/llm/prompts/*.md`, referenciados por nombre desde el código —
**editar un prompt en producción es crear `_v2`, nunca modificar el que ya está en uso**, porque
cada fila de `extraction_runs` guarda con qué versión se generó.

### Reprocesar sin perder trabajo manual

Re-correr la etapa 2 sobre un documento ya revisado no pisa las correcciones humanas. La regla es
por estado de cada transacción existente: `pending` se actualiza (el modelo manda), `confirmed`/
`edited` no se toca (queda en `conflicts[]`), `rejected` no se reinserta. Lo que lo hace posible es
que **editar una transacción no recalcula su `dedupe_key`** — esa clave es la identidad de la
línea del modelo de la que salió, no un hash del contenido actual; si se recalculara, un reparse
no reconocería la fila corregida y la duplicaría.

### Cola de jobs

Vive en el mismo proceso que la web (loop en el `lifespan` + polling cada 5s como red de
seguridad). La toma de trabajo usa `for update skip locked` desde el día uno. El **encolado es
siempre a demanda** — solo tres sitios llaman a `enqueue()` (`app/services/ingest.py` al subir un
documento, `app/web/documents.py` en el reparse, el handler de confirmación en
`app/services/review.py` para el enrich) — no hay ningún job programado por horario. El **reaper**
recicla jobs `running` con `locked_at` viejo, necesario porque un redeploy mata jobs a mitad de
camino.

Hay una migración futura documentada pero **no implementada** en `docs/plan.md` sección 15, para
sacar el runner de un host siempre encendido usando `pg_cron`/`pg_net` de Supabase — no asumir que
ya existe.

### Capas

`app/domain/` es puro (sin I/O), con mypy strict junto con `app/llm/` y `app/schemas/`.
`app/repositories/` recibe siempre una conexión ya scopeada (`user_tx`/`system_tx`) — nunca abre
la suya. `app/services/` orquesta repos + dominio + LLM. `app/web/` son las rutas Jinja2+HTMX
(server-rendered, sin SPA — elegido a propósito para que el token de sesión nunca tenga que vivir
en el browser). `app/jobs/` es el runner/handlers/reaper de la cola.

## Testing

El 90% de los tests no toca PDFs reales: la etapa 2 consume `document_texts`, así que los
fixtures son texto. Los tests reemplazan el LLM por `app/llm/fake.py`, que replaya respuestas
grabadas — el resto (validación, persistencia, merge) es código de producción real, sin mocks del
dominio. Los PDFs sintéticos (`tests/fixtures/pdfs.py`, con reportlab) cubren casos de la etapa 1
(cifrados, escaneados, con JavaScript) sin depender de resúmenes reales.

**Datos sensibles**: los PDFs reales van a `tests/fixtures/private/`, en `.gitignore`. Un hook de
pre-commit (`scripts/check_no_pdfs.py`) bloquea cualquier `.pdf` fuera de `tests/fixtures/pdfs/`
(los sintéticos). Nunca commitear un resumen de tarjeta real, ni siquiera de prueba.

Los tests con `@pytest.mark.llm` pegan contra la API real de Gemini y **cuestan dinero** — no
correrlos salvo que se pida explícitamente; están excluidos de CI.
