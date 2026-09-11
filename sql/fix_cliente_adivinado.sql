-- ============================================================================
-- Corrección de datos: cliente adivinado en formularios que eligen "Sesursa"
--
-- Pensado para GCP Cloud SQL Studio: sin meta-comandos de psql (\echo) y sin
-- BEGIN/COMMIT explícitos. Cada BLOQUE es una sola sentencia y devuelve una sola
-- grilla, así que se ejecutan de a uno, en orden, seleccionando el bloque.
--
-- QUÉ PASÓ
-- Los <select> de Cliente/Empresa de las planillas de flota posteaban el TEXTO
-- "Sesursa", no un id numérico. Sesursa es la operadora y no existe como fila en
-- customer_companies, así que la búsqueda por nombre no encontraba nada y
-- _resolve_scope_fields caía en _ensure_default_customer_company, que NO busca un
-- cliente llamado Sesursa: devuelve el PRIMER cliente del tenant por id. Esos
-- registros quedaron atribuidos a un cliente cualquiera (en producción,
-- "P.H. MULTIPLAZA PACIFIC"), que es lo que muestra Reportes, porque esa ficha lee
-- customer_companies.name y no el campo elegido en el formulario.
--
-- VENTANAS AFECTADAS
--   A) 2026-04-16 → 2026-05-16 (commits df9e4ee → afc2b02): el fallback aplicaba a
--      TODOS los formularios cuando el cliente no se podía resolver.
--   B) 2026-08-19 → 2026-09-10 (commits 5122f65 / 06ed7d5): sólo a los formularios
--      que postean el literal "Sesursa" — las dos planillas de flota y Capacitaciones
--      cuando se elige el grupo SESURSA.
-- El código quedó corregido el 2026-09-10: las planillas postean '' y, sin
-- coincidencia, la columna queda NULL.
--
-- POR QUÉ EL CRITERIO ES SEGURO
-- `id_propiedad IS NULL AND customer_company_id IS NOT NULL` sólo puede venir del
-- fallback en las tres tablas que se corrigen:
--   · Toda resolución legítima parte de la propiedad, que setea id_propiedad.
--   · La única propiedad no numérica del selector es 'NO APLICA', que existe
--     exclusivamente bajo el grupo sintético SESURSA (customer-hierarchy.js).
--   · Antes del 2026-08-19 las planillas ni siquiera tenían campo de cliente.
-- Un registro con propiedad real elegida NO cumple el criterio y no se toca.
--
-- ORDEN DE EJECUCIÓN
--   BLOQUE 1 → diagnóstico. Revisar el resultado antes de seguir.
--   BLOQUE 2 → crea la tabla de respaldo.
--   BLOQUE 3 → respalda y corrige, de forma atómica.
--   BLOQUE 4 → verifica (las tres filas deben dar 0).
-- Los bloques 2 y 3 son idempotentes: repetirlos no cambia nada.
-- ============================================================================


-- ============================================================================
-- BLOQUE 1 · DIAGNÓSTICO (sólo lectura)
--
-- `accion` separa lo que el BLOQUE 3 va a corregir de lo que sólo se informa.
-- Las filas "sólo diagnóstico" son la ventana A en el resto de formularios: ahí
-- el criterio NO es concluyente, porque una propiedad que ya estuviera inactiva
-- al momento del envío produce la misma combinación de forma legítima.
-- ============================================================================

SELECT 'se corrige'  AS accion, 'planilla_vehicular' AS tabla,
       cc.name AS cliente_atribuido, COUNT(*) AS registros,
       MIN(t.creado_en)::date AS desde, MAX(t.creado_en)::date AS hasta
  FROM planilla_vehicular t
  JOIN customer_companies cc ON cc.id = t.customer_company_id
 WHERE t.id_propiedad IS NULL AND t.customer_company_id IS NOT NULL
 GROUP BY 1, 2, 3
UNION ALL
SELECT 'se corrige', 'planilla_motocicletas', cc.name, COUNT(*),
       MIN(t.creado_en)::date, MAX(t.creado_en)::date
  FROM planilla_motocicletas t
  JOIN customer_companies cc ON cc.id = t.customer_company_id
 WHERE t.id_propiedad IS NULL AND t.customer_company_id IS NOT NULL
 GROUP BY 1, 2, 3
UNION ALL
SELECT 'se corrige', 'registro_de_capacitaciones', cc.name, COUNT(*),
       MIN(t.creado_en)::date, MAX(t.creado_en)::date
  FROM registro_de_capacitaciones t
  JOIN customer_companies cc ON cc.id = t.customer_company_id
 WHERE t.id_propiedad IS NULL AND t.customer_company_id IS NOT NULL
 GROUP BY 1, 2, 3
UNION ALL
SELECT 'sólo diagnóstico', 'reportes_incidentes', NULL, COUNT(*), NULL, NULL
  FROM reportes_incidentes
 WHERE id_propiedad IS NULL AND customer_company_id IS NOT NULL
UNION ALL
SELECT 'sólo diagnóstico', 'supervision_puesto', NULL, COUNT(*), NULL, NULL
  FROM supervision_puesto
 WHERE id_propiedad IS NULL AND customer_company_id IS NOT NULL
UNION ALL
SELECT 'sólo diagnóstico', 'registro_y_acta_de_visita', NULL, COUNT(*), NULL, NULL
  FROM registro_y_acta_de_visita
 WHERE id_propiedad IS NULL AND customer_company_id IS NOT NULL
UNION ALL
SELECT 'sólo diagnóstico', 'informe_novedades_disciplinario', NULL, COUNT(*), NULL, NULL
  FROM informe_novedades_disciplinario
 WHERE id_propiedad IS NULL AND customer_company_id IS NOT NULL
UNION ALL
SELECT 'sólo diagnóstico', 'checklist_cumplimiento', NULL, COUNT(*), NULL, NULL
  FROM checklist_cumplimiento
 WHERE id_propiedad IS NULL AND customer_company_id IS NOT NULL
UNION ALL
SELECT 'sólo diagnóstico', 'medicion_experiencia_cliente', NULL, COUNT(*), NULL, NULL
  FROM medicion_experiencia_cliente
 WHERE id_propiedad IS NULL AND customer_company_id IS NOT NULL
UNION ALL
SELECT 'sólo diagnóstico', 'confiabilidad_equipos', NULL, COUNT(*), NULL, NULL
  FROM confiabilidad_equipos
 WHERE id_propiedad IS NULL AND customer_company_id IS NOT NULL
 ORDER BY 1, 4 DESC;


-- ============================================================================
-- BLOQUE 2 · Tabla de respaldo (permite revertir el BLOQUE 3)
-- ============================================================================

CREATE TABLE IF NOT EXISTS respaldo_cliente_adivinado (
    tabla                   TEXT        NOT NULL,
    registro_id             INTEGER     NOT NULL,
    customer_company_id     INTEGER     NOT NULL,
    corregido_en            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (tabla, registro_id)
);


-- ============================================================================
-- BLOQUE 3 · CORRECCIÓN (ejecutar sólo tras revisar el BLOQUE 1)
--
-- Va dentro de un bloque DO porque es UNA sola sentencia para el servidor: o se
-- aplica entero o no se aplica nada, sin necesitar BEGIN/COMMIT explícitos, que
-- Cloud SQL Studio maneja por su cuenta. Respalda antes de cada UPDATE, y el
-- ON CONFLICT DO NOTHING conserva el respaldo original si el bloque se repite.
--
-- NULL es el valor correcto: la flota y las capacitaciones internas son de la
-- operadora, no de un cliente. Con NULL, la ficha de las dos planillas muestra el
-- nombre de la empresa leído de `companies`.
-- ============================================================================

DO $$
DECLARE
    v_veh INTEGER;
    v_mot INTEGER;
    v_cap INTEGER;
BEGIN
    INSERT INTO respaldo_cliente_adivinado (tabla, registro_id, customer_company_id)
    SELECT 'planilla_vehicular', id_planilla_vehicular, customer_company_id
      FROM planilla_vehicular
     WHERE id_propiedad IS NULL AND customer_company_id IS NOT NULL
    ON CONFLICT (tabla, registro_id) DO NOTHING;

    INSERT INTO respaldo_cliente_adivinado (tabla, registro_id, customer_company_id)
    SELECT 'planilla_motocicletas', id, customer_company_id
      FROM planilla_motocicletas
     WHERE id_propiedad IS NULL AND customer_company_id IS NOT NULL
    ON CONFLICT (tabla, registro_id) DO NOTHING;

    INSERT INTO respaldo_cliente_adivinado (tabla, registro_id, customer_company_id)
    SELECT 'registro_de_capacitaciones', id_capacitacion, customer_company_id
      FROM registro_de_capacitaciones
     WHERE id_propiedad IS NULL AND customer_company_id IS NOT NULL
    ON CONFLICT (tabla, registro_id) DO NOTHING;

    UPDATE planilla_vehicular
       SET customer_company_id = NULL
     WHERE id_propiedad IS NULL AND customer_company_id IS NOT NULL;
    GET DIAGNOSTICS v_veh = ROW_COUNT;

    UPDATE planilla_motocicletas
       SET customer_company_id = NULL
     WHERE id_propiedad IS NULL AND customer_company_id IS NOT NULL;
    GET DIAGNOSTICS v_mot = ROW_COUNT;

    UPDATE registro_de_capacitaciones
       SET customer_company_id = NULL
     WHERE id_propiedad IS NULL AND customer_company_id IS NOT NULL;
    GET DIAGNOSTICS v_cap = ROW_COUNT;

    RAISE NOTICE 'Corregidos: % vehiculares, % motocicletas, % capacitaciones',
                 v_veh, v_mot, v_cap;
END $$;


-- ============================================================================
-- BLOQUE 4 · VERIFICACIÓN (las tres filas deben dar 0)
-- ============================================================================

SELECT 'planilla_vehicular' AS tabla, COUNT(*) AS pendientes
  FROM planilla_vehicular
 WHERE id_propiedad IS NULL AND customer_company_id IS NOT NULL
UNION ALL
SELECT 'planilla_motocicletas', COUNT(*)
  FROM planilla_motocicletas
 WHERE id_propiedad IS NULL AND customer_company_id IS NOT NULL
UNION ALL
SELECT 'registro_de_capacitaciones', COUNT(*)
  FROM registro_de_capacitaciones
 WHERE id_propiedad IS NULL AND customer_company_id IS NOT NULL;


-- ============================================================================
-- REVERSA · sólo si hiciera falta deshacer el BLOQUE 3
-- (quitar los guiones para ejecutarlo)
-- ============================================================================

-- DO $$
-- BEGIN
--     UPDATE planilla_vehicular t SET customer_company_id = r.customer_company_id
--       FROM respaldo_cliente_adivinado r
--      WHERE r.tabla = 'planilla_vehicular' AND r.registro_id = t.id_planilla_vehicular;
--     UPDATE planilla_motocicletas t SET customer_company_id = r.customer_company_id
--       FROM respaldo_cliente_adivinado r
--      WHERE r.tabla = 'planilla_motocicletas' AND r.registro_id = t.id;
--     UPDATE registro_de_capacitaciones t SET customer_company_id = r.customer_company_id
--       FROM respaldo_cliente_adivinado r
--      WHERE r.tabla = 'registro_de_capacitaciones' AND r.registro_id = t.id_capacitacion;
-- END $$;
