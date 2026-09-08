-- ─────────────────────────────────────────────────────────────────────────────
-- Row Level Security: el aislamiento entre usuarios.
--
-- Esta migracion es la razon por la que el proyecto puede tener datos financieros
-- de varias personas en la misma base. Todo lo demas es conveniencia; esto es
-- correctitud.
--
-- Tres decisiones que vale entender:
--
-- 1. `force row level security`, no solo `enable`. `enable` no aplica las
--    politicas al OWNER de la tabla. Si una conexion queda abierta como owner (una
--    migracion, un script de mantenimiento, un `psql` olvidado), con `enable` ve
--    todo. Con `force`, no.
--
-- 2. Las politicas comparan contra `auth.uid()`, que lee el `sub` de
--    `request.jwt.claims`. Ese contexto lo setea el backend por transaccion (ver
--    app/infra/db.py). Sin contexto, `auth.uid()` es NULL, `user_id = NULL` es
--    NULL, y NULL no es TRUE: no se ve nada. El default es cerrado.
--
-- 3. `user_id` se fuerza por trigger en el INSERT, no se confia en que el codigo
--    lo mande bien. Un INSERT que intente escribir con el user_id de otro es
--    rechazado por el `with check` ademas de sobreescrito por el trigger.
--
-- El grant es explicito tabla por tabla: nada de `grant all on all tables`, que
-- haria invisible el hecho de que una tabla nueva quedo accesible.
-- ─────────────────────────────────────────────────────────────────────────────

-- ─── Trigger que fuerza el dueño de la fila ──────────────────────────────────
create or replace function app.force_owner() returns trigger
language plpgsql as $$
declare
  current_uid uuid := auth.uid();
begin
  -- En UPDATE solo interesa el caso en que se intente MOVER la fila a otro dueño.
  -- Un update que no toca user_id se deja pasar sin pedir identidad, porque los
  -- cascades `on delete set null` (borrar una tarjeta anula documents.card_id)
  -- disparan UPDATEs sin usuario en contexto, y RLS ya impide que un usuario
  -- modifique filas ajenas.
  if tg_op = 'UPDATE' then
    if new.user_id is distinct from old.user_id then
      raise exception 'no se puede cambiar el dueño de una fila de %', tg_table_name
        using errcode = '42501';
    end if;
    return new;
  end if;

  if current_uid is null then
    -- Insertar sin identidad seria crear una fila sin dueño verificable. Cubre el
    -- caso de un job mal configurado que corriera sin setear el contexto.
    raise exception 'no hay identidad en la transaccion: no se puede insertar en %',
      tg_table_name using errcode = '42501';
  end if;

  -- Queda el INSERT. El camino normal es que el codigo NO mande user_id y el
  -- trigger lo complete. Si manda uno AJENO no se corrige en silencio: eso
  -- enmascararia un bug de la aplicacion (un id mal propagado entre capas) que
  -- conviene ver explotar en desarrollo antes que descubrirlo en produccion.
  if new.user_id is not null and new.user_id <> current_uid then
    raise exception
      'intento de insertar en % con user_id ajeno (% != %)',
      tg_table_name, new.user_id, current_uid
      using errcode = '42501';
  end if;

  new.user_id := current_uid;
  return new;
end
$$;

-- ─────────────────────────────────────────────────────────────────────────────
-- Tablas con dueño simple: `user_id not null`, politica de una linea.
--
-- Se aplica en bucle para que sea imposible que una tabla quede con la politica
-- escrita de otra forma por copiar y pegar. Agregar una tabla a esta lista es la
-- unica manera de darle acceso.
-- ─────────────────────────────────────────────────────────────────────────────
do $$
declare
  owned_tables text[] := array[
    'cards',
    'documents',
    'document_texts',
    'extraction_runs',
    'statements',
    'transactions',
    'transaction_revisions',
    'merchants',
    'merchant_aliases',
    'subscriptions',
    'processing_jobs',
    'llm_usage'
  ];
  t text;
begin
  foreach t in array owned_tables loop
    execute format('alter table app.%I enable row level security', t);
    execute format('alter table app.%I force row level security', t);

    execute format(
      'create policy %I on app.%I for all to authenticated '
      'using (user_id = auth.uid()) with check (user_id = auth.uid())',
      t || '_owner', t
    );

    execute format(
      'grant select, insert, update, delete on app.%I to authenticated', t
    );

    execute format(
      'create trigger %I before insert or update on app.%I '
      'for each row execute function app.force_owner()',
      t || '_force_owner', t
    );
  end loop;
end
$$;

-- ─────────────────────────────────────────────────────────────────────────────
-- profiles: solo la propia fila.
-- ─────────────────────────────────────────────────────────────────────────────
alter table app.profiles enable row level security;
alter table app.profiles force row level security;

create policy profiles_select on app.profiles
  for select to authenticated
  using (id = auth.uid());

-- El `with check` sobre `role` impide la escalada de privilegios: un usuario
-- puede cambiar su nombre y su moneda base, pero no puede convertirse en admin
-- editando su propio perfil.
create policy profiles_update on app.profiles
  for update to authenticated
  using (id = auth.uid())
  with check (
    id = auth.uid()
    and role = (select p.role from app.profiles p where p.id = auth.uid())
  );

grant select, update on app.profiles to authenticated;
-- Sin INSERT ni DELETE: los perfiles los crea el trigger de auth.users y los
-- borra el cascade. Un usuario no se da de alta a si mismo.

-- ─────────────────────────────────────────────────────────────────────────────
-- invitations: las ve y crea quien es admin.
--
-- `role = 'admin'` se lee de app.profiles, no del JWT: el token lleva metadata
-- que el propio usuario puede editar en Supabase, asi que no es autoridad.
-- ─────────────────────────────────────────────────────────────────────────────
alter table app.invitations enable row level security;
alter table app.invitations force row level security;

create policy invitations_admin_all on app.invitations
  for all to authenticated
  using (
    exists (select 1 from app.profiles p where p.id = auth.uid() and p.role = 'admin')
  )
  with check (
    invited_by = auth.uid()
    and exists (select 1 from app.profiles p where p.id = auth.uid() and p.role = 'admin')
  );

grant select, insert, update, delete on app.invitations to authenticated;

-- ─────────────────────────────────────────────────────────────────────────────
-- categories y fx_rates: filas del sistema (user_id IS NULL) + filas del usuario.
--
-- Lectura: las propias y las del sistema. Escritura: solo las propias. Las del
-- sistema son inmutables desde la aplicacion; se cargan por seed.
-- ─────────────────────────────────────────────────────────────────────────────
do $$
declare
  shared_tables text[] := array['categories', 'fx_rates'];
  t text;
begin
  foreach t in array shared_tables loop
    execute format('alter table app.%I enable row level security', t);
    execute format('alter table app.%I force row level security', t);

    execute format(
      'create policy %I on app.%I for select to authenticated '
      'using (user_id is null or user_id = auth.uid())',
      t || '_read', t
    );
    execute format(
      'create policy %I on app.%I for insert to authenticated '
      'with check (user_id = auth.uid())',
      t || '_insert', t
    );
    execute format(
      'create policy %I on app.%I for update to authenticated '
      'using (user_id = auth.uid()) with check (user_id = auth.uid())',
      t || '_update', t
    );
    execute format(
      'create policy %I on app.%I for delete to authenticated '
      'using (user_id = auth.uid())',
      t || '_delete', t
    );

    execute format(
      'grant select, insert, update, delete on app.%I to authenticated', t
    );
  end loop;
end
$$;

-- ─────────────────────────────────────────────────────────────────────────────
-- Verificacion en la propia migracion.
--
-- Si alguien agrega una tabla a `app` y se olvida de las politicas, la migracion
-- falla aca en lugar de desplegar una tabla sin aislamiento. El test
-- tests/integration/test_rls_isolation.py hace la verificacion dinamica; esto es
-- el cinturon de seguridad para que ni siquiera llegue a aplicarse.
-- ─────────────────────────────────────────────────────────────────────────────
do $$
declare
  unprotected text;
begin
  select string_agg(c.relname, ', ' order by c.relname)
    into unprotected
    from pg_class c
    join pg_namespace n on n.oid = c.relnamespace
   where n.nspname = 'app'
     and c.relkind = 'r'
     and not (c.relrowsecurity and c.relforcerowsecurity);

  if unprotected is not null then
    raise exception 'tablas de app sin RLS forzado: %', unprotected;
  end if;

  select string_agg(c.relname, ', ' order by c.relname)
    into unprotected
    from pg_class c
    join pg_namespace n on n.oid = c.relnamespace
   where n.nspname = 'app'
     and c.relkind = 'r'
     and not exists (select 1 from pg_policy p where p.polrelid = c.oid);

  if unprotected is not null then
    raise exception 'tablas de app con RLS pero sin ninguna politica: %', unprotected;
  end if;
end
$$;
