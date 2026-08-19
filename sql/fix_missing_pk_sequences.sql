-- ============================================================================
-- Restaura las claves primarias autoincrementales que faltan.
--
-- PROBLEMA
-- En la base de tz-prod-sesursa, 8 tablas de formularios tienen su columna de
-- ID declarada pero SIN secuencia, SIN default y SIN primary key. Cada INSERT
-- deja el ID en NULL, así que todo lo que busca un registro por ID falla:
-- el detalle del reporte, el PDF, el Excel y el envío por correo — todos pasan
-- por fetch_reports_by_ids. El listado funciona porque no necesita el ID.
--
-- Las tablas cuya PK se llama 'id' (checklist_cumplimiento, confiabilidad_equipos,
-- planilla_motocicletas) están sanas y este script no las toca.
--
-- QUÉ HACE, por tabla y de forma idempotente:
--   1. Crea la secuencia <tabla>_<columna>_seq si no existe.
--   2. Backfillea los ID nulos en orden cronológico por creado_en, para que la
--      numeración siga el orden real de creación y no uno arbitrario.
--   3. Fija el default a nextval(), pone NOT NULL y agrega la primary key.
--   4. Deja la secuencia sincronizada con el máximo ID existente.
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
