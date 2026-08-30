-- ============================================================================
-- Migration Script: Apply Latest Table and Column Updates to Existing Instances
--
-- Each statement runs independently (autocommit) so exclusive locks are released
-- immediately, preventing deadlocks when running against live application traffic.
-- All statements use IF NOT EXISTS and are 100% idempotent and safe to re-run.
-- ============================================================================

-- 1. Puestos table
CREATE TABLE IF NOT EXISTS puestos (
    id_puesto SERIAL PRIMARY KEY,
    id_propiedad INTEGER REFERENCES propiedades(id_propiedad),
    nombre VARCHAR(255),
    descripcion TEXT,
    activo BOOLEAN DEFAULT TRUE,
    creado_en TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
    editado BOOLEAN DEFAULT FALSE,
    editado_en TIMESTAMPTZ,
    editado_por VARCHAR(255)
);
CREATE INDEX IF NOT EXISTS idx_puestos_id_propiedad ON puestos(id_propiedad);

-- 2. Flota table
CREATE TABLE IF NOT EXISTS flota (
    id SERIAL PRIMARY KEY,
    placa VARCHAR(255),
    tipo VARCHAR(255),
    marca VARCHAR(255),
    modelo VARCHAR(255),
    anio INTEGER,
    estado VARCHAR(255) DEFAULT 'Activo',
    sucursal VARCHAR(255),
    ubicacion VARCHAR(255),
    company_id INTEGER REFERENCES companies(id),
    customer_company_id INTEGER REFERENCES customer_companies(id),
    id_propiedad INTEGER REFERENCES propiedades(id_propiedad),
    creado_en TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
    editado BOOLEAN DEFAULT FALSE,
    editado_en TIMESTAMPTZ,
    editado_por VARCHAR(255)
);
CREATE INDEX IF NOT EXISTS idx_flota_company_id ON flota(company_id);
CREATE INDEX IF NOT EXISTS idx_flota_placa ON flota(placa);

-- 3. Programación de Supervisiones
CREATE TABLE IF NOT EXISTS supervision_programacion (
    customer_company_id INTEGER PRIMARY KEY REFERENCES customer_companies(id),
    periodicidad        TEXT    NOT NULL DEFAULT 'semanal',
    meta                INTEGER NOT NULL DEFAULT 0,
    actualizado_en      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    actualizado_por     TEXT
);

-- 4. Backups Realizados
CREATE TABLE IF NOT EXISTS backups_realizados (
    id             SERIAL PRIMARY KEY,
    generado_en    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    generado_por   TEXT,
    periodo_desde  DATE,
    periodo_hasta  DATE,
    cliente_id     INTEGER,
    cliente_nombre TEXT,
    propiedad_id   INTEGER,
    propiedad_nombre TEXT,
    total_registros INTEGER NOT NULL DEFAULT 0,
    formato        TEXT,
    archivo        TEXT,
    company_id     INTEGER REFERENCES companies(id)
);
CREATE INDEX IF NOT EXISTS idx_backups_generado_en ON backups_realizados(generado_en DESC);

-- 5. Historial de Edición de Formularios
CREATE TABLE IF NOT EXISTS formulario_edicion_historial (
    id SERIAL PRIMARY KEY,
    tabla VARCHAR(100) NOT NULL,
    registro_id INTEGER NOT NULL,
    usuario_email VARCHAR(255) NOT NULL,
    fecha_hora TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
    motivo VARCHAR(100) NOT NULL,
    motivo_detalle TEXT,
    campo VARCHAR(100) NOT NULL,
    valor_anterior TEXT,
    valor_nuevo TEXT
);
CREATE INDEX IF NOT EXISTS idx_formulario_edicion_historial_tabla_registro ON formulario_edicion_historial (tabla, registro_id);

-- 6. Dynamic KPI Thresholds & Modules
ALTER TABLE kpi_thresholds ADD COLUMN IF NOT EXISTS text_value TEXT;
ALTER TABLE companies ADD COLUMN IF NOT EXISTS enabled_modules JSONB NOT NULL DEFAULT '[]'::jsonb;

-- 7. Asignaciones de Hallazgos (Cierre)
ALTER TABLE asignaciones_hallazgo ADD COLUMN IF NOT EXISTS cerrado_en TIMESTAMP;
ALTER TABLE asignaciones_hallazgo ADD COLUMN IF NOT EXISTS cerrado_por TEXT;

-- 8. Supervisión de Puesto (Modalidad & Edición)
ALTER TABLE supervision_puesto ADD COLUMN IF NOT EXISTS modalidad_servicio VARCHAR(255);
ALTER TABLE supervision_puesto ADD COLUMN IF NOT EXISTS problemas_uniforme TEXT;
ALTER TABLE supervision_puesto ADD COLUMN IF NOT EXISTS editado BOOLEAN DEFAULT FALSE;
ALTER TABLE supervision_puesto ADD COLUMN IF NOT EXISTS editado_en TIMESTAMPTZ;
ALTER TABLE supervision_puesto ADD COLUMN IF NOT EXISTS editado_por VARCHAR(255);

-- 9. Pre-operacional Vehicular (Kilometraje, Fotos & Edición)
ALTER TABLE planilla_vehicular ADD COLUMN IF NOT EXISTS kilometraje_anterior INTEGER;
ALTER TABLE planilla_vehicular ADD COLUMN IF NOT EXISTS kilometraje_recorrido INTEGER;
ALTER TABLE planilla_vehicular ADD COLUMN IF NOT EXISTS foto_frente_url TEXT;
ALTER TABLE planilla_vehicular ADD COLUMN IF NOT EXISTS foto_atras_url TEXT;
ALTER TABLE planilla_vehicular ADD COLUMN IF NOT EXISTS foto_lado_derecho_url TEXT;
ALTER TABLE planilla_vehicular ADD COLUMN IF NOT EXISTS foto_lado_izquierdo_url TEXT;
ALTER TABLE planilla_vehicular ADD COLUMN IF NOT EXISTS editado BOOLEAN DEFAULT FALSE;
ALTER TABLE planilla_vehicular ADD COLUMN IF NOT EXISTS editado_en TIMESTAMPTZ;
ALTER TABLE planilla_vehicular ADD COLUMN IF NOT EXISTS editado_por VARCHAR(255);

-- 10. Pre-operacional Motocicletas (Kilometraje, Fotos, Diagrama Daños & Edición)
ALTER TABLE planilla_motocicletas ADD COLUMN IF NOT EXISTS kilometraje_anterior INTEGER;
ALTER TABLE planilla_motocicletas ADD COLUMN IF NOT EXISTS kilometraje_recorrido INTEGER;
ALTER TABLE planilla_motocicletas ADD COLUMN IF NOT EXISTS foto_frente_url TEXT;
ALTER TABLE planilla_motocicletas ADD COLUMN IF NOT EXISTS foto_atras_url TEXT;
ALTER TABLE planilla_motocicletas ADD COLUMN IF NOT EXISTS foto_lado_derecho_url TEXT;
ALTER TABLE planilla_motocicletas ADD COLUMN IF NOT EXISTS foto_lado_izquierdo_url TEXT;
ALTER TABLE planilla_motocicletas ADD COLUMN IF NOT EXISTS diagrama_danos TEXT;
ALTER TABLE planilla_motocicletas ADD COLUMN IF NOT EXISTS editado BOOLEAN DEFAULT FALSE;
ALTER TABLE planilla_motocicletas ADD COLUMN IF NOT EXISTS editado_en TIMESTAMPTZ;
ALTER TABLE planilla_motocicletas ADD COLUMN IF NOT EXISTS editado_por VARCHAR(255);

-- 11. Columnas de Edición para los demás formularios
ALTER TABLE reportes_incidentes ADD COLUMN IF NOT EXISTS editado BOOLEAN DEFAULT FALSE;
ALTER TABLE reportes_incidentes ADD COLUMN IF NOT EXISTS editado_en TIMESTAMPTZ;
ALTER TABLE reportes_incidentes ADD COLUMN IF NOT EXISTS editado_por VARCHAR(255);

ALTER TABLE registro_y_acta_de_visita ADD COLUMN IF NOT EXISTS editado BOOLEAN DEFAULT FALSE;
ALTER TABLE registro_y_acta_de_visita ADD COLUMN IF NOT EXISTS editado_en TIMESTAMPTZ;
ALTER TABLE registro_y_acta_de_visita ADD COLUMN IF NOT EXISTS editado_por VARCHAR(255);

ALTER TABLE medicion_experiencia_cliente ADD COLUMN IF NOT EXISTS editado BOOLEAN DEFAULT FALSE;
ALTER TABLE medicion_experiencia_cliente ADD COLUMN IF NOT EXISTS editado_en TIMESTAMPTZ;
ALTER TABLE medicion_experiencia_cliente ADD COLUMN IF NOT EXISTS editado_por VARCHAR(255);

ALTER TABLE informe_novedades_disciplinario ADD COLUMN IF NOT EXISTS editado BOOLEAN DEFAULT FALSE;
ALTER TABLE informe_novedades_disciplinario ADD COLUMN IF NOT EXISTS editado_en TIMESTAMPTZ;
ALTER TABLE informe_novedades_disciplinario ADD COLUMN IF NOT EXISTS editado_por VARCHAR(255);

ALTER TABLE log_de_patrullas ADD COLUMN IF NOT EXISTS editado BOOLEAN DEFAULT FALSE;
ALTER TABLE log_de_patrullas ADD COLUMN IF NOT EXISTS editado_en TIMESTAMPTZ;
ALTER TABLE log_de_patrullas ADD COLUMN IF NOT EXISTS editado_por VARCHAR(255);

ALTER TABLE registro_de_capacitaciones ADD COLUMN IF NOT EXISTS editado BOOLEAN DEFAULT FALSE;
ALTER TABLE registro_de_capacitaciones ADD COLUMN IF NOT EXISTS editado_en TIMESTAMPTZ;
ALTER TABLE registro_de_capacitaciones ADD COLUMN IF NOT EXISTS editado_por VARCHAR(255);

ALTER TABLE checklist_cumplimiento ADD COLUMN IF NOT EXISTS editado BOOLEAN DEFAULT FALSE;
ALTER TABLE checklist_cumplimiento ADD COLUMN IF NOT EXISTS editado_en TIMESTAMPTZ;
ALTER TABLE checklist_cumplimiento ADD COLUMN IF NOT EXISTS editado_por VARCHAR(255);

ALTER TABLE confiabilidad_equipos ADD COLUMN IF NOT EXISTS editado BOOLEAN DEFAULT FALSE;
ALTER TABLE confiabilidad_equipos ADD COLUMN IF NOT EXISTS editado_en TIMESTAMPTZ;
ALTER TABLE confiabilidad_equipos ADD COLUMN IF NOT EXISTS editado_por VARCHAR(255);
