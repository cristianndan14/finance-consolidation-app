-- ─────────────────────────────────────────────────────────────────────────────
-- Toma de trabajos: las dos unicas operaciones que cruzan usuarios.
--
-- # El problema
--
-- `app.processing_jobs` tiene RLS como todo lo demas: una conexion con la
-- identidad del usuario A solo ve los jobs de A. Pero el runner tiene que sacar
-- "el proximo job de la cola" sin saber de antemano de quien es. No hay ninguna
-- identidad de usuario con la que ese query sea expresable.
--
-- # Las opciones y por que esta
--
-- 1. Conectarse con un rol con BYPASSRLS. Volveria a poner una credencial que ve
--    toda la base en el proceso servidor, que es exactamente lo que el diseño
--    evita.
-- 2. Pegarle a PostgREST con la service_role key. Agrega una dependencia de red
--    para algo que es un UPDATE, no se puede testear contra el Postgres pelado
--    de CI, y la key sigue siendo una credencial total.
-- 3. Dos funciones `security definer`, chicas y de proposito unico, que hacen
--    exactamente esto y nada mas.
--
-- La (3) es la que esta. El privilegio queda acotado al cuerpo de dos funciones
-- que se leen en una pantalla, en vez de a una conexion entera.
--
-- # Por que el `grant` es a `app_runtime` y NO a `authenticated`
--
-- `claim_next_job` devuelve el `payload` de un job de cualquier usuario. Si
-- `authenticated` pudiera ejecutarla, un usuario podria ir sacando jobs ajenos y
-- leerles el payload. El backend corre estas funciones en una transaccion que
-- **no** hace `set_config('role','authenticated')`, o sea como `app_runtime`, un
-- rol cuya contraseña solo tiene el proceso servidor. Un usuario del navegador
-- nunca ejecuta SQL arbitrario, asi que no tiene forma de llegar.
--
-- Los handlers, una vez que saben de quien es el job, vuelven al camino normal:
-- `system_tx(job.user_id)` con RLS activo. Un bug en un handler no puede escribir
-- en los datos de otro.
-- ─────────────────────────────────────────────────────────────────────────────

-- ─── Tomar el proximo job ────────────────────────────────────────────────────
-- `for update skip locked` es lo que permite que mañana esto sean N workers en N
-- maquinas sin cambiar una linea: dos runners nunca se llevan el mismo job.
create or replace function app.claim_next_job(worker text)
returns table (
  id           uuid,
  user_id      uuid,
  document_id  uuid,
  job_type     text,
  payload      jsonb,
  attempts     smallint,
  max_attempts smallint
)
language sql
security definer
set search_path = app, pg_temp
as $$
  update app.processing_jobs j
     set status     = 'running',
         locked_at  = now(),
         locked_by  = worker,
         started_at = coalesce(j.started_at, now()),
         attempts   = j.attempts + 1
   where j.id = (
     select c.id
       from app.processing_jobs c
      where c.status = 'queued'
        and c.scheduled_at <= now()
      order by c.scheduled_at
      limit 1
      for update skip locked
   )
  returning j.id, j.user_id, j.document_id, j.job_type, j.payload, j.attempts, j.max_attempts;
$$;

-- ─── Devolver a la cola los jobs huerfanos ───────────────────────────────────
-- Un redeploy de Fly (o un kill -9) mata los jobs en curso. Sin esto, el
-- documento se queda en "procesando" para siempre y no hay forma de destrabarlo
-- desde la UI. Los que ya agotaron los intentos se marcan `failed`: reencolar
-- eternamente un job que falla siempre es un bucle, no una recuperacion.
create or replace function app.reap_stale_jobs(max_age interval)
returns integer
language plpgsql
security definer
set search_path = app, pg_temp
as $$
declare
  reaped integer;
begin
  with stale as (
    update app.processing_jobs j
       set status      = case when j.attempts >= j.max_attempts then 'failed' else 'queued' end,
           locked_at   = null,
           locked_by   = null,
           finished_at = case when j.attempts >= j.max_attempts then now() else null end,
           error       = coalesce(j.error, 'el proceso murio antes de terminar el job')
     where j.status = 'running'
       and j.locked_at < now() - max_age
    returning 1
  )
  select count(*) into reaped from stale;

  return reaped;
end
$$;

-- El privilegio vive en las funciones, no en un rol: nadie mas puede ejecutarlas.
do $$
begin
  revoke all on function app.claim_next_job(text) from public;
  revoke all on function app.reap_stale_jobs(interval) from public;

  if exists (select 1 from pg_roles where rolname = 'app_runtime') then
    grant execute on function app.claim_next_job(text) to app_runtime;
    grant execute on function app.reap_stale_jobs(interval) to app_runtime;
  end if;
end
$$;
