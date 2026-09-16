# Migración de Fly.io a Render (free tier)

## Por qué migrar

Fly.io eliminó su free tier en octubre de 2024: hoy es 100% pay-as-you-go, con una
prueba gratis de 2 horas de VM o 7 días. Con `min_machines_running = 1` en
`fly.toml` (necesario porque el runner de jobs vive dentro del proceso web), la
máquina nunca se apaga, así que factura siempre — del orden de USD 3–5/mes.

El objetivo de esta migración es dejar el hosting en USD 0/mes, sin tocar la
arquitectura de la app (sigue siendo un único proceso FastAPI que sirve HTML
server-rendered y corre el runner de jobs adentro).

## Alternativas descartadas

| Opción | Por qué no |
|---|---|
| Vercel | Solo funciones serverless de vida corta (10–60s). El runner de jobs es un proceso persistente con polling cada 5s — no puede vivir en una función que se mata al responder. Requeriría rearmar todo el modelo de jobs. |
| Cloudflare Workers | Corre en V8 (JS/WASM), sin soporte real para extensiones C nativas. `pikepdf`/`pdfplumber` (etapa 1) dependen de `libqpdf`, una librería nativa — descarta la opción de entrada. |
| Frontend en Vercel + backend en Render | Este proyecto no tiene frontend separado (Jinja2+HTMX server-rendered, sin SPA). Separar dominios rompe la razón de ser del diseño de auth actual (`CLAUDE.md`, sección Auth): la cookie de sesión depende de que frontend y backend sean el mismo origen (`SameSite=Lax`). Cambiarlo a cross-origin (`SameSite=None`) debilita la protección CSRF sin ninguna ganancia real. |
| Oracle Cloud Always Free VM | Es la única alternativa que replica el modelo "siempre encendido" sin rearquitectura, pero: pide tarjeta de crédito en el signup, tiene errores de "out of capacity" frecuentes y documentados al crear la VM, y hay que administrar SO/Docker/TLS/updates a mano. Descartada por fricción de setup y carga operativa, no por incompatibilidad técnica. |
| Google Cloud Run | La opción más "cloud-native" y con más cuota gratis, pero escala a cero de verdad entre requests. Para no perder jobs a mitad de camino requeriría implementar la migración que `docs/plan.md` sección 15 ya documenta pero no implementa (mover el disparo de jobs a `pg_cron`/`pg_net` de Supabase). Es la solución "correcta" a mediano plazo, pero es desarrollo nuevo, no una migración de infraestructura. |
| **Render.com free web service** | **Elegida.** Corre el mismo `Dockerfile` sin cambios, sin pedir tarjeta de crédito, con 750 horas gratis/mes por workspace (alcanza para un servicio siempre corriendo). El único trade-off real es que se duerme a los 15 min sin tráfico HTTP — documentado abajo con su mitigación. |

## Qué implica técnicamente

### Lo que NO cambia

- El código de la app: cero cambios en `app/`, `templates/`, ni en el modelo de
  jobs. El mismo `Dockerfile` que usa Fly sirve para Render (Render soporta
  Docker de forma nativa).
- La base de datos: sigue siendo el mismo proyecto Supabase hosted
  (`jegofrgufyiqzobfxiip`). Esta migración es solo de dónde corre el proceso
  web, no de dónde vive la base.
- El healthcheck: `/health` ya existe y Render lo puede usar igual que Fly.

### Lo que sí cambia: comportamiento con el sleep de 15 minutos

Render apaga el servicio free tras 15 minutos sin requests HTTP entrantes, y lo
despierta en el próximo request (~1 minuto de cold start). Esto interactúa con
el runner de jobs de una forma específica que hay que entender antes de migrar:

- **Caso normal (el que importa)**: subís un PDF → esa request HTTP es la que
  despierta el proceso si estaba dormido → el mismo proceso que atiende la
  request es el que tiene el loop de polling adentro → el job se encola y se
  procesa con normalidad. No hay pérdida de trabajo en este flujo, solo la
  latencia del cold start (~1 min) si el servicio estaba dormido.
- **Caso degradado**: si el proceso queda dormido por horas sin que nadie abra
  la app, el **reaper** (que recicla jobs `running` con `locked_at` viejo) no
  corre hasta que algo despierte el proceso. Esto ya es tolerable hoy — el
  reaper está pensado justamente para redeploys que matan jobs a mitad de
  camino, así que un job recuperable simplemente espera un poco más al
  próximo request. No hay corrupción de datos, solo demora.
- **Mitigación opcional**: si la demora de cold start molesta en el uso diario,
  un ping externo cada 10 minutos (cron-job.org, UptimeRobot, ambos gratis)
  al endpoint `/health` mantiene el servicio despierto — a costa de acercarse
  al límite de 750 horas/mes (correr 24/7 usa ~720h, dentro del límite pero
  sin margen para otros servicios free en el mismo workspace).

### Presupuesto de horas gratis

750 horas/mes por workspace. Un solo servicio corriendo ~24/7 (con el ping de
arriba) usa ~720h — deja margen para el mes, pero **no dejar corriendo otro
servicio web gratis en paralelo en el mismo workspace** sin revisar el total.

## Pasos de migración

1. **Cuenta en Render**: crear cuenta (no pide tarjeta para web service +
   sin Postgres propio, ya que la DB sigue en Supabase).
2. **Nuevo Web Service** apuntando al repo de GitHub, `Environment: Docker`,
   usa el `Dockerfile` existente sin cambios. Plan: `Free`.
3. **Variables de entorno** (Render → Environment, no van al repo):
   `DATABASE_URL`, `SUPABASE_URL`, `SUPABASE_ANON_KEY`,
   `SUPABASE_SERVICE_ROLE_KEY`, `SUPABASE_JWT_SECRET`, `SESSION_SECRET`
   (uno nuevo, no reutilizar el de Fly ni el de `.env` local), `GEMINI_API_KEY`,
   `ENV=production`, `LOG_LEVEL=INFO`.
4. **Health check**: configurar `/health` como healthcheck path en el servicio
   (Render → Settings → Health Check Path).
5. **Primer deploy**: Render deploya automáticamente al crear el servicio.
   Verificar logs de arranque (migraciones ya deberían estar aplicadas en
   Supabase — ver pendiente aparte sobre la migración de `category_slug`).
6. **DNS / dominio**: Render da un subdominio `*.onrender.com` gratis. Dominio
   propio es opcional y no necesario para uso personal.
7. **Verificar el flujo completo**: login, subir un documento, confirmar que
   se procesa (etapa 1 + etapa 2), revisar una transacción, ver el dashboard.
8. **Apagar/borrar la app de Fly.io** una vez confirmado que Render funciona,
   para no seguir facturando ahí.
9. **Actualizar el repo**: borrar `fly.toml`, actualizar la sección "Costos"
   del `README.md` (hoy dice "Fly.io con una máquina siempre encendida
   USD 3–5"), y documentar el deploy a Render en el README (reemplaza la
   sección de Fly).

## Riesgos y mitigación

| Riesgo | Mitigación |
|---|---|
| Cold start de ~1 min en el primer request tras dormir | Aceptable para uso personal; ping externo opcional si molesta (ver arriba). |
| Sin CD automático (hoy Fly tampoco lo tenía) | Render puede auto-deployar en cada push a `main` si se habilita "Auto-Deploy" en la config del servicio — más simple que lo que había con Fly (deploy manual con `fly deploy`). |
| Quedarse sin horas del mes por tener otro servicio free corriendo en paralelo | Revisar el uso de horas en el dashboard de Render antes de agregar otro servicio al mismo workspace. |
| Perder logs/métricas al borrar la app de Fly | Exportar logs relevantes antes de borrar, si hace falta para debugging histórico. |

## Rollback

Mientras la app de Fly.io no se borre, el rollback es simplemente volver a
apuntar el tráfico ahí (o no borrar Fly hasta confirmar Render en producción
por al menos unos días). No hay migración de datos: la base es la misma
Supabase en ambos casos.
