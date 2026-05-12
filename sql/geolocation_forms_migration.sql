-- ================================================================
-- GEOLOCATION COLUMNS — Schema Migration
-- Run in: GCP Cloud SQL Studio
-- Database: tz-dev-secapp (PostgreSQL)
-- Date: 2026-05-12
-- ================================================================
-- Adds latitude, longitude, and location_accuracy to the 5 form
-- tables that already capture GPS in their frontend and backend
-- but were missing the DB columns (fields were silently dropped
-- by _filter_existing_columns).
--
-- Tables:
--   reportes_incidentes
--   medicion_experiencia_cliente
--   informe_novedades_disciplinario
--   registro_y_acta_de_visita
--   confiabilidad_equipos
-- ================================================================


-- ----------------------------------------------------------------
-- Reporte de Incidente
-- ----------------------------------------------------------------
ALTER TABLE reportes_incidentes
  ADD COLUMN IF NOT EXISTS latitude          NUMERIC(10, 7),
  ADD COLUMN IF NOT EXISTS longitude         NUMERIC(10, 7),
  ADD COLUMN IF NOT EXISTS location_accuracy NUMERIC(8, 2);


-- ----------------------------------------------------------------
-- Encuesta al Cliente
-- ----------------------------------------------------------------
ALTER TABLE medicion_experiencia_cliente
  ADD COLUMN IF NOT EXISTS latitude          NUMERIC(10, 7),
  ADD COLUMN IF NOT EXISTS longitude         NUMERIC(10, 7),
  ADD COLUMN IF NOT EXISTS location_accuracy NUMERIC(8, 2);


-- ----------------------------------------------------------------
-- Reporte Disciplinario
-- ----------------------------------------------------------------
ALTER TABLE informe_novedades_disciplinario
  ADD COLUMN IF NOT EXISTS latitude          NUMERIC(10, 7),
  ADD COLUMN IF NOT EXISTS longitude         NUMERIC(10, 7),
  ADD COLUMN IF NOT EXISTS location_accuracy NUMERIC(8, 2);


-- ----------------------------------------------------------------
-- Acta de Visita al Cliente
-- ----------------------------------------------------------------
ALTER TABLE registro_y_acta_de_visita
  ADD COLUMN IF NOT EXISTS latitude          NUMERIC(10, 7),
  ADD COLUMN IF NOT EXISTS longitude         NUMERIC(10, 7),
  ADD COLUMN IF NOT EXISTS location_accuracy NUMERIC(8, 2);


-- ----------------------------------------------------------------
-- Confiabilidad de Equipos
-- ----------------------------------------------------------------
ALTER TABLE confiabilidad_equipos
  ADD COLUMN IF NOT EXISTS latitude          NUMERIC(10, 7),
  ADD COLUMN IF NOT EXISTS longitude         NUMERIC(10, 7),
  ADD COLUMN IF NOT EXISTS location_accuracy NUMERIC(8, 2);


-- ----------------------------------------------------------------
-- Verify all columns were added
-- ----------------------------------------------------------------
SELECT table_name, column_name, data_type, numeric_precision, numeric_scale
FROM information_schema.columns
WHERE table_name IN (
    'reportes_incidentes',
    'medicion_experiencia_cliente',
    'informe_novedades_disciplinario',
    'registro_y_acta_de_visita',
    'confiabilidad_equipos'
  )
  AND column_name IN ('latitude', 'longitude', 'location_accuracy')
ORDER BY table_name, column_name;
