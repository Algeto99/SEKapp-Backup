-- ================================================================
-- EXPEDIENTE DE INSTALACIÓN — Schema Migration
-- Run in: GCP Cloud SQL Studio
-- Database: tz-dev-secapp (PostgreSQL)
-- Date: 2026-05-12
-- ================================================================
-- This migration adds GPS target coordinates to the `propiedades`
-- table, enabling the 100m geofence check for the Expediente.
-- It also creates a read-only aggregation VIEW over supervision,
-- incidents, and visit records keyed by id_propiedad.
-- ================================================================


-- ----------------------------------------------------------------
-- STEP 1: Add GPS target columns to propiedades
-- These define the geofence center for each installation.
-- Once set, any supervision recorded > 100m away is flagged RED.
-- ----------------------------------------------------------------
ALTER TABLE propiedades
  ADD COLUMN IF NOT EXISTS latitude  NUMERIC(10, 7),
  ADD COLUMN IF NOT EXISTS longitude NUMERIC(10, 7);


-- ----------------------------------------------------------------
-- STEP 2: Verify columns were added
-- ----------------------------------------------------------------
SELECT column_name, data_type
FROM information_schema.columns
WHERE table_name = 'propiedades'
  AND column_name IN ('latitude', 'longitude')
ORDER BY column_name;


-- ----------------------------------------------------------------
-- STEP 3: Set GPS coordinates for each property
-- Replace the example values below with the real coordinates.
-- Use Google Maps: right-click a location → "¿Qué hay aquí?"
-- Format: NUMERIC(10,7) — e.g. 14.6349150 / -90.5068900
--
-- To update a specific property:
--   UPDATE propiedades
--   SET latitude = 14.6349150, longitude = -90.5068900
--   WHERE id_propiedad = <ID>;
--
-- To see all properties and their current GPS status:
-- ----------------------------------------------------------------
SELECT id_propiedad, nombre, direccion,
       latitude, longitude,
       CASE
         WHEN latitude IS NULL OR longitude IS NULL THEN 'SIN GPS'
         ELSE 'CON GPS'
       END AS gps_status
FROM propiedades
WHERE activa = TRUE
ORDER BY nombre;


-- ----------------------------------------------------------------
-- STEP 4: (Optional) Auto-seed GPS from median supervision coords
-- This approximates the geofence center using the median of all
-- recorded supervision GPS points per property.
-- REVIEW the SELECT output before uncommenting the UPDATE block.
-- ----------------------------------------------------------------

-- Preview what would be set:
SELECT
    p.id_propiedad,
    p.nombre,
    ROUND(PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY sp.latitude)::NUMERIC, 7)  AS suggested_lat,
    ROUND(PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY sp.longitude)::NUMERIC, 7) AS suggested_lng,
    COUNT(*) AS supervision_count
FROM supervision_puesto sp
JOIN propiedades p ON p.id_propiedad = sp.id_propiedad
WHERE sp.latitude IS NOT NULL
  AND sp.longitude IS NOT NULL
  AND p.latitude IS NULL
GROUP BY p.id_propiedad, p.nombre
ORDER BY supervision_count DESC;

-- Uncomment below ONLY after reviewing the preview above:
/*
UPDATE propiedades p
SET
    latitude  = sub.med_lat,
    longitude = sub.med_lng
FROM (
    SELECT
        id_propiedad,
        PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY latitude)::NUMERIC(10,7)  AS med_lat,
        PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY longitude)::NUMERIC(10,7) AS med_lng
    FROM supervision_puesto
    WHERE latitude IS NOT NULL AND longitude IS NOT NULL
    GROUP BY id_propiedad
) sub
WHERE p.id_propiedad = sub.id_propiedad
  AND p.latitude IS NULL;
*/


-- ----------------------------------------------------------------
-- STEP 5: Create the aggregated read-only VIEW
-- Combines supervision, incidents, and visit/agreements in a
-- single timeline keyed by id_propiedad and company_id.
-- The application layer applies semaphore logic (GREEN/RED) on top.
-- ----------------------------------------------------------------
CREATE OR REPLACE VIEW v_expediente_feed AS

SELECT
    sp.id_supervision::text                           AS source_id,
    'SUPERVISION'::text                               AS module,
    COALESCE(sp.fecha_hora, sp.creado_en)             AS event_ts,
    sp.latitude,
    sp.longitude,
    sp.location_accuracy,
    sp.foto_evidencia_url                             AS foto_url,
    sp.supervisor                                     AS actor,
    COALESCE(sp.observaciones_novedades, '')          AS summary,
    sp.estado_bitacora                                AS estado,
    NULL::text                                        AS nivel_severidad,
    NULL::date                                        AS compromisos_fecha_limite,
    NULL::text                                        AS compromisos_estados,
    sp.id_propiedad,
    sp.company_id,
    sp.customer_company_id
FROM supervision_puesto sp

UNION ALL

SELECT
    ri.id_reporte_incidente::text,
    'INCIDENTE'::text,
    COALESCE(ri.fecha_hora, ri.creado_en),
    NULL::numeric, NULL::numeric, NULL::numeric,
    ri.foto_evidencia_url,
    ri.responsable_asignado,
    COALESCE(ri.descripcion_incidente, ''),
    ri.estado,
    ri.nivel_severidad,
    NULL::date,
    NULL::text,
    ri.id_propiedad,
    ri.company_id,
    ri.customer_company_id
FROM reportes_incidentes ri

UNION ALL

SELECT
    rav.id_visita::text,
    'ACUERDO'::text,
    rav.creado_en,
    NULL::numeric, NULL::numeric, NULL::numeric,
    NULL::text,
    rav.compromisos_responsable,
    COALESCE(rav.acuerdos_compromisos, ''),
    rav.compromisos_estados,
    NULL::text,
    rav.compromisos_fecha_limite,
    rav.compromisos_estados,
    rav.id_propiedad,
    rav.company_id,
    rav.customer_company_id
FROM registro_y_acta_de_visita rav;


-- ----------------------------------------------------------------
-- STEP 6: Verify the view — row count per module
-- ----------------------------------------------------------------
SELECT module, COUNT(*) AS total
FROM v_expediente_feed
GROUP BY module
ORDER BY module;


-- ----------------------------------------------------------------
-- STEP 7: Smoke test — last 10 events across all modules
-- ----------------------------------------------------------------
SELECT source_id, module, event_ts, actor,
       LEFT(summary, 60) AS summary_preview,
       id_propiedad
FROM v_expediente_feed
ORDER BY event_ts DESC NULLS LAST
LIMIT 10;
