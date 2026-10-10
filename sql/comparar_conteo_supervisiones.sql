-- ============================================================================
-- Comparación del conteo de supervisiones: regla anterior vs regla nueva
--
-- Pensado para GCP Cloud SQL Studio: una sola sentencia, una sola grilla. Sólo lee.
-- Base: tz-prod-sesursa-database (NO la base `postgres`, que no tiene tablas).
--
-- QUÉ COMPARA (ajuste del KPI de supervisiones, KANAN 2026-10-09)
--   anterior (Fase 1): una supervisión por instalación y día; contadas = mín(realizadas,
--                      programadas) por cliente.
--   nueva (Fase 2):    cada formulario cuenta como una visita; contadas con tope por
--                      cliente y, en los clientes diarios, tope por día (el exceso de un
--                      día no compensa el faltante de otro).
-- Programadas: la meta repartida por día sobre el lapso, igual que la app
--   (diario × días; semanal × días / 7; mensual × días / días del mes), redondeo half-up.
-- Cliente de cada formulario: el mismo cruce del filtro Cliente de la app (id de cliente,
--   id de instalación, o el texto de cliente_instalacion contra el nombre de la
--   instalación o del cliente). Sólo clientes activos con programación > 0.
--
-- CÓMO USARLO: editar las dos fechas de `lapso` (lapso cerrado, ambas inclusive) y
-- ejecutar. Para la prueba obligatoria: 2026-10-01 a 2026-10-07. La última fila es el TOTAL.
-- ============================================================================

WITH lapso AS (
    SELECT DATE '2026-10-01' AS desde, DATE '2026-10-07' AS hasta
),
prog AS (
    SELECT cc.id, cc.name, sp.periodicidad, sp.meta
      FROM supervision_programacion sp
      JOIN customer_companies cc ON cc.id = sp.customer_company_id
     WHERE sp.meta > 0 AND COALESCE(cc.is_active, TRUE)
),
dias AS (
    SELECT d::date AS dia
      FROM lapso, generate_series(lapso.desde, lapso.hasta, INTERVAL '1 day') AS d
),
programadas AS (
    SELECT p.id,
           FLOOR(SUM(CASE p.periodicidad
                         WHEN 'diario'  THEN p.meta
                         WHEN 'semanal' THEN p.meta / 7.0
                         ELSE p.meta / EXTRACT(DAY FROM (DATE_TRUNC('month', dias.dia) + INTERVAL '1 month - 1 day'))::numeric
                     END) + 0.5)::int AS programadas
      FROM prog p CROSS JOIN dias
     GROUP BY p.id
),
sup AS (
    SELECT s.id_supervision, s.fecha_hora::date AS dia,
           NULLIF(TRIM(s.cliente_instalacion), '') AS inst, p.id AS cliente_id
      FROM lapso l
      CROSS JOIN supervision_puesto s
      JOIN prog p ON (
               s.customer_company_id = p.id
            OR s.id_propiedad IN (SELECT id_propiedad FROM propiedades WHERE customer_company_id = p.id)
            OR LOWER(TRIM(s.cliente_instalacion)) IN (SELECT LOWER(TRIM(nombre)) FROM propiedades WHERE customer_company_id = p.id)
            OR LOWER(TRIM(s.cliente_instalacion)) = LOWER(TRIM(p.name)))
     WHERE s.fecha_hora::date BETWEEN l.desde AND l.hasta
),
antes AS (
    SELECT cliente_id, COUNT(*) AS realizadas
      FROM (SELECT DISTINCT cliente_id, dia, inst FROM sup WHERE inst IS NOT NULL) x
     GROUP BY cliente_id
),
despues_dia AS (
    SELECT cliente_id, dia, COUNT(*) AS n FROM sup GROUP BY cliente_id, dia
),
despues AS (
    SELECT d.cliente_id,
           SUM(d.n)::int AS realizadas,
           SUM(CASE WHEN p.periodicidad = 'diario' THEN LEAST(d.n, p.meta) ELSE d.n END)::int AS con_tope_dia
      FROM despues_dia d JOIN prog p ON p.id = d.cliente_id
     GROUP BY d.cliente_id
),
filas AS (
    SELECT p.name AS cliente, p.periodicidad, p.meta, pr.programadas,
           COALESCE(a.realizadas, 0)::int                                AS antes_realizadas,
           LEAST(COALESCE(a.realizadas, 0), pr.programadas)::int          AS antes_contadas,
           COALESCE(de.realizadas, 0)                                     AS despues_realizadas,
           LEAST(COALESCE(de.con_tope_dia, 0), pr.programadas)::int       AS despues_contadas
      FROM prog p
      JOIN programadas pr ON pr.id = p.id
      LEFT JOIN antes a    ON a.cliente_id = p.id
      LEFT JOIN despues de ON de.cliente_id = p.id
)
SELECT * FROM (
    SELECT cliente, periodicidad, meta, programadas,
           antes_realizadas, antes_contadas,
           despues_realizadas, despues_contadas,
           despues_contadas - antes_contadas AS diferencia_contadas
      FROM filas
    UNION ALL
    SELECT 'TOTAL', NULL, NULL, SUM(programadas),
           SUM(antes_realizadas), SUM(antes_contadas),
           SUM(despues_realizadas), SUM(despues_contadas),
           SUM(despues_contadas) - SUM(antes_contadas)
      FROM filas
) t
ORDER BY (cliente = 'TOTAL'), cliente;
