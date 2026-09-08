-- ─────────────────────────────────────────────────────────────────────────────
-- Politicas de Supabase Storage para el bucket de resumenes.
--
-- Los PDFs se guardan como `{user_id}/{yyyy}/{sha256}.pdf`. El user_id como
-- PRIMER segmento del path no es cosmetico: es lo que hace que la politica se
-- pueda expresar, comparando `(storage.foldername(name))[1]` contra `auth.uid()`.
-- Con cualquier otro layout habria que parsear el path o mantener una tabla
-- paralela de permisos.
--
-- El bucket es privado. Las descargas se sirven con signed URLs de TTL corto,
-- generadas con el token del usuario, no con service_role.
--
-- Todo el archivo es condicional: `storage.objects` solo existe en Supabase. En
-- el Postgres pelado de CI esto es un no-op, y los tests de storage se marcan
-- para correr solo contra Supabase local.
-- ─────────────────────────────────────────────────────────────────────────────

do $$
begin
  if not exists (
    select 1 from pg_tables where schemaname = 'storage' and tablename = 'objects'
  ) then
    raise notice 'schema storage ausente (Postgres pelado): se saltean las politicas';
    return;
  end if;

  -- Bucket privado. `public = false` es el default, pero explicito es mejor:
  -- un bucket publico de resumenes de tarjeta seria una filtracion total.
  insert into storage.buckets (id, name, public, file_size_limit, allowed_mime_types)
  values ('statements', 'statements', false, 20971520, array['application/pdf'])
  on conflict (id) do update
    set public             = false,
        file_size_limit    = excluded.file_size_limit,
        allowed_mime_types = excluded.allowed_mime_types;

  -- Cada usuario solo alcanza su propia carpeta, en las cuatro operaciones.
  execute $pol$
    create policy statements_own_folder_select on storage.objects
      for select to authenticated
      using (
        bucket_id = 'statements'
        and (storage.foldername(name))[1] = auth.uid()::text
      )
  $pol$;

  execute $pol$
    create policy statements_own_folder_insert on storage.objects
      for insert to authenticated
      with check (
        bucket_id = 'statements'
        and (storage.foldername(name))[1] = auth.uid()::text
      )
  $pol$;

  execute $pol$
    create policy statements_own_folder_update on storage.objects
      for update to authenticated
      using (
        bucket_id = 'statements'
        and (storage.foldername(name))[1] = auth.uid()::text
      )
      with check (
        bucket_id = 'statements'
        and (storage.foldername(name))[1] = auth.uid()::text
      )
  $pol$;

  execute $pol$
    create policy statements_own_folder_delete on storage.objects
      for delete to authenticated
      using (
        bucket_id = 'statements'
        and (storage.foldername(name))[1] = auth.uid()::text
      )
  $pol$;

exception
  when duplicate_object then
    raise notice 'las politicas de storage ya existian';
end
$$;
