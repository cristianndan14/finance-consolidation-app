-- ─────────────────────────────────────────────────────────────────────────────
-- Shim de `auth` para Postgres pelado (CI y tests).
--
-- Supabase provee el schema `auth`, la funcion `auth.uid()` y los roles
-- `anon` / `authenticated` / `service_role`. Un Postgres comun no.
--
-- Los tests de integracion tienen que correr contra las MISMAS migraciones que
-- produccion — si no, el test de aislamiento RLS validaria un esquema que no es
-- el que se despliega, que es peor que no tenerlo. Este archivo crea las piezas
-- minimas solo cuando faltan, y es un no-op contra Supabase.
--
-- La definicion de `auth.uid()` es la de Supabase: lee el `sub` del JSON que el
-- backend inyecta en `request.jwt.claims` (ver app/infra/db.py). El segundo
-- argumento `true` de current_setting hace que devuelva NULL en lugar de fallar
-- cuando el contexto no fue seteado, que es lo que queremos: sin identidad, las
-- politicas no matchean nada.
-- ─────────────────────────────────────────────────────────────────────────────

create schema if not exists auth;

do $$
begin
  if not exists (select 1 from pg_roles where rolname = 'anon') then
    create role anon nologin noinherit;
  end if;
  if not exists (select 1 from pg_roles where rolname = 'authenticated') then
    create role authenticated nologin noinherit;
  end if;
  if not exists (select 1 from pg_roles where rolname = 'service_role') then
    create role service_role nologin noinherit bypassrls;
  end if;
end
$$;

-- Solo se crea si no existe: contra Supabase no se toca la version oficial.
do $$
begin
  if not exists (
    select 1 from pg_proc p
      join pg_namespace n on n.oid = p.pronamespace
     where n.nspname = 'auth' and p.proname = 'uid'
  ) then
    execute $fn$
      create function auth.uid() returns uuid
      language sql stable
      as $body$
        -- El nullif va sobre el SETTING y antes del cast, no despues. Es la
        -- diferencia entre andar y romperse: `set_config(..., is_local => true)`
        -- no borra el parametro al hacer COMMIT, lo deja en cadena VACIA. En una
        -- conexion del pool que ya sirvio una request, castear '' a json tira
        -- "invalid input syntax for type json" en lugar de devolver NULL, y
        -- entonces cualquier query sin identidad (el runner tomando un job)
        -- falla en vez de simplemente no ver nada.
        -- Es tambien la forma en que Supabase define la suya.
        select nullif(
          nullif(current_setting('request.jwt.claims', true), '')::json ->> 'sub',
          ''
        )::uuid
      $body$;
    $fn$;
  end if;
end
$$;

-- Espejo minimo de auth.users. En Supabase ya existe con muchas mas columnas;
-- aca solo hace falta que la FK de app.profiles tenga a donde apuntar.
do $$
begin
  if not exists (
    select 1 from pg_tables where schemaname = 'auth' and tablename = 'users'
  ) then
    create table auth.users (
      id         uuid primary key default gen_random_uuid(),
      email      text unique,
      created_at timestamptz not null default now()
    );
  end if;
end
$$;

grant usage on schema auth to anon, authenticated, service_role;
