-- =============================================================================
-- SEKapp - Script de Limpieza de Información de Prueba
-- Objetivo: Eliminar todos los registros de prueba y validación generados
--           durante la etapa de desarrollo y pruebas en SEKapp.
-- Tablas preservadas: Catálogos maestros (companies, customer_companies,
--                     propiedades, puestos), usuarios (users, authorized_emails),
--                     configuraciones (kpi_thresholds, saved_filters).
-- =============================================================================

BEGIN;

-- 1. Detalle / Tablas dependientes de formularios
TRUNCATE TABLE
    reportes_incidentes_personas,
    capacitacion_asistencia,
    asignaciones_hallazgo,
    formulario_edicion_historial
RESTART IDENTITY CASCADE;

-- 2. Formularios y registros operativos principales
TRUNCATE TABLE
    reportes_incidentes,
    supervision_puesto,
    medicion_experiencia_cliente,
    log_de_patrullas,
    informe_novedades_disciplinario,
    registro_de_capacitaciones,
    registro_y_acta_de_visita,
    planilla_vehicular,
    planilla_motocicletas,
    checklist_cumplimiento,
    confiabilidad_equipos
RESTART IDENTITY CASCADE;

-- 3. Limpieza de tablas opcionales de auditoría operativa si existen
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.tables WHERE table_schema = 'public' AND table_name = 'auditoria_modificaciones') THEN
        EXECUTE 'TRUNCATE TABLE auditoria_modificaciones RESTART IDENTITY CASCADE';
    END IF;
END $$;

COMMIT;
