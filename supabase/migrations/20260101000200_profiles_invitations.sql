-- ─────────────────────────────────────────────────────────────────────────────
-- Identidad: perfiles e invitaciones.
--
-- No hay registro autogestionado: el acceso se da por invitacion desde
-- /admin/invitations. Son 2 a 5 personas de confianza, no un producto abierto.
-- ─────────────────────────────────────────────────────────────────────────────

create table app.profiles (
  id            uuid primary key references auth.users(id) on delete cascade,
  email         citext      not null unique,
  display_name  text,
  -- Moneda en la que el dashboard consolida. Los consumos guardan su moneda
  -- original; la conversion se hace al leer, con el tipo de cambio del mes.
  base_currency char(3)     not null default 'ARS',
  role          text        not null default 'member' check (role in ('member', 'admin')),
  created_at    timestamptz not null default now(),
  updated_at    timestamptz not null default now()
);

comment on column app.profiles.role is
  'admin puede invitar usuarios. NO da acceso a datos de otros: eso lo impide RLS.';

create table app.invitations (
  id          uuid        primary key default gen_random_uuid(),
  email       citext      not null unique,
  invited_by  uuid        not null references app.profiles(id) on delete cascade,
  expires_at  timestamptz not null,
  accepted_at timestamptz,
  created_at  timestamptz not null default now(),
  check (expires_at > created_at)
);

create index invitations_pending_idx on app.invitations (email)
  where accepted_at is null;

-- ─── updated_at automatico ───────────────────────────────────────────────────
-- Un trigger, no un default: `now()` en un default solo corre en el INSERT.
create or replace function app.touch_updated_at() returns trigger
language plpgsql as $$
begin
  new.updated_at := now();
  return new;
end
$$;

create trigger profiles_touch before update on app.profiles
  for each row execute function app.touch_updated_at();

-- ─── Alta del perfil al crearse el usuario en auth ───────────────────────────
-- Sin esto haria falta que el backend cree el perfil despues del primer login, y
-- una request que falle a mitad dejaria un usuario sin perfil. Como trigger es
-- atomico con el alta.
--
-- SECURITY DEFINER porque corre en el contexto del signup, que no tiene todavia
-- una identidad con permisos sobre app.profiles. `search_path` fijo para que el
-- cuerpo no pueda resolverse contra objetos plantados por otro schema.
create or replace function app.handle_new_auth_user() returns trigger
language plpgsql security definer set search_path = app, pg_temp as $$
begin
  insert into app.profiles (id, email)
  values (new.id, new.email)
  on conflict (id) do nothing;

  update app.invitations
     set accepted_at = now()
   where email = new.email
     and accepted_at is null;

  return new;
end
$$;

create trigger on_auth_user_created after insert on auth.users
  for each row execute function app.handle_new_auth_user();
