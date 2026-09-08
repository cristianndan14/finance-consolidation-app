-- ─────────────────────────────────────────────────────────────────────────────
-- Extensiones, schema `app` y el rol con el que se conecta el backend.
--
-- `app_runtime` es la pieza que hace que Row Level Security sea real y no
-- decorativa:
--
--   * NO es superuser y NO tiene BYPASSRLS  -> las politicas se evaluan siempre
--   * NO es owner de las tablas             -> ni con `force rls` podria saltearlas
--   * SI puede asumir `authenticated`       -> el rol al que apuntan las politicas
--
-- El backend se conecta como `app_runtime` y, en cada transaccion, hace
-- `set_config('role','authenticated',true)`. A partir de ahi un query que olvide
-- filtrar por user_id devuelve CERO filas en lugar de las filas de otro usuario.
-- El modo de falla es seguro.
--
-- La contraseña no se fija aca (una migracion versionada no puede llevar
-- secretos): `app/cli/__main__.py migrate` la aplica desde APP_RUNTIME_PASSWORD
-- despues de correr las migraciones.
-- ─────────────────────────────────────────────────────────────────────────────

create extension if not exists pgcrypto;   -- gen_random_uuid, digest
create extension if not exists citext;     -- emails case-insensitive

create schema if not exists app;

do $$
begin
  if not exists (select 1 from pg_roles where rolname = 'app_runtime') then
    create role app_runtime login noinherit;
  end if;
end
$$;

-- Explicito y verificado por tests: si alguien "arregla" un permiso dandole
-- superuser o bypassrls a este rol, el aislamiento entre usuarios desaparece
-- sin que falle nada visible.
--
-- El ALTER solo se puede ejecutar siendo superuser: Postgres exige el atributo
-- SUPERUSER para cambiar SUPERUSER y BYPASSRLS, incluso para APAGARLOS. En
-- Supabase gestionado el rol `postgres` no es superuser, asi que ahi el ALTER
-- fallaria con 42501 y la migracion no correria.
--
-- `create role` deja los cinco atributos apagados por default, con lo cual en
-- ese entorno alcanza con VERIFICARLO y hacer fallar la migracion si no fuera
-- cierto. La garantia es la misma: la migracion no pasa con un app_runtime que
-- pueda saltear RLS.
do $$
begin
  if (select rolsuper from pg_roles where rolname = current_user) then
    execute 'alter role app_runtime '
            'nosuperuser nobypassrls nocreatedb nocreaterole noreplication';
  end if;

  if exists (
    select 1 from pg_roles
     where rolname = 'app_runtime'
       and (rolsuper or rolbypassrls or rolcreatedb or rolcreaterole
            or rolreplication)
  ) then
    raise exception
      'app_runtime tiene atributos que anulan RLS '
      '(superuser/bypassrls/createdb/createrole/replication)';
  end if;
end
$$;

-- Puede convertirse en `authenticated` (las politicas apuntan a ese rol) y en
-- `anon` (para el flujo de login, antes de tener identidad).
grant authenticated to app_runtime;
grant anon to app_runtime;

grant usage on schema app to app_runtime, authenticated, anon;

-- Nada de permisos por default sobre objetos futuros: cada tabla concede
-- explicitamente en la migracion de politicas. Un `grant all on all tables`
-- convertiria en invisible el hecho de que una tabla nueva quedo accesible.
alter default privileges in schema app revoke all on tables from authenticated;
alter default privileges in schema app revoke all on sequences from authenticated;

comment on schema app is
  'Datos de la aplicacion. Todas las tablas con RLS forzado y user_id denormalizado.';
