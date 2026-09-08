# Finance Consolidation

Seguimiento mensual de finanzas personales a partir de los resúmenes de tarjeta de crédito.
Subís el PDF, el sistema extrae las transacciones, vos revisás y corregís lo que el LLM erró,
confirmás, y el dashboard te muestra el mes.

Aplicación web privada, multi-usuario, donde **cada usuario ve solo sus propias finanzas**.

## Cómo funciona

El pipeline tiene dos etapas deliberadamente separadas:

```
PDF ──[etapa 1: pdfplumber]──> texto crudo en DB ──[etapa 2: Gemini]──> transacciones
      determinista, sin LLM                        re-corrible sin tocar el PDF
```

Separarlas permite iterar los prompts sobre el corpus real sin volver a procesar archivos, y
mejorar la categorización sin volver a extraer.

**No usa OCR.** Los resúmenes de homebanking son PDFs con capa de texto, así que se extraen
con `pdfplumber`: gratis, offline y sin errores de reconocimiento.

## Estado

| Fase | Qué incluye | Estado |
|---|---|---|
| 0 | Andamiaje, settings, logging con redacción de PII | ✅ |
| 1a | Esquema completo + RLS + test de aislamiento | ✅ |
| 1b | Auth: verificación de JWT, sesión, invitaciones | ⬜ |
| — | Dominio puro: montos, fechas, dedupe, cuotas | ✅ |
| 2 | Upload y extracción de texto (sin LLM) | ⬜ |
| 3 | Extracción con Gemini + validación | ⬜ |
| 4 | Pantalla de revisión y corrección | ⬜ |
| 5 | Enriquecimiento y dashboard | ⬜ |
| 6 | Suscripciones, devengado, export | ⬜ |

El plan completo está en `docs/plan.md`.

**182 tests en verde** (165 unitarios sin dependencias + 17 de aislamiento contra un
Postgres real). La Fase 1b está bloqueada hasta que exista un proyecto de Supabase:
necesita `SUPABASE_URL` y `SUPABASE_ANON_KEY`.

### Correr los tests de integración

Necesitan un Postgres de verdad — el aislamiento entre usuarios es un comportamiento
de Postgres y un mock no probaría nada:

```bash
docker run -d --name fc_test_pg -e POSTGRES_PASSWORD=postgres -p 55432:5432 postgres:15
export TEST_DATABASE_URL=postgresql://postgres:postgres@127.0.0.1:55432/postgres
make test-rls
```

## Puesta en marcha

Requiere Python 3.12+, [uv](https://docs.astral.sh/uv/) y Docker (para el Supabase local).

```bash
cp .env.example .env          # y completar los secretos
make install
make db-up                    # Supabase local en Docker
make db-reset                 # aplica migraciones + seed
make dev                      # http://localhost:8000
```

Generar el secreto de sesión:

```bash
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

## Comandos

```bash
make check       # lo que corre CI: ruff + mypy + tests
make test-rls    # el test de aislamiento entre usuarios
make test-llm    # accuracy real contra Gemini (CUESTA DINERO)
make help        # todos los targets
```

## Seguridad

El aislamiento entre usuarios es el requisito central del proyecto, no un detalle.

- El backend se conecta a Postgres con el rol **`app_runtime`, que no tiene `BYPASSRLS`**, y
  propaga el JWT del usuario por transacción con `set_config(..., is_local => true)`. Un query
  que olvide filtrar por `user_id` devuelve **0 filas**, no las filas de otro usuario.
- Todas las tablas tienen `force row level security`. Un test parametrizado sobre
  `information_schema` falla si alguien agrega una tabla sin políticas.
- La `service_role` key vive **solo** en `app/infra/supabase_admin.py`, con un inventario
  cerrado de usos (invitaciones, seed, reaper de jobs). Un lint falla si se importa desde otro
  módulo.

**Datos sensibles:** los PDFs reales van a `tests/fixtures/private/`, que está en `.gitignore`.
Para los tests se usan textos anonimizados (`make anonymize`) y PDFs sintéticos generados con
reportlab. `.gitignore` bloquea cualquier `.pdf` fuera de `tests/fixtures/pdfs/`.

## Costos

Del orden de **USD 3–6/mes**: Fly.io con una máquina siempre encendida (USD 3–5), Supabase
Free (el volumen cabe años; el riesgo es la pausa por inactividad, no el espacio) y Gemini
2.5 Flash en centavos al volumen de ~20 documentos por mes.
