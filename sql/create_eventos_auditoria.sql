-- ============================================================================
-- Log de eventos (Administración → Auditoría): tabla, índices y triggers.
--
-- Para correr en Cloud SQL Studio sobre la base de la app (en producción
-- SESURSA: tz-prod-sesursa-database, NO la base `postgres`). Es idempotente:
-- se puede repetir sin efecto. La app crea lo mismo sola la primera vez que
-- escribe (auditoria.asegurar_tabla), así que este script sólo adelanta el
-- trabajo o sirve para verificar. Sin meta-comandos de psql.
--
-- Los triggers rechazan UPDATE, DELETE y TRUNCATE: el log es evidencia. Si
-- algún día hiciera falta depurarlo, la única vía es deshabilitar el trigger
-- a propósito (ALTER TABLE ... DISABLE TRIGGER), lo que queda en los logs
-- de Cloud SQL.
-- ============================================================================

CREATE TABLE IF NOT EXISTS eventos_auditoria (
    id             BIGSERIAL PRIMARY KEY,
    fecha_hora     TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    usuario_email  VARCHAR(255),
    usuario_nombre VARCHAR(255),
    licencia       VARCHAR(255),
    sesion_jti     VARCHAR(64),
    tipo_evento    VARCHAR(40)  NOT NULL,
    modulo         VARCHAR(60)  NOT NULL,
    accion         VARCHAR(120) NOT NULL,
    formulario     VARCHAR(60),
    registro_id    INTEGER,
    detalle        JSONB,
    estado         VARCHAR(20)  NOT NULL,
    http_status    SMALLINT,
    metodo_ruta    VARCHAR(200),
    dispositivo    VARCHAR(20),
    user_agent     VARCHAR(300),
    ip             VARCHAR(64),
    origen         VARCHAR(20)  NOT NULL DEFAULT 'app'
);
CREATE INDEX IF NOT EXISTS idx_eventos_auditoria_usuario_fecha ON eventos_auditoria (usuario_email, fecha_hora DESC);
CREATE INDEX IF NOT EXISTS idx_eventos_auditoria_fecha         ON eventos_auditoria (fecha_hora DESC);
CREATE INDEX IF NOT EXISTS idx_eventos_auditoria_registro      ON eventos_auditoria (formulario, registro_id);
CREATE INDEX IF NOT EXISTS idx_eventos_auditoria_modulo_tipo   ON eventos_auditoria (modulo, tipo_evento, fecha_hora DESC);

CREATE OR REPLACE FUNCTION eventos_auditoria_inmutable() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'eventos_auditoria es un registro de auditoría: no se permite %', TG_OP;
END;
$$ LANGUAGE plpgsql;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = 'trg_eventos_auditoria_inmutable_fila' AND NOT tgisinternal) THEN
        CREATE TRIGGER trg_eventos_auditoria_inmutable_fila
            BEFORE UPDATE OR DELETE ON eventos_auditoria
            FOR EACH ROW EXECUTE FUNCTION eventos_auditoria_inmutable();
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = 'trg_eventos_auditoria_inmutable_truncate' AND NOT tgisinternal) THEN
        CREATE TRIGGER trg_eventos_auditoria_inmutable_truncate
            BEFORE TRUNCATE ON eventos_auditoria
            FOR EACH STATEMENT EXECUTE FUNCTION eventos_auditoria_inmutable();
    END IF;
END $$;

-- Verificación en una sola grilla: tabla, índices, triggers y filas.
SELECT 'tabla' AS objeto, CASE WHEN to_regclass('eventos_auditoria') IS NULL THEN 'FALTA' ELSE 'OK' END AS estado
UNION ALL
SELECT 'indices', COUNT(*)::text || ' de 4'
  FROM pg_indexes WHERE tablename = 'eventos_auditoria' AND indexname LIKE 'idx_eventos_auditoria_%'
UNION ALL
SELECT 'triggers', COUNT(*)::text || ' de 2'
  FROM pg_trigger WHERE tgname LIKE 'trg_eventos_auditoria_inmutable_%' AND NOT tgisinternal
UNION ALL
SELECT 'eventos registrados', COUNT(*)::text FROM eventos_auditoria;
