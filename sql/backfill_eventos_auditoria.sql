-- ============================================================================
-- Backfill del log de eventos: importa a eventos_auditoria lo que SEKapp ya
-- guardaba antes de existir el log, marcado con origen = 'retroactivo':
--   · sesiones_usuario            → inicio y cierre de sesión
--   · las 11 tablas de formularios → envío de formulario (quién y cuándo)
--   · formulario_edicion_historial → edición de registro (motivo y campos)
--   · asignaciones_hallazgo        → asignación y gestión de hallazgos
--   · alertas_seguridad            → revisión de alertas de seguridad
--
-- Para correr UNA vez en Cloud SQL Studio sobre la base de la app (producción
-- SESURSA: tz-prod-sesursa-database, no `postgres`), después de
-- create_eventos_auditoria.sql o de que la app haya creado la tabla. Es un
-- solo bloque DO, atómico: si ya hay filas retroactivas no hace nada y lo
-- avisa con NOTICE. Las tablas que no existan en la instancia se saltan.
-- ============================================================================

DO $$
DECLARE
    v_licencia  TEXT;
    v_total     BIGINT := 0;
    v_n         BIGINT;
    v_forms     TEXT[][] := ARRAY[
        -- tabla,                          pk,                      form_type,                          col_fecha,   col_usuario
        ARRAY['reportes_incidentes',            'id_reporte_incidente',  'reporte_incidente',               'creado_en',  'user_email'],
        ARRAY['medicion_experiencia_cliente',   'id_encuesta',           'medicion_experiencia_cliente',    'creado_en',  'submitted_by_email'],
        ARRAY['supervision_puesto',             'id_supervision',        'supervision_puesto',              'creado_en',  'submitted_by_email'],
        ARRAY['informe_novedades_disciplinario','id_informe',            'informe_novedades_disciplinario', 'creado_en',  'submitted_by_email'],
        ARRAY['log_de_patrullas',               'id_patrulla',           'log_de_patrullas',                'creado_en',  'submitted_by_email'],
        ARRAY['registro_de_capacitaciones',     'id_capacitacion',       'registro_de_capacitaciones',      'creado_en',  'submitted_by_email'],
        ARRAY['registro_y_acta_de_visita',      'id_visita',             'registro_y_acta_de_visita',       'creado_en',  'submitted_by_email'],
        ARRAY['planilla_vehicular',             'id_planilla_vehicular', 'planilla_vehicular',              'creado_en',  'submitted_by_email'],
        ARRAY['planilla_motocicletas',          'id',                    'planilla_motocicletas',           'creado_en',  'submitted_by_email'],
        ARRAY['checklist_cumplimiento',         'id',                    'checklist_cumplimiento',          'created_at', 'submitted_by_email'],
        ARRAY['confiabilidad_equipos',          'id',                    'confiabilidad_equipos',           'created_at', 'submitted_by_email']
    ];
    v_f         TEXT[];
    i           INT;
BEGIN
    IF to_regclass('eventos_auditoria') IS NULL THEN
        RAISE EXCEPTION 'No existe eventos_auditoria: correr primero create_eventos_auditoria.sql';
    END IF;
    IF EXISTS (SELECT 1 FROM eventos_auditoria WHERE origen = 'retroactivo') THEN
        RAISE NOTICE 'El backfill ya se corrió (hay filas con origen = retroactivo). No se hace nada.';
        RETURN;
    END IF;

    SELECT name INTO v_licencia FROM companies ORDER BY id LIMIT 1;

    -- 1. Sesiones: inicio y cierre.
    IF to_regclass('sesiones_usuario') IS NOT NULL THEN
        INSERT INTO eventos_auditoria (fecha_hora, usuario_email, usuario_nombre, licencia, sesion_jti,
                                       tipo_evento, modulo, accion, estado, dispositivo, user_agent, ip, origen)
        SELECT s.creado_en, s.usuario_email, u.name, v_licencia, s.jti,
               'sesion', 'Acceso', 'Inicio de sesión', 'Exitoso', s.dispositivo, LEFT(s.user_agent, 300), s.ip, 'retroactivo'
          FROM sesiones_usuario s LEFT JOIN users u ON u.email = s.usuario_email
         WHERE s.creado_en IS NOT NULL;
        GET DIAGNOSTICS v_n = ROW_COUNT; v_total := v_total + v_n;
        RAISE NOTICE 'Inicios de sesión importados: %', v_n;

        INSERT INTO eventos_auditoria (fecha_hora, usuario_email, usuario_nombre, licencia, sesion_jti,
                                       tipo_evento, modulo, accion, estado, dispositivo, user_agent, ip, origen)
        SELECT s.cerrada_en, s.usuario_email, u.name, v_licencia, s.jti,
               'sesion', 'Acceso', 'Cierre de sesión', 'Exitoso', s.dispositivo, LEFT(s.user_agent, 300), s.ip, 'retroactivo'
          FROM sesiones_usuario s LEFT JOIN users u ON u.email = s.usuario_email
         WHERE s.cerrada_en IS NOT NULL;
        GET DIAGNOSTICS v_n = ROW_COUNT; v_total := v_total + v_n;
        RAISE NOTICE 'Cierres de sesión importados: %', v_n;
    END IF;

    -- 2. Envíos de los 11 formularios.
    FOR i IN 1 .. array_length(v_forms, 1) LOOP
        v_f := v_forms[i:i][1:5];
        IF to_regclass(v_f[1][1]) IS NULL THEN
            RAISE NOTICE 'Tabla % no existe: se salta', v_f[1][1];
            CONTINUE;
        END IF;
        IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                        WHERE table_name = v_f[1][1] AND column_name IN (v_f[1][4], v_f[1][5])
                        GROUP BY table_name HAVING COUNT(*) = 2) THEN
            RAISE NOTICE 'Tabla % sin columnas % / %: se salta', v_f[1][1], v_f[1][4], v_f[1][5];
            CONTINUE;
        END IF;
        EXECUTE format(
            'INSERT INTO eventos_auditoria (fecha_hora, usuario_email, usuario_nombre, licencia, tipo_evento, modulo,
                                            accion, formulario, registro_id, estado, origen)
             SELECT t.%2$I, t.%3$I, u.name, %4$L, ''envio'', ''Formularios'',
                    ''Envío de formulario'', %5$L, t.%6$I, ''Exitoso'', ''retroactivo''
               FROM %1$I t LEFT JOIN users u ON u.email = t.%3$I
              WHERE t.%2$I IS NOT NULL',
            v_f[1][1], v_f[1][4], v_f[1][5], v_licencia, v_f[1][3], v_f[1][2]);
        GET DIAGNOSTICS v_n = ROW_COUNT; v_total := v_total + v_n;
        RAISE NOTICE 'Envíos importados de %: %', v_f[1][1], v_n;
    END LOOP;

    -- 3. Ediciones: una fila por (registro, usuario, momento, motivo) con los campos cambiados.
    IF to_regclass('formulario_edicion_historial') IS NOT NULL THEN
        INSERT INTO eventos_auditoria (fecha_hora, usuario_email, usuario_nombre, licencia, tipo_evento, modulo,
                                       accion, formulario, registro_id, detalle, estado, origen)
        SELECT h.fecha_hora, h.usuario_email, MAX(u.name), v_licencia, 'edicion', 'Formularios',
               'Edición de registro',
               CASE h.tabla
                   WHEN 'reportes_incidentes' THEN 'reporte_incidente'
                   ELSE h.tabla
               END,
               h.registro_id,
               jsonb_build_object('motivo', h.motivo,
                                  'motivo_detalle', MAX(h.motivo_detalle),
                                  'campos', jsonb_agg(h.campo ORDER BY h.campo)),
               'Exitoso', 'retroactivo'
          FROM formulario_edicion_historial h LEFT JOIN users u ON u.email = h.usuario_email
         WHERE h.fecha_hora IS NOT NULL
         GROUP BY h.tabla, h.registro_id, h.usuario_email, h.fecha_hora, h.motivo;
        GET DIAGNOSTICS v_n = ROW_COUNT; v_total := v_total + v_n;
        RAISE NOTICE 'Ediciones importadas: %', v_n;
    END IF;

    -- 4. Hallazgos: asignación y gestión. creado_en/cerrado_en son TIMESTAMP sin zona, guardados en UTC.
    IF to_regclass('asignaciones_hallazgo') IS NOT NULL THEN
        INSERT INTO eventos_auditoria (fecha_hora, usuario_email, usuario_nombre, licencia, tipo_evento, modulo,
                                       accion, formulario, registro_id, detalle, estado, origen)
        SELECT a.creado_en AT TIME ZONE 'UTC', a.asignado_por, u.name, v_licencia, 'asignacion', 'Hallazgos',
               'Asignación de hallazgo', a.form_type, a.record_id,
               jsonb_strip_nulls(jsonb_build_object('asignacion_id', a.id, 'asignado_a', a.asignado_a,
                                                    'asignado_email', a.asignado_email,
                                                    'fecha_limite', a.fecha_limite)),
               'Exitoso', 'retroactivo'
          FROM asignaciones_hallazgo a LEFT JOIN users u ON u.email = a.asignado_por
         WHERE a.creado_en IS NOT NULL;
        GET DIAGNOSTICS v_n = ROW_COUNT; v_total := v_total + v_n;
        RAISE NOTICE 'Asignaciones importadas: %', v_n;

        INSERT INTO eventos_auditoria (fecha_hora, usuario_email, usuario_nombre, licencia, tipo_evento, modulo,
                                       accion, formulario, registro_id, detalle, estado, origen)
        SELECT a.cerrado_en AT TIME ZONE 'UTC', a.cerrado_por, u.name, v_licencia, 'asignacion', 'Hallazgos',
               'Gestión de hallazgo', a.form_type, a.record_id,
               jsonb_build_object('asignacion_id', a.id, 'estado', a.estado),
               'Exitoso', 'retroactivo'
          FROM asignaciones_hallazgo a LEFT JOIN users u ON u.email = a.cerrado_por
         WHERE a.cerrado_en IS NOT NULL;
        GET DIAGNOSTICS v_n = ROW_COUNT; v_total := v_total + v_n;
        RAISE NOTICE 'Gestiones importadas: %', v_n;
    END IF;

    -- 5. Alertas de seguridad revisadas.
    IF to_regclass('alertas_seguridad') IS NOT NULL THEN
        INSERT INTO eventos_auditoria (fecha_hora, usuario_email, usuario_nombre, licencia, tipo_evento, modulo,
                                       accion, registro_id, detalle, estado, origen)
        SELECT al.revisada_en, al.revisada_por, u.name, v_licencia, 'estado', 'Morning Briefing',
               'Revisión de alerta de seguridad', al.id,
               jsonb_build_object('usuario_alertado', al.usuario_email, 'tipo', al.tipo),
               'Exitoso', 'retroactivo'
          FROM alertas_seguridad al LEFT JOIN users u ON u.email = al.revisada_por
         WHERE al.revisada_en IS NOT NULL;
        GET DIAGNOSTICS v_n = ROW_COUNT; v_total := v_total + v_n;
        RAISE NOTICE 'Revisiones de alertas importadas: %', v_n;
    END IF;

    RAISE NOTICE 'Backfill terminado: % eventos retroactivos.', v_total;
END $$;

-- Resultado en una sola grilla.
SELECT origen, tipo_evento, accion, COUNT(*) AS eventos, MIN(fecha_hora) AS primero, MAX(fecha_hora) AS ultimo
  FROM eventos_auditoria
 GROUP BY origen, tipo_evento, accion
 ORDER BY origen, tipo_evento, accion;
