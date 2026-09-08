# Finance Consolidation — Plan de implementación

## Context

Querés hacer seguimiento mensual de tus finanzas personales a partir de los resúmenes de
tus tarjetas de crédito. Hoy no existe nada: `C:\personal-projects\finance_consolidation`
está vacío.

La idea original era un pipeline de scripts locales (OCR de Google → SQLite → traducción a
tabla → dashboard). Durante la conversación el objetivo se amplió y eso cambió el diseño en
dos puntos importantes:

1. **No hace falta OCR.** Tus resúmenes son PDFs digitales con capa de texto, así que se
   extrae con `pdfplumber`: gratis, offline, sin errores de reconocimiento. El OCR queda
   como fallback futuro para el caso de un escaneo.
2. **Es una aplicación web multi-usuario, no scripts locales.** Querés subir los PDFs desde
   la web, en una página privada, y dar acceso a 2–5 personas de confianza donde **cada una
   ve solo sus propias finanzas**. Eso convierte el aislamiento entre usuarios en el
   requisito de seguridad central del proyecto, no en un detalle.

### Decisiones cerradas

| Tema | Decisión |
|---|---|
| Input | PDFs digitales de resúmenes de tarjeta de crédito (sin OCR) |
| Emisores | 4+, mezcla de bancos tradicionales AR y fintechs → extracción **genérica vía LLM**, no regex por banco |
| LLM | Gemini 2.5 Flash, detrás de un adapter para poder cambiar a Mistral |
| Monedas | ARS + USD, con tipo de cambio mensual para consolidar en moneda base |
| Cuotas | Modelo de **flujo de caja** (la cuota impacta el mes en que se paga), persistiendo total y N/M para reconstruir la vista devengada |
| Alcance v1 | Solo tarjetas de crédito |
| Stack | FastAPI + Supabase (Postgres + Auth + Storage), deploy en Fly.io |
| Multi-tenancy | Aislamiento estricto por Row Level Security de Postgres |
| Usuarios | 2–5, por invitación. Sin registro abierto |
| Frontend | Jinja2 + HTMX + Alpine + Tailwind + ECharts (ver §7) |

### Objetivo de resultado

Subís el PDF del resumen, el sistema extrae las transacciones, vos revisás y corregís lo que
el LLM erró, confirmás, y el dashboard te muestra el mes con categorías, cuotas comprometidas
a futuro y comparación contra el mes anterior. Números en los que podés confiar porque los
validaste.

---

## 1. Aislamiento entre usuarios (la parte crítica — se hace primero)

Con datos financieros de familiares, "los usuarios están separados porque escribí bien el
`WHERE`" no alcanza. El motor de base de datos tiene que ser la última línea de defensa.

**El problema:** si el backend se conecta con la `service_role` key de Supabase, Postgres ve
un rol con `BYPASSRLS` y las políticas no se evalúan nunca. Un `WHERE user_id` olvidado =
filtración.

**La solución:** un rol `app_runtime` sin `BYPASSRLS`, y propagación del JWT a Postgres por
transacción.

```sql
create role app_runtime login password :'app_runtime_password' noinherit;
grant usage on schema app to app_runtime;
grant authenticated to app_runtime;   -- puede "convertirse" en el rol al que apuntan las políticas
```

```python
# app/infra/db.py  — ÚNICO dueño del pool; no importable desde otros módulos
@asynccontextmanager
async def user_tx(pool, claims: dict) -> AsyncIterator[AsyncConnection]:
    async with pool.begin() as conn:
        await conn.exec_driver_sql(
            "select set_config('role','authenticated',true),"
            "       set_config('request.jwt.claims',$1,true)",
            (json.dumps(claims),),
        )
        yield conn
```

Puntos deliberados:

- `is_local => true` → scope de transacción, se descarta en el `COMMIT`. Seguro con pooling.
  **Prohibido** `SET SESSION` o `set_config(..., false)`; un lint lo verifica por grep.
- `auth.uid()` de Supabase lee `request.jwt.claims`, así que funciona igual por conexión
  directa que por PostgREST.
- Con el rol activo en `authenticated`, un `WHERE` olvidado devuelve **0 filas**, no las del
  vecino. El fallo es seguro por default.
- El JWT se **verifica** (JWKS cacheado, TTL 1h; valida `exp`, `aud`, `iss`) antes de armar
  los claims. Solo se propagan `sub`, `role`, `email`, `session_id`. Nunca `user_metadata`,
  que el usuario puede editar → jamás usarla en una política.
- **Los jobs de background también pasan por RLS**: el worker no tiene JWT, pero sintetiza
  `{"sub": job.user_id, "role": "authenticated"}` y usa el mismo `app_runtime`. Un bug en el
  worker no puede escribir en datos de otro usuario. Cuesta cero.

**Inventario cerrado de `service_role`.** Único módulo autorizado: `app/infra/supabase_admin.py`.
Usos permitidos: (1) invitaciones y alta de usuarios — solo toca `auth.users`; (2) seed de
categorías del sistema y tipos de cambio globales, en migración/CLI; (3) reaper de jobs
huérfanos — solo `processing_jobs.status`. Prohibido tocar `documents`, `document_texts`,
`statements`, `transactions`, `merchants`. Un test parsea imports y falla si aparece fuera de
la lista blanca. Las migraciones usan el usuario `postgres`, disponible solo en local/CI.

**El test que cierra el tema:** `tests/integration/test_rls_isolation.py` crea usuarios A y B
e intenta `select`/`insert`/`update`/`delete` cruzado **para cada tabla**, parametrizado sobre
`information_schema` → si alguien agrega una tabla sin RLS, el test falla. Más asserts de
`relrowsecurity` y `relforcerowsecurity` en todas las tablas de `app`.

---

## 2. Esquema de base de datos

Todo en el schema `app`. **`user_id` denormalizado en todas las tablas**, incluso las hijas.
Cuesta una columna redundante; gana políticas RLS de una línea (sin `EXISTS` por fila),
índices compuestos eficientes, y **FKs compuestas que hacen estructuralmente imposible cruzar
dueños**.

### Tablas

- **`profiles`** — espejo de `auth.users`: `email`, `display_name`, `base_currency`,
  `role ∈ (member, admin)`.
- **`invitations`** — `email`, `invited_by`, `expires_at`, `accepted_at`.
- **`cards`** — `issuer_slug`, `issuer_name`, `product_name`, `brand`, `last4`, `closing_day`,
  `due_day`. `unique (user_id, id)` para habilitar las FKs compuestas.

**Etapa 1 (crudo):**

- **`documents`** — `storage_path`, `original_filename`, `byte_size`, `sha256`, `page_count`,
  `was_encrypted`, `status ∈ (uploaded, text_extracted, parsed, needs_review, confirmed, failed)`.
  **`unique (user_id, sha256)` = idempotencia de archivo.**
- **`document_texts`** — `extractor`, `extractor_version`, `page_texts jsonb`, `full_text`,
  `char_count`, `has_text_layer`. Único por `(user_id, document_id, extractor, extractor_version)`
  → se puede mejorar la extracción de texto sin perder la anterior.

**Etapa 2 (estructurado):**

- **`extraction_runs`** — `stage ∈ (parse, enrich, repair)`, `provider`, `model`,
  `prompt_version`, `status`, tokens, `cost_usd`, `raw_response jsonb`, `validation jsonb`.
  Guardar el crudo del LLM permite reprocesar sin volver a llamar.
- **`statements`** — **uno por `(documento, moneda)`**, porque un PDF argentino trae sección
  ARS y sección USD. `period_year/month`, `closing_date`, `due_date`, `previous_balance`,
  `payments_credits`, **`new_charges`** (contra esto se valida la suma), `total_due`,
  `minimum_payment`, `status ∈ (draft, needs_review, confirmed)`.
- **`transactions`** — el corazón:
  - `posted_date`, `transaction_date`, `description_raw`, **`source_line`** (línea literal del
    PDF), `source_page`
  - `amount numeric(14,2)` **siempre positivo** + `direction ∈ (1,-1)` separado
  - `kind ∈ (charge, installment, payment, refund, interest, fee, tax, fx_charge, adjustment, unknown)`
  - cuotas: `installment_number`, `installment_total`, `purchase_total_amount`,
    `purchase_date`, `installment_group_key`
  - `merchant_id`, `category_id`, `is_recurring`, `subscription_id`
  - `dedupe_key char(64)`, `occurrence_index`, `confidence`,
    `review_status ∈ (pending, confirmed, edited, rejected)`
  - **`unique (user_id, statement_id, dedupe_key, occurrence_index)`**

- **`categories`** (`user_id NULL` = del sistema), **`merchants`**, **`merchant_aliases`**
  (cache de normalización: evita re-preguntarle al LLM lo mismo cada mes),
  **`subscriptions`**, **`fx_rates`** (`user_id NULL` = global, por mes y par de monedas),
  **`processing_jobs`**, **`transaction_revisions`** (bitácora de correcciones del usuario →
  few-shot y métricas de accuracy), **`llm_usage`** (control de costo por usuario/mes).

### Sobre `dedupe_key` + `occurrence_index`

`dedupe_key = sha256(normalize(description) | posted_date | amount | currency | installment_number | installment_total)`.

Pero dos cafés idénticos el mismo día en el mismo comercio son legítimos y no deben
colapsarse. Por eso el `occurrence_index`: al insertar un lote se agrupa por `dedupe_key` y se
enumera `0..n-1`. La unicidad protege contra reimportar el mismo resumen (mismo lote → mismos
índices → conflicto) **sin destruir duplicados reales**.

### Índices

```sql
create index tx_user_date_idx on app.transactions (user_id, posted_date desc);
create index tx_cat_date_idx  on app.transactions (user_id, category_id, posted_date);
create index tx_review_idx    on app.transactions (user_id, review_status) where review_status='pending';
create index tx_instgroup_idx on app.transactions (user_id, installment_group_key) where installment_group_key is not null;
create unique index jobs_active_uq on app.processing_jobs (user_id, document_id, job_type)
  where status in ('queued','running');   -- encolar dos veces = no-op, no doble gasto de LLM
```

### Patrón RLS

```sql
alter table app.transactions enable row level security;
alter table app.transactions force row level security;   -- aplica incluso al owner de la tabla
create policy tx_owner on app.transactions for all to authenticated
  using (user_id = auth.uid()) with check (user_id = auth.uid());
grant select, insert, update, delete on app.transactions to authenticated;
```

Tablas con filas del sistema (`categories`, `fx_rates`): lectura `user_id is null or user_id = auth.uid()`,
escritura solo `user_id = auth.uid()`. En `profiles`, el `with check` del update impide
autoasignarse `role='admin'`. `user_id` con `default auth.uid()` + trigger `before insert` que
lo fuerza.

### Vistas (con `security_invoker = true` → heredan RLS)

`v_tx_enriched`, `v_monthly_cashflow` (por año/mes/categoría/moneda),
`v_monthly_base` (convertido a moneda base vía `fx_rates`), `v_accrual` (devengado),
`v_installment_forward` (cuotas futuras comprometidas).

---

## 3. Las dos etapas

### Etapa 1 — documento → texto (determinista, sin LLM)

`POST /api/documents` (multipart: `file`, `card_id?`, `pdf_password?`)

1. Leer bytes (máx 20 MB), validar magic `%PDF-`.
2. `sha256`. Si existe `(user_id, sha256)` → **200 con `already_imported: true`**, no error.
3. Si está encriptado, intentar con `pdf_password`; si falla, `409 password_required`.
   **No es un caso raro**: Galicia, Santander y BBVA mandan los resúmenes protegidos con
   DNI/CUIT. La contraseña **no se persiste**; se guarda el PDF desencriptado con
   `was_encrypted = true`.
4. Subir a Storage `{user_id}/{yyyy}/{sha256}.pdf` con el token del usuario.
5. Insertar `documents`, encolar `extract_text`, responder `202`.

El job `extract_text` guarda **tres representaciones por página**: `text_layout`
(`extract_text(layout=True)`, preserva columnas — esencial en tablas de resúmenes),
`text_flow` (orden de lectura) y `tables` (`extract_tables()`). Guardar las tres es
baratísimo y le permite a la etapa 2 usar la que mejor funcione por emisor sin reabrir el PDF.
Si `char_count / page_count < 200` → `has_text_layer = false`, documento a `failed` con
`reason: no_text_layer`.

### Etapa 2 — texto → transacciones (re-corrible, nunca toca el PDF)

`POST /api/documents/{id}/reparse?prompt_version=&model=` relee de `document_texts`. Esto
permite iterar prompts sobre el corpus real sin costo de I/O.

1. Cargar `document_texts`, crear `extraction_runs`.
2. **Llamada A — cabecera**: primera + última página (donde están los totales) → objeto
   `statement` por moneda. Schema chico = alta precisión.
3. **Llamada B — transacciones**: chunking por página con solapamiento de 300 caracteres,
   `asyncio.gather` con semáforo de 3.
4. Merge de chunks, dedupe para eliminar las repeticiones del solapamiento.
5. Validación (§5).
6. Persistencia con merge (abajo).
7. Encolar `enrich` — **separado**, para que mejorar la categorización no requiera re-extraer.

### Reconciliación en re-corridas

Re-correr la etapa 2 **no debe borrar el trabajo manual**:

```
por cada tx nueva, buscar por (statement_id, dedupe_key, occurrence_index):
  no existe                         -> INSERT (pending)
  existe y pending                  -> UPDATE completo
  existe y confirmed/edited         -> NO tocar; registrar en validation.conflicts[]
  existe y rejected                 -> NO reinsertar
existentes que el run nuevo no produjo:
  pending -> DELETE ;  otro -> conservar y marcar validation.orphans[]
```

Todo en una transacción. El run anterior pasa a `superseded`.

---

## 4. Adapter de LLM y JSON schema

```python
# app/llm/ports.py
class LLMExtractor(Protocol):
    async def extract_statement_header(self, *, text, hints, prompt_version) -> LLMResult[StatementHeaderPayload]: ...
    async def extract_transactions(self, *, text_chunk, chunk_index, header, prompt_version) -> LLMResult[TransactionsPayload]: ...
    async def normalize_merchants(self, *, raw_descriptions, known, category_slugs) -> LLMResult[list[MerchantMapping]]: ...
```

`LLMResult` lleva `data`, `raw`, `model`, `prompt_version`, tokens, `cost_usd`, `latency_ms`.
Implementaciones: `gemini.py` (`google-genai`, `response_schema`), `mistral.py` (stub),
`fake.py` (cassettes para tests). Prompts versionados en `app/llm/prompts/*.md` y
referenciados por `prompt_version` para poder auditar qué versión produjo cada dato.

Schema de transacciones, con las decisiones que importan:

```json
{"transactions": {"type": "array", "items": {"type": "object",
  "required": ["posted_date","description_raw","amount","currency","direction","kind","source_line"],
  "properties": {
    "posted_date":     {"type":"string","description":"ISO. Si solo hay DD/MM, inferir el año del período."},
    "description_raw": {"type":"string","description":"Literal, sin limpiar"},
    "source_line":     {"type":"string","description":"La línea completa del texto de la que salió"},
    "amount":          {"type":"string","pattern":"^\\d{1,12}\\.\\d{2}$",
                        "description":"Positivo, punto decimal. '1.234,56' -> '1234.56'"},
    "currency":        {"enum":["ARS","USD"]},
    "direction":       {"type":"integer","enum":[1,-1],"description":"1 = aumenta deuda. -1 = pago/crédito/devolución."},
    "kind":            {"enum":["charge","installment","payment","refund","interest","fee","tax","fx_charge","adjustment","unknown"]},
    "installment_number": {"type":"integer"}, "installment_total": {"type":"integer"},
    "purchase_total_amount": {"type":"string","pattern":"^\\d{1,12}\\.\\d{2}$"},
    "confidence":      {"type":"number"}}}},
 "warnings": {"type":"array","items":{"type":"string"}}}
```

- **Montos como string con regex, no `number`.** Los JSON numbers llegan a Python como `float`
  → error de centavos. El string se convierte a `Decimal` en el boundary de Pydantic, y el
  regex evita el formato `es-AR`.
- **`direction` separado del signo.** Los resúmenes argentinos marcan crédito con sufijos
  inconsistentes (`CR`, `-`, `H`); pedir un entero explícito es mucho más robusto.
- **`source_line` obligatorio.** Es el ancla anti-alucinación: se verifica por similitud que
  la línea exista en el texto de entrada. Y la pantalla de revisión la muestra al lado del dato.
- **`kind` con enum cerrado.** Impuestos (IVA 21%, percepción RG 4815, IIBB, sellos) son ~30%
  de las líneas de un resumen argentino y no son consumo. Separarlos desde la extracción evita
  ensuciar el dashboard.
- **Nada de `merchant`/`category` acá.** Extracción y enriquecimiento son llamadas distintas.
- **`warnings[]`**: canal para que el modelo diga "esta página parece cortada" en lugar de inventar.

Header, análogo: `statements: [{currency, closing_date, due_date, previous_balance,
payments_credits, new_charges, total_due, minimum_payment, card_last4, issuer_name}]` —
array, por las dos monedas.

Config: `temperature=0` y `thinking_budget=0` para el parse (es transcripción, no
razonamiento: más barato y determinista); thinking sí en el enrich.

---

## 5. Validación de la extracción

`app/domain/validation.py` → `ValidationReport` guardado en `extraction_runs.validation`,
determina `statements.status`. Por moneda:

1. **Cuadre de saldo** (el principal): `sum(amount * direction)` vs **`new_charges`**, no vs
   `total_due` — `total_due` incluye el saldo anterior y validar contra él es el error clásico.
   Tolerancia `max(0.5%, 50 ARS / 1 USD)`.
2. **Identidad del header**: `previous_balance - payments + new_charges ≈ total_due`. Si falla,
   el header está mal leído, no las transacciones.
3. **Anclaje en el texto**: cada `source_line` debe existir en `full_text` con similitud ≥ 0.85.
   Las que no → `confidence = 0.2` y flag `hallucination_suspect`. Atrapa transacciones
   inventadas de forma casi determinista.
4. **Conteo de líneas candidatas**: regex barata que cuenta líneas con `fecha + monto`. Si el
   LLM devolvió < 80% → `possible_truncation`.
5. **Coherencia de fechas**: `posted_date` en `[closing_date - 45d, closing_date + 5d]`.
   Atrapa años mal inferidos (diciembre/enero es el que más falla).
6. **Cuotas**: `1 ≤ n ≤ total ≤ 120`; si hay `purchase_total_amount`, `amount ≈ total / n`
   con 15% de tolerancia (hay interés incluido).
7. **Duplicados**: `dedupe_key` repetida > 2 veces → solapamiento mal resuelto.
8. **Cobertura de páginas**: toda página con líneas candidatas debe aparecer en algún `source_page`.

**Qué pasa cuando no cuadra:**

- **Nunca se descarta la extracción.** Se persiste con `status='needs_review'`.
- **Un solo reintento automático** (`stage='repair'`): se le manda el texto + lo extraído + el
  delta exacto (`"faltan 12.480,50 ARS"`) y se piden solo las transacciones faltantes o
  corregidas. Máximo 1 por documento, para no quemar tokens en loop.
- Si sigue sin cuadrar, la UI muestra el delta y una fila "diferencia sin explicar": agregás la
  transacción a mano, o aceptás el statement con el delta registrado.
- **Un statement solo pasa a `confirmed` con acción explícita tuya, y los dashboards por
  default solo agregan `confirmed`.** Así los números nunca son silenciosamente incorrectos.

---

## 6. Procesamiento asíncrono

**v1: tabla `processing_jobs` + `BackgroundTasks` + un loop in-process.** El endpoint inserta
el job y lo dispara; además un `asyncio.Task` del `lifespan` hace polling cada 5 s como red de
seguridad. La toma de trabajo usa locking real desde el día uno, para que escalar sea trivial:

```sql
update app.processing_jobs set status='running', locked_at=now(), locked_by=$1, attempts=attempts+1
where id = (select id from app.processing_jobs where status='queued' and scheduled_at <= now()
            order by scheduled_at limit 1 for update skip locked)
returning *;
```

Concurrencia con `Semaphore(2)`. Reintentos con backoff `2^attempts * 30s`. **Reaper**: jobs en
`running` con `locked_at < now() - 10 min` vuelven a `queued` — necesario porque un redeploy
mata los `BackgroundTasks` a mitad de camino.

**Upgrade path, por esfuerzo creciente:** (1) mismo código como proceso separado
(`python -m app.worker` en `fly.toml`) — cero cambios de lógica gracias al `SKIP LOCKED`;
(2) `LISTEN/NOTIFY` si molestan los 5 s; (3) ARQ + Redis solo si hace falta prioridad o
fan-out. Para 5 usuarios y ~20 documentos/mes, (3) es innecesario y **no se construye**.

**Trampa de Fly.io**: con `auto_stop_machines = true` la máquina se suspende y mata los jobs.
Configurar `min_machines_running = 1` y `auto_stop_machines = false`.

---

## 7. Frontend: Jinja2 + HTMX + Alpine + Tailwind + ECharts

Por qué, en orden de peso:

1. **Encaja con el diseño de RLS.** El login server-side guarda los tokens en una cookie
   httpOnly firmada, y el backend tiene el JWT en cada request para propagarlo a Postgres. Con
   un SPA el token vive en el browser (localStorage → XSS, o cookies cross-site → CORS + CSRF)
   y hay que construir el refresh flow del lado cliente. Esto elimina toda esa superficie.
2. **Un lenguaje, un deploy, un repo.** Un Next.js separado agrega node en CI, un segundo
   servicio, versionado de contratos y tipos duplicados. Para 2–5 usuarios no se amortiza.
3. **HTMX resuelve exactamente los cuatro flujos**: polling del estado del job
   (`hx-trigger="every 2s"` que se auto-detiene), edición inline por fila (`hx-patch` que
   devuelve la fila re-renderizada), navegación de meses (`hx-push-url`), upload con progreso.

**Gráficos: ECharts (~350 KB) vía endpoints JSON**, no Plotly server-side — `fig.to_html()`
arrastra ~3.5 MB y sus divs no sobreviven bien a los swaps de HTMX.

Reconsiderar React solo si aparece necesidad de edición masiva tipo hoja de cálculo o app móvil.

### Pantallas v1

| Ruta | Contenido |
|---|---|
| `/login` | Magic link. Sin registro |
| `/` | Dashboard: mes actual, total por moneda y en base, top categorías, cuotas futuras, vs mes anterior |
| `/documents` | Lista con estado y badge de validación, botón reprocesar |
| `/documents/upload` | Drag&drop, selector de tarjeta, campo de contraseña opcional |
| `/documents/{id}` | Progreso del job, reporte de validación, texto crudo colapsable |
| **`/documents/{id}/review`** | **La pantalla más importante.** Tabla editable con `source_line` al lado, banner con el delta de cuadre, filas de baja confianza destacadas, acciones masivas, botón "Confirmar" bloqueado hasta resolver los `pending` |
| `/transactions` | Búsqueda, filtro, edición, split |
| `/settings` | Tarjetas, categorías custom, tipo de cambio del mes, moneda base |
| `/admin/invitations` | Solo `role='admin'` |

Todas las mutaciones con CSRF token (double-submit cookie), porque hay cookie de sesión.

---

## 8. Storage

Bucket **privado** `statements`, path `{user_id}/{yyyy}/{sha256}.pdf` — el `user_id` como
primer segmento es lo que hace expresable la política:

```sql
create policy "own folder" on storage.objects for select to authenticated
  using (bucket_id='statements' and (storage.foldername(name))[1] = auth.uid()::text);
```

**La subida va por el backend, no por signed URL al browser**: el backend necesita los bytes
igual para el `sha256` (idempotencia confiable, no delegada al cliente), validar magic bytes y
desencriptar PDFs con contraseña. Con archivos de 200 KB–3 MB y 5 usuarios el ancho de banda
no es problema. Descarga con `create_signed_url` TTL 60 s. Guardas: 20 MB, `application/pdf`
verificado por contenido, máx 40 páginas, sanitizado de JS embebido y adjuntos con `pikepdf`.

---

## 9. Costos

| Servicio | Estimación |
|---|---|
| Supabase Free | Volumen proyectado: ~240 MB/año de PDFs, ~20k transacciones/año. Cabe años. **El riesgo real no es el volumen sino la pausa por 7 días de inactividad** → cron de keepalive o Pro (USD 25) |
| Gemini 2.5 Flash | ~8–20k tokens in / 3–6k out por resumen, ~20 documentos/mes → **centavos por mes**. Reprocesar 200 documentos para tunear un prompt < USD 2 |
| Fly.io | `shared-cpu-1x` 512 MB con 1 machine siempre encendida ≈ **USD 3–5/mes**. Render Free duerme el servicio y rompe los jobs → descartado |

**Total esperado: USD 3–6/mes.**

---

## 10. Fases

**Fase 0 — Andamiaje (0.5 día).** `uv init`, ruff + mypy (strict en `domain` y `llm`), pytest,
`pydantic-settings`, `structlog` con redacción de PII, Dockerfile, `fly.toml`, `supabase init`,
pre-commit, CI.

**Fase 1 — Seguridad y esquema (1.5 días) — ANTES de cualquier feature.** Migraciones completas
(tablas + índices + RLS + grants + rol `app_runtime` + seed). Verificación de JWT con JWKS.
`user_tx()`. Login/logout con cookie httpOnly + refresh. Invitaciones. El lint de `service_role`.
> **Hito: dos usuarios existen y demostradamente no se ven entre sí, con `test_rls_isolation.py`
> verde.** Sin esto, nada más importa.

**Fase 2 — Etapa 1 completa (2 días) — PRIMER HITO USABLE, SIN LLM.** Upload con hash +
idempotencia + desencriptado + Storage. Jobs + runner + reaper. `extract_text`. UI de login,
lista, upload y detalle con el texto crudo visible.
> **Hito: subir un resumen real de cada uno de los 4+ emisores y ver el texto extraído.** Esto
> valida el supuesto más riesgoso del proyecto (que los PDFs tienen capa de texto usable) a
> costo cero de LLM, y produce el corpus de fixtures para todo lo que sigue.

**Fase 3 — Extracción (3 días).** Puerto `LLMExtractor` + `gemini.py` + `fake.py`. Prompts v1.
Pydantic con `Decimal`. Chunking. Validación completa. Persistencia con merge. Jobs
`parse_statement` + `repair`. **CLI `python -m app.cli parse <doc_id> --dry-run`** para iterar
prompts contra los fixtures sin tocar la UI — acelera mucho esta fase.
> Hito: transacciones en la DB para los 4+ emisores, cuadre dentro de tolerancia en ≥ 80% de
> los documentos.

**Fase 4 — Revisión y corrección (2 días).** `/documents/{id}/review` con edición inline,
acciones masivas, banner de delta, `transaction_revisions`, confirmación, split.
> Hito: el sistema es **confiable**, no solo funcional.

**Fase 5 — Enriquecimiento y dashboard (2.5 días).** Job `enrich` (`merchant_aliases` como
cache + LLM en batch para el resto), categorización, FX manual en `/settings`, vistas de
agregación, dashboard con ECharts, cuotas futuras.
> Hito: v1 completa.

**Fase 6 — Refinamiento (2 días).** Detección de suscripciones. Vista devengada. Export
CSV/XLSX. Few-shot dinámico alimentado por `transaction_revisions`. Accuracy por emisor.

**Diferido explícitamente:** OCR, cuentas bancarias, efectivo, presupuestos y alertas, import
de CSV, PWA, notificaciones por email, finanzas compartidas de pareja, i18n.

---

## 11. Estructura del repo

```
finance_consolidation/
├── pyproject.toml  uv.lock  Dockerfile  fly.toml  Makefile  .env.example
├── supabase/migrations/       # 0001 extensions+roles ... 0006 rls_policies, 0007 views, 0008 storage
├── supabase/seed.sql          # categorías del sistema, emisores conocidos
├── app/
│   ├── main.py  settings.py  deps.py  worker.py
│   ├── security/     jwt.py  session.py  csrf.py
│   ├── infra/        db.py ★  supabase_auth.py  supabase_admin.py ★  storage.py
│   ├── domain/       money.py  dates.py  dedupe.py  installments.py
│   │                 validation.py  categorization.py  models.py     # puro, sin I/O, mypy strict
│   ├── schemas/      api.py  llm.py
│   ├── repositories/ documents  texts  statements  transactions  merchants
│   │                 categories  fx  jobs  analytics                 # todas reciben conn
│   ├── services/     ingest.py  parse.py  enrich.py  review.py  analytics.py
│   ├── pdf/          inspect.py  extract.py  chunking.py
│   ├── llm/          ports.py  gemini.py  mistral.py  fake.py  factory.py  cost.py  prompts/
│   ├── jobs/         queue.py  runner.py  handlers.py  reaper.py
│   ├── api/  web/  cli/
├── templates/        base  login  dashboard  documents/  review/  settings/  partials/
├── static/           css/tailwind.css  js/app.js  vendor/echarts.min.js
└── tests/            unit/  integration/  llm/  e2e/  fixtures/
```

★ = `infra/db.py` es el único dueño del pool; `infra/supabase_admin.py` el único módulo con
`service_role`.

---

## 12. Testing con datos sensibles

**Principio: el 90% de los tests consume TEXTO, no PDFs.** La etapa 2 recibe
`document_texts`, así que los fixtures principales son `.txt` anonimizados — cómodo y seguro.

- **Unit** (sin red, sin DB): `parse_ars_amount("1.234,56") == Decimal("1234.56")`; casos borde
  de `dedupe_key`; inferencia de año en el cruce diciembre/enero; los 8 checks de validación;
  conversión FX con `ROUND_HALF_UP`; chunking con solapamiento.
- **Anonimización**: `python -m app.cli anonymize <pdf>` extrae el texto y reemplaza
  determinísticamente tarjetas, CUIT/DNI, nombres y direcciones; **escala todos los montos por
  un factor constante** (preserva el cuadre, que es lo que se testea) y desplaza fechas por un
  offset fijo. Solo lo anonimizado se commitea; `tests/fixtures/private/` en `.gitignore` + hook
  de pre-commit que bloquea cualquier `.pdf` fuera de `fixtures/pdfs/`.
- **PDFs sintéticos**: `generate_pdfs.py` con reportlab reproduce el layout de cada emisor
  (columnas, secciones ARS/USD, PDF con contraseña) para testear la etapa 1 sin datos reales.
- **LLM con cassettes**: `fake.py` replaya respuestas grabadas → tests de `parse.py`
  deterministas y gratis en CI.
- **Accuracy real** (`@pytest.mark.llm`, fuera de CI): pega contra Gemini con los fixtures y
  compara contra `fixtures/expected/*.json`, reportando por emisor recall, MAE de montos,
  exactitud de fecha y `kind`, delta de cuadre y costo. Es el guardrail para cambios de prompt
  o modelo: la regresión se ve en números, no en anécdotas.
- **Integración con Postgres real** (mismas migraciones que producción):
  `test_rls_isolation.py`, idempotencia de upload, merge de re-parse (confirmar una tx,
  re-parsear, verificar que sobrevive), claim concurrente de jobs, reaper.
- **E2E** con `httpx.ASGITransport`: login → upload → jobs → revisión → confirmación → dashboard.
- **CI**: ruff, mypy, unit, integración con Postgres de servicio, lint de `service_role`. Sin LLM.

---

## 13. Riesgos

| Riesgo | Sev | Mitigación |
|---|---|---|
| **RLS aparente** (tabla o query sin política) | Crítica | `app_runtime` sin BYPASSRLS + `force row level security` + test parametrizado sobre `information_schema` + lint de `service_role` + pool no importable |
| **PDFs con contraseña** (Galicia/Santander/BBVA usan el DNI) | Alta, muy probable | Soportado desde Fase 2: campo opcional, `pikepdf` desencripta, contraseña nunca persistida, `409` claro |
| Layout multi-columna que pdfplumber desordena | Alta | Guardar layout + flow + tables por página; el prompt usa la variante que funcione por emisor |
| **Alucinación u omisión del LLM** | Alta | `source_line` verificado por similitud; cuadre contra `new_charges`; conteo de líneas; `temperature=0`; nada cuenta en el dashboard sin `confirmed` |
| Formato `es-AR` y signos de crédito (`CR`, `-`, `H`) | Alta | Regex `^\d+\.\d{2}$` en el schema; `direction` explícito; tests de `money.py` por emisor |
| Impuestos contaminando "gastos" (~30% de las líneas) | Media-Alta | `kind` con enum cerrado; el dashboard separa consumo / impuestos / intereses |
| Deriva del formato entre meses | Media | `prompt_version` + `raw_response` persistidos; test de accuracy por emisor detecta la caída |
| Pérdida de correcciones manuales al re-parsear | Media | Algoritmo de merge de §3 + `review_status` + `transaction_revisions` |
| Pausa de Supabase Free por inactividad | Media | Cron de keepalive o Pro; documentado en el README |
| Jobs muertos por redeploy / auto-stop | Media | Reaper + `min_machines_running=1` |
| **PII en logs** (`full_text` tiene todos tus consumos) | Media | Procesador de `structlog` que censura por clave (`full_text`, `raw_response`, `description_raw`, `source_line`, `pdf_password`, tokens); `raw_response` solo en DB |
| Costo de LLM descontrolado | Baja | `llm_usage` + tope mensual configurable + `jobs_active_uq` + máx 1 reparación automática |
| Float en montos | Media | `Decimal` end-to-end, string en el JSON del LLM, `numeric(14,2)` en DB |

---

## 14. Verificación end-to-end

Al terminar cada fase:

1. **Fase 1:** `make test-rls` → `test_rls_isolation.py` verde, incluyendo el assert de que
   toda tabla de `app` tiene RLS forzado. Manualmente: crear dos usuarios locales, loguearse
   con cada uno, confirmar que ninguno ve datos del otro.
2. **Fase 2:** `supabase start && make dev`, subir por la UI un resumen real de cada emisor.
   Verificar en `/documents/{id}` que el texto extraído es legible y las columnas están
   ordenadas. Subir el mismo archivo dos veces → un solo documento, `already_imported: true`.
3. **Fase 3:** `python -m app.cli parse <doc_id> --dry-run` por emisor; revisar el
   `ValidationReport`: cuadre dentro de tolerancia, cero `hallucination_suspect`, cobertura de
   páginas completa. Después `pytest -m llm` para la tabla de accuracy por emisor.
4. **Fase 4:** confirmar una transacción, correr `reparse`, verificar que la corrección
   sobrevivió y que aparece en `validation.conflicts[]`.
5. **Fase 5:** cargar el tipo de cambio del mes y verificar a mano, con calculadora, que el
   total consolidado en moneda base del dashboard coincide con la suma de ARS + USD×tc.
6. **Siempre:** `make lint test` (ruff + mypy + unit + integración + lint de `service_role`).

---

## Preguntas abiertas (no bloquean el arranque)

1. **Tipo de cambio**: ¿uno mensual (cierre) o por transacción? El diseño soporta ambos; v1
   arranca con mensual manual.
2. **Cuotas ya en curso** al empezar a usar la app: recomiendo cargarlas a mano en `/settings`
   para que la vista de cuotas futuras sea correcta desde el mes 1.
3. **Lista exacta de emisores**: conviene conseguir un PDF real de cada uno antes de la Fase 3.
   Los `issuer_slug` iniciales salen de ahí.
