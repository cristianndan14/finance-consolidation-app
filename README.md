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
| 1b | Auth: verificación de JWT, sesión, invitaciones | ✅ |
| — | Dominio puro: montos, fechas, dedupe, cuotas | ✅ |
| 2 | Upload y extracción de texto (sin LLM) | ✅ |
| 3 | Extracción con Gemini + validación | ✅ |
| 4 | Pantalla de revisión y corrección | ✅ |
| 5 | Enriquecimiento y dashboard | ⬜ |
| 6 | Suscripciones, devengado, export | ⬜ |

El plan completo está en `docs/plan.md`.

**494 tests en verde**: 387 unitarios sin red ni base —verificación de JWT, cookie de
sesión, CSRF, el flujo de login/logout contra la app real por `ASGITransport`, la etapa 1
sobre PDFs sintéticos (cifrados, escaneados, con JavaScript), los ocho checks de validación
y el borde de conversión de lo que devuelve el LLM— y 107 contra un Postgres real:
aislamiento entre usuarios, invitaciones, el pipeline de ingesta, la etapa 2 completa con
su reconciliación y la pantalla de revisión de punta a punta. Los de integración se saltean solos si no hay base levantada (ver abajo).

Los tests de la etapa 2 no le pagan a nadie: el adapter del LLM se reemplaza por
`app/llm/fake.py`, que replaya respuestas grabadas. Lo demás es el código de producción.

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

## La extracción

La etapa 2 son dos llamadas al modelo y ocho verificaciones.

**Dos llamadas, no una.** La cabecera (seis números) y las transacciones (una tabla larga) se
cumplen con precisión muy distinta: pedirlas juntas empeora las dos. Además la cabecera da la
fecha de cierre, que es lo que permite resolver un `15/03` sin año. Las transacciones salen en
fragmentos de una o dos páginas, en paralelo, con 300 caracteres de solapamiento para no
perder una línea partida entre páginas.

**Los montos viajan como string.** Un JSON `number` llega a Python como `float`, y sobre
doscientas transacciones el error de centavos rompe justo la verificación principal. El schema
pide `"1234.56"` y Pydantic lo convierte a `Decimal` una sola vez, en el borde.

**El cuadre va contra el movimiento neto del período** (`total_due − previous_balance`). La
primera versión comparaba contra `new_charges` y sobre resúmenes reales dio **0% de cuadre**:
los emisores declaran `new_charges` como los consumos *sin los pagos*, mientras que
`sum(amount × direction)` incluye el pago en negativo, así que la diferencia es del tamaño del
pago en todo resumen donde hayas pagado algo. El movimiento neto sí es una identidad de la
cuenta y no depende de cómo cada emisor reparta la información.

Las otras siete verificaciones cubren: la identidad de la cabecera (que distingue "faltan
transacciones" de "la cabecera está mal leída"), el anclaje de cada transacción en una línea
literal del PDF (anti-alucinación), el conteo de líneas candidatas (truncamiento), la
coherencia de fechas (el año mal inferido en el cruce diciembre/enero), la coherencia de las
cuotas contra el total informado, los duplicados del solapamiento y la cobertura de páginas.

**Nada se descarta.** Lo que no cuadra se guarda con `needs_review` y el delta anotado, y hay
**un solo** reintento automático con la diferencia exacta en la mano (`"faltan 12.480,50
ARS"`). Si esa pasada deja el cuadre peor, se descarta y quedan los números originales.

### Accuracy medida

Sobre **5 resúmenes reales de 4 emisores** (Galicia Visa, Galicia Mastercard, BNA Visa,
Mercado Pago, Naranja X): **6 de 6 secciones cuadran con diferencia 0,00**. El hito de la
fase —≥80% de cuadre sobre 4+ emisores— está cumplido. Costo total de la corrida: USD 0,034.

Llegar ahí requirió dos rondas de prompts y arreglar cuatro bugs que solo aparecen contra
documentos de verdad: fechas con el mes en texto (`06-Ago-26`), el signo perdido en un
`saldo a favor`, el recorte del texto que le escondía al modelo la página con el saldo
anterior, y el criterio de cuadre contra `new_charges` en lugar del movimiento neto.

### Pendiente conocido

Mercado Pago y Naranja X no entregan la **fecha de cierre**: está en prosa (`Cierre actual
18 de agosto`, `El resumen actual cerró el 27/07`) y el prompt v2 no la encuentra. El sistema
cae a la fecha de subida, así que esos resúmenes quedan imputados al mes equivocado. No
afecta el cuadre —las transacciones traen su año explícito— pero sí la imputación mensual del
dashboard. Pendiente para una v3 del prompt de cabecera.

Lo mismo con `header_identity_mismatch` en Visa Galicia y Mercado Pago: los totales de la
cabecera no cierran entre sí aunque las transacciones sí cuadran, o sea que hay algún campo
del header mal leído que no afecta la verificación principal.

### Iterar prompts

Los prompts viven versionados en `app/llm/prompts/*.md` y cada fila de `extraction_runs`
guarda con cuál se generó. Editar un prompt en producción es crear `_v2`, no modificar `_v1`.

```bash
uv run python -m app.cli prompts                      # versiones disponibles
uv run python -m app.cli parse <document_id> --user <user_id>   # dry-run: no escribe nada
uv run python -m app.cli parse <document_id> --user <user_id>     --transactions-prompt parse_transactions_v2       # comparar dos versiones
```

El `--dry-run` es el default y es la parte importante: llama al modelo, valida e imprime el
cuadre por moneda sin tocar la base, así que se puede probar contra resúmenes que ya
revisaste. Con `--write` persiste.

Desde la UI, `POST /documents/{id}/reparse` hace lo mismo: relee `document_texts` sin volver a
bajar el PDF.

### Reprocesar sin perder lo corregido

Re-correr la etapa 2 **no pisa el trabajo manual**. La regla es por estado de cada fila:

| En la base | Qué hace la re-corrida |
|---|---|
| no existe | inserta como `pending` |
| `pending` | actualiza (el modelo manda) |
| `confirmed` / `edited` | **no toca**; queda en `conflicts[]` |
| `rejected` | no reinserta |
| sobra y es `pending` | borra |
| sobra y la tocaste vos | conserva; queda en `orphans[]` |

En una frase: el modelo puede pisar lo que escribió el modelo.

## La revisión

`/statements/{id}/review` es la pantalla que convierte "el sistema extrajo transacciones" en
"los números del dashboard son ciertos". Muestra la tabla editable con la **línea literal del
PDF debajo de cada fila** —verificar un dato es mirar dos renglones, no abrir el PDF en otra
ventana— y arriba el delta de cuadre, recalculado desde la base después de cada cambio.

Lo que se puede hacer: corregir cualquier campo inline, confirmar o descartar de a una o en
bloque, agregar a mano una transacción que el modelo no vio, y dividir una línea en varias
(el super donde también cargaste nafta). Las filas que el check de anclaje marcó como posibles
invenciones aparecen destacadas.

Tres reglas la sostienen:

- **Confirmar exige haber mirado todo.** El botón está bloqueado mientras quede una
  transacción en `pending`: confirmar es afirmar que lo revisaste, y con pendientes esa
  afirmación sería falsa.
- **Si no cuadra, la diferencia se acepta explícitamente** y queda guardada en
  `accepted_delta`. Dentro de tres meses se puede saber que ese mes cerró con 340 pesos sin
  explicar y que fue una decisión, no un error.
- **Dividir no cambia el total.** Las partes tienen que sumar el original; la línea original
  queda descartada, no borrada, con su `source_line` a la vista.

Cada corrección va a `transaction_revisions` con el valor anterior. Eso permite medir la
accuracy real del modelo por emisor y, en la Fase 6, alimentar ejemplos few-shot con las
correcciones reales.

**Lo corregido a mano sobrevive a un reparse**, y el detalle que lo hace posible es
contraintuitivo: editar **no** recalcula la `dedupe_key`. Esa clave no es un hash del
contenido actual de la fila, es la identidad de la línea del modelo de la que salió. Si se
recalculara, el próximo reparse no reconocería la fila corregida: insertaría de nuevo la
versión mal leída y dejaría la corregida como huérfana, con el mes inflado y sin ninguna
señal. Manteniéndola, el reparse la matchea, ve que está `edited` y no la toca.

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

### La cola de trabajos

El runner vive en el mismo proceso que la web: un loop que se despierta cuando una ruta
encola algo y, además, hace polling cada 5 s como red de seguridad (para los reintentos con
backoff y para lo que devuelva el reaper). La toma de trabajo usa `for update skip locked`
desde el día uno, así que mover el runner a un proceso aparte no requiere cambiar lógica.

Los handlers corren con `system_tx(job.user_id)`: mismo rol `app_runtime`, RLS activo, con
la identidad del dueño del job sintetizada. Un bug en un handler no puede escribir en los
datos de otro. Las **dos únicas operaciones que cruzan usuarios** —tomar el próximo job y
reciclar los huérfanos— son funciones `security definer` con `execute` otorgado solo a
`app_runtime`, nunca a `authenticated`.

### Cómo se autentica una request

```
cookie httpOnly firmada  ->  sesión  ->  access token verificado  ->  claims  ->  Postgres
```

Dos verificaciones seguidas, a propósito: la firma de la cookie prueba que la sesión la
emitió este backend, y la verificación del JWT contra el JWKS del proyecto prueba que los
claims los emitió Supabase. El access token nunca queda accesible desde JavaScript, así
que un XSS no puede exfiltrarlo; el CSRF se cubre con `SameSite=Lax` más un token firmado
que lleva adentro el id de la sesión.

No hay registro abierto: las cuentas se crean desde `/admin/invitations`, y `role = admin`
solo habilita invitar — **no** da acceso a los datos de nadie, porque eso lo decide RLS.

**Datos sensibles:** los PDFs reales van a `tests/fixtures/private/`, que está en `.gitignore`.
Para los tests se usan textos anonimizados (`make anonymize`) y PDFs sintéticos generados con
reportlab. `.gitignore` bloquea cualquier `.pdf` fuera de `tests/fixtures/pdfs/`.

## Costos

Del orden de **USD 3–6/mes**: Fly.io con una máquina siempre encendida (USD 3–5), Supabase
Free (el volumen cabe años; el riesgo es la pausa por inactividad, no el espacio) y Gemini
2.5 Flash en centavos al volumen de ~20 documentos por mes.
