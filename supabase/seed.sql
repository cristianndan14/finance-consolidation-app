-- ─────────────────────────────────────────────────────────────────────────────
-- Categorias del sistema (user_id IS NULL: visibles para todos, editables por
-- nadie desde la aplicacion).
--
-- Las cuatro ultimas son las que hacen legible un resumen argentino. IVA 21%,
-- percepcion RG 4815, IIBB, impuesto de sellos, intereses de financiacion y
-- comisiones de mantenimiento son cerca de un tercio de las lineas de un resumen,
-- y no son consumo. Sin categorias propias contaminarian el grafico de gastos y
-- el total del mes no se entenderia.
--
-- Este archivo lo aplica `supabase db reset` y tambien `app.cli seed`. Es
-- idempotente: se puede correr las veces que sea.
-- ─────────────────────────────────────────────────────────────────────────────

insert into app.categories (user_id, slug, name, kind, icon, sort_order) values
  -- ─── Consumo ──────────────────────────────────────────────────────────────
  (null, 'supermercado',    'Supermercado',           'expense', '🛒', 10),
  (null, 'gastronomia',     'Bares y restaurantes',   'expense', '🍽️', 20),
  (null, 'transporte',      'Transporte',             'expense', '🚗', 30),
  (null, 'combustible',     'Combustible',            'expense', '⛽', 40),
  (null, 'salud',           'Salud y farmacia',       'expense', '💊', 50),
  (null, 'servicios',       'Servicios y facturas',   'expense', '💡', 60),
  (null, 'suscripciones',   'Suscripciones',          'expense', '📺', 70),
  (null, 'tecnologia',      'Tecnologia',             'expense', '💻', 80),
  (null, 'indumentaria',    'Indumentaria',           'expense', '👕', 90),
  (null, 'hogar',           'Hogar y ferreteria',     'expense', '🏠', 100),
  (null, 'educacion',       'Educacion',              'expense', '📚', 110),
  (null, 'entretenimiento', 'Entretenimiento',        'expense', '🎬', 120),
  (null, 'viajes',          'Viajes y alojamiento',   'expense', '✈️', 130),
  (null, 'mascotas',        'Mascotas',               'expense', '🐾', 140),
  (null, 'regalos',         'Regalos y donaciones',   'expense', '🎁', 150),
  (null, 'otros',           'Otros',                  'expense', '❓', 900),

  -- ─── No es consumo: lo que cobra el banco y el fisco ──────────────────────
  (null, 'impuestos',       'Impuestos y percepciones', 'tax',    '🧾', 200),
  (null, 'intereses',       'Intereses y financiacion', 'fee',    '📈', 210),
  (null, 'comisiones',      'Comisiones y cargos',      'fee',    '🏦', 220),
  (null, 'seguros',         'Seguros',                  'expense', '🛡️', 230),

  -- ─── Movimientos que no son gasto ─────────────────────────────────────────
  (null, 'pagos',           'Pagos del resumen',      'transfer', '💸', 300),
  (null, 'devoluciones',    'Devoluciones y ajustes', 'income',   '↩️', 310),
  (null, 'adelantos',       'Adelantos de efectivo',  'transfer', '💵', 320)
on conflict (coalesce(user_id, '00000000-0000-0000-0000-000000000000'::uuid), slug)
  do update set name = excluded.name,
                kind = excluded.kind,
                icon = excluded.icon,
                sort_order = excluded.sort_order;
