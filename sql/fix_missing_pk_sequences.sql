-- ============================================================================
-- Restaura las claves primarias autoincrementales y los timestamps de creación
-- que faltan.
--
-- PROBLEMA 1 — claves primarias sin secuencia
-- En la base de tz-prod-sesursa, 8 tablas de formularios tienen su columna de
-- ID declarada pero SIN secuencia, SIN default y SIN primary key. Cada INSERT
-- deja el ID en NULL, así que todo lo que busca un registro por ID falla:
-- el detalle del reporte, el PDF, el Excel y el envío por correo — todos pasan
-- por fetch_reports_by_ids. El listado funciona porque no necesita el ID.
--
-- Las tablas cuya PK se llama 'id' (checklist_cumplimiento, confiabilidad_equipos,
-- planilla_motocicletas) están sanas y este script no las toca.
--
-- PROBLEMA 2 — timestamps de creación sin default
-- Las mismas tablas perdieron el DEFAULT CURRENT_TIMESTAMP de su columna de
-- creación (creado_en / created_at). La app NUNCA inserta esa columna: depende
-- por completo del default de la base. Sin él cada registro nuevo queda con la
-- fecha en NULL, y eso no es cosmético:
--   * el listado muestra "Enviado por: X el N/A";
--   * los filtros por rango de fechas del visor comparan contra esa columna
--     (t.<date_col> >= %s), así que TODO registro con NULL queda invisible;
--   * el orden del listado (ORDER BY <date_col> DESC NULLS LAST) los manda
--     siempre al final, sin importar cuándo se crearon.
--
-- Este script NO rellena las fechas ya perdidas: no hay de dónde recuperar el
-- instante real de creación, e inventarlo corrompería el rastro de auditoría.
-- Sólo repone el default para que de aquí en adelante se llenen solas.
--
-- QUÉ HACE, por tabla y de forma idempotente:
--   1. Crea la secuencia <tabla>_<columna>_seq si no existe.
--   2. Backfillea los ID nulos en orden cronológico por creado_en, para que la
--      numeración siga el orden real de creación y no uno arbitrario.
--   3. Fija el default a nextval(), pone NOT NULL y agrega la primary key.
--   4. Deja la secuencia sincronizada con el máximo ID existente.
--   5. Repone DEFAULT CURRENT_TIMESTAMP en la columna de creación si le falta.
--
-- Es seguro re-ejecutarlo: cada paso verifica su estado antes de actuar.
--
-- ANTES DE CORRER: respaldar la instancia.
--   gcloud sql backups create --instance=<INSTANCIA> --project tz-prod-sesursa
-- ============================================================================

BEGIN;

DO $$
DECLARE
    objetivo   RECORD;
    seq_nombre TEXT;
    base_id    BIGINT;
    afectadas  BIGINT;
    max_id     BIGINT;
BEGIN
    FOR objetivo IN
        SELECT * FROM (VALUES
            ('reportes_incidentes',             'id_reporte_incidente',  'creado_en'),
            ('medicion_experiencia_cliente',    'id_encuesta',           'creado_en'),
            ('supervision_puesto',              'id_supervision',        'creado_en'),
            ('informe_novedades_disciplinario', 'id_informe',            'creado_en'),
            ('log_de_patrullas',                'id_patrulla',           'creado_en'),
            ('registro_de_capacitaciones',      'id_capacitacion',       'creado_en'),
            ('registro_y_acta_de_visita',       'id_visita',             'creado_en'),
            ('planilla_vehicular',              'id_planilla_vehicular', 'creado_en'),
            ('planilla_motocicletas',           'id',                    'creado_en'),
            ('flota',                           'id',                    'creado_en')
        ) AS t(tabla, id_col, date_col)
    LOOP
        -- La tabla puede no existir en un tenant que no tenga todos los módulos.
        IF NOT EXISTS (
            SELECT 1 FROM information_schema.columns
            WHERE table_schema = 'public'
              AND table_name = objetivo.tabla
              AND column_name = objetivo.id_col
        ) THEN
            RAISE NOTICE '[--] %.% no existe, se omite', objetivo.tabla, objetivo.id_col;
            CONTINUE;
        END IF;

        seq_nombre := format('%s_%s_seq', objetivo.tabla, objetivo.id_col);

        -- 1. Secuencia. OWNED BY la ata al ciclo de vida de la columna.
        IF NOT EXISTS (
            SELECT 1 FROM pg_class WHERE relkind = 'S' AND relname = seq_nombre
        ) THEN
            EXECUTE format('CREATE SEQUENCE public.%I OWNED BY public.%I.%I',
                           seq_nombre, objetivo.tabla, objetivo.id_col);
            RAISE NOTICE '[OK] secuencia % creada', seq_nombre;
        END IF;

        -- 2. Backfill cronológico de los ID nulos.
        --    Se numera con row_number() sobre el máximo actual en vez de llamar a
        --    nextval() dentro del UPDATE: nextval no garantiza seguir el ORDER BY,
        --    y queremos que el ID respete el orden de creación.
        EXECUTE format('SELECT COALESCE(MAX(%I), 0) FROM public.%I',
                       objetivo.id_col, objetivo.tabla) INTO base_id;

        EXECUTE format($f$
            UPDATE public.%1$I AS t
               SET %2$I = o.nuevo_id
              FROM (
                    SELECT ctid,
                           %4$s + row_number() OVER (ORDER BY %3$I ASC NULLS LAST, ctid) AS nuevo_id
                      FROM public.%1$I
                     WHERE %2$I IS NULL
                   ) AS o
             WHERE t.ctid = o.ctid
        $f$, objetivo.tabla, objetivo.id_col, objetivo.date_col, base_id);

        GET DIAGNOSTICS afectadas = ROW_COUNT;
        IF afectadas > 0 THEN
            RAISE NOTICE '[OK] %: % registros recibieron ID', objetivo.tabla, afectadas;
        END IF;

        -- 3. Default, NOT NULL y primary key.
        EXECUTE format('ALTER TABLE public.%I ALTER COLUMN %I SET DEFAULT nextval(%L)',
                       objetivo.tabla, objetivo.id_col, format('public.%I', seq_nombre));

        EXECUTE format('ALTER TABLE public.%I ALTER COLUMN %I SET NOT NULL',
                       objetivo.tabla, objetivo.id_col);

        IF NOT EXISTS (
            SELECT 1
              FROM pg_constraint c
              JOIN pg_class r ON r.oid = c.conrelid
             WHERE c.contype = 'p'
               AND r.relname = objetivo.tabla
               AND r.relnamespace = 'public'::regnamespace
        ) THEN
            EXECUTE format('ALTER TABLE public.%I ADD PRIMARY KEY (%I)',
                           objetivo.tabla, objetivo.id_col);
            RAISE NOTICE '[OK] %: primary key agregada sobre %', objetivo.tabla, objetivo.id_col;
        END IF;

        -- 4. Sincronizar la secuencia con el máximo real.
        EXECUTE format('SELECT COALESCE(MAX(%I), 0) FROM public.%I',
                       objetivo.id_col, objetivo.tabla) INTO max_id;
        PERFORM setval(format('public.%I', seq_nombre)::regclass, GREATEST(max_id, 1), max_id > 0);

        RAISE NOTICE '[OK] % lista (secuencia en %)', objetivo.tabla, max_id;
    END LOOP;
END $$;

-- ----------------------------------------------------------------------------
-- Paso 5: DEFAULT CURRENT_TIMESTAMP en la columna de creación.
--
-- La lista incluye las 11 tablas de formulario que lee el visor más 'flota'.
-- Ojo con los nombres: 9 tablas usan 'creado_en' y dos usan 'created_at'.
-- (confiabilidad_equipos ordena y filtra por 'fecha', que sí captura el
-- formulario; 'created_at' es igual su columna de auditoría y también la
-- necesita.)
-- ----------------------------------------------------------------------------
DO $$
DECLARE
    objetivo       RECORD;
    default_actual TEXT;
BEGIN
    FOR objetivo IN
        SELECT * FROM (VALUES
            ('reportes_incidentes',             'creado_en'),
            ('medicion_experiencia_cliente',    'creado_en'),
            ('supervision_puesto',              'creado_en'),
            ('informe_novedades_disciplinario', 'creado_en'),
            ('log_de_patrullas',                'creado_en'),
            ('registro_de_capacitaciones',      'creado_en'),
            ('registro_y_acta_de_visita',       'creado_en'),
            ('planilla_vehicular',              'creado_en'),
            ('planilla_motocicletas',           'creado_en'),
            ('checklist_cumplimiento',          'created_at'),
            ('confiabilidad_equipos',           'created_at'),
            ('flota',                           'creado_en')
        ) AS t(tabla, ts_col)
    LOOP
        -- SELECT INTO deja FOUND en false si la columna no existe en este tenant.
        -- Si existe pero no tiene default, FOUND es true y default_actual es NULL:
        -- justo el caso que hay que reparar.
        SELECT c.column_default
          INTO default_actual
          FROM information_schema.columns c
         WHERE c.table_schema = 'public'
           AND c.table_name   = objetivo.tabla
           AND c.column_name  = objetivo.ts_col;

        IF NOT FOUND THEN
            RAISE NOTICE '[--] %.% no existe, se omite', objetivo.tabla, objetivo.ts_col;
            CONTINUE;
        END IF;

        IF default_actual IS NULL THEN
            EXECUTE format('ALTER TABLE public.%I ALTER COLUMN %I SET DEFAULT CURRENT_TIMESTAMP',
                           objetivo.tabla, objetivo.ts_col);
            RAISE NOTICE '[OK] %.%: default CURRENT_TIMESTAMP repuesto', objetivo.tabla, objetivo.ts_col;
        ELSE
            RAISE NOTICE '[--] %.% ya tenía default (%), se deja igual',
                         objetivo.tabla, objetivo.ts_col, default_actual;
        END IF;
    END LOOP;
END $$;

COMMIT;

-- ============================================================================
-- Verificación. Las 11 tablas deben mostrar is_nullable = NO,
-- un column_default con nextval(...) y tiene_pk = true.
-- ============================================================================
SELECT c.table_name,
       c.column_name,
       c.is_nullable,
       c.column_default,
       EXISTS (
           SELECT 1 FROM pg_constraint pc
             JOIN pg_class pr ON pr.oid = pc.conrelid
            WHERE pc.contype = 'p' AND pr.relname = c.table_name
       ) AS tiene_pk
  FROM information_schema.columns c
 WHERE c.table_schema = 'public'
   AND (c.table_name, c.column_name) IN (
        ('reportes_incidentes',             'id_reporte_incidente'),
        ('medicion_experiencia_cliente',    'id_encuesta'),
        ('supervision_puesto',              'id_supervision'),
        ('informe_novedades_disciplinario', 'id_informe'),
        ('log_de_patrullas',                'id_patrulla'),
        ('registro_de_capacitaciones',      'id_capacitacion'),
        ('registro_y_acta_de_visita',       'id_visita'),
        ('planilla_vehicular',              'id_planilla_vehicular'),
        ('checklist_cumplimiento',          'id'),
        ('confiabilidad_equipos',           'id'),
        ('planilla_motocicletas',           'id')
   )
 ORDER BY c.table_name;

-- ============================================================================
-- Verificación de los timestamps de creación.
-- Las 12 columnas deben mostrar un column_default con CURRENT_TIMESTAMP / now().
-- Los registros que ya quedaron con la fecha en NULL siguen así a propósito:
-- este script sólo evita que se sumen nuevos.
-- ============================================================================
SELECT c.table_name,
       c.column_name,
       c.column_default
  FROM information_schema.columns c
 WHERE c.table_schema = 'public'
   AND (c.table_name, c.column_name) IN (
        ('reportes_incidentes',             'creado_en'),
        ('medicion_experiencia_cliente',    'creado_en'),
        ('supervision_puesto',              'creado_en'),
        ('informe_novedades_disciplinario', 'creado_en'),
        ('log_de_patrullas',                'creado_en'),
        ('registro_de_capacitaciones',      'creado_en'),
        ('registro_y_acta_de_visita',       'creado_en'),
        ('planilla_vehicular',              'creado_en'),
        ('planilla_motocicletas',           'creado_en'),
        ('checklist_cumplimiento',          'created_at'),
        ('confiabilidad_equipos',           'created_at'),
        ('flota',                           'creado_en')
   )
 ORDER BY c.table_name;
