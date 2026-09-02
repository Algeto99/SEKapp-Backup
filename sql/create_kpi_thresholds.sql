CREATE TABLE IF NOT EXISTS kpi_thresholds (
    key         VARCHAR(100) PRIMARY KEY,
    value       NUMERIC,
    updated_at  TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_by  TEXT,
    text_value  TEXT
);

ALTER TABLE kpi_thresholds ADD COLUMN IF NOT EXISTS text_value TEXT;

-- Defaults
INSERT INTO kpi_thresholds (key, value) VALUES
    ('supervision_verde_min',       90),
    ('supervision_amarillo_min',    70),
    ('supervision_amarillo_max',    89),
    ('supervision_rojo_max',        70),
    ('supervision_meta',            25),
    ('equipos_verde_max',            5),
    ('equipos_amarillo_min',         5),
    ('equipos_amarillo_max',        15),
    ('equipos_rojo_min',            15),
    ('dias_sin_supervision_alerta',  2),
    ('horas_incidente_escalar',     24),
    ('dias_certificacion_vencer',   30),
    ('dias_compromiso_vencer',       5),
    ('dias_backup_frecuencia',        7),
    ('visita_verde_min',            90),
    ('visita_amarillo_min',         70),
    ('visita_amarillo_max',         89),
    ('visita_rojo_max',             70),
    ('visita_meta',                 20),
    ('estatus_peso_satisfaccion',   30),
    ('estatus_peso_atencion',       25),
    ('estatus_peso_servicio',       25),
    ('estatus_peso_eventos',        20),
    ('estatus_banda_optimo',        90),
    ('estatus_banda_observacion',   75),
    ('estatus_banda_seguimiento',   60)
ON CONFLICT (key) DO NOTHING;

