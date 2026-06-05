-- KPI thresholds configurable by admins
CREATE TABLE IF NOT EXISTS kpi_thresholds (
    key         VARCHAR(100) PRIMARY KEY,
    value       NUMERIC      NOT NULL,
    updated_at  TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_by  TEXT
);

-- Defaults
INSERT INTO kpi_thresholds (key, value) VALUES
    ('supervision_verde_min',       90),
    ('supervision_amarillo_min',    70),
    ('supervision_amarillo_max',    89),
    ('supervision_rojo_max',        70),
    ('equipos_verde_max',            5),
    ('equipos_amarillo_min',         5),
    ('equipos_amarillo_max',        15),
    ('equipos_rojo_min',            15),
    ('dias_sin_supervision_alerta',  2),
    ('horas_incidente_escalar',     24),
    ('dias_certificacion_vencer',   30)
ON CONFLICT (key) DO NOTHING;
