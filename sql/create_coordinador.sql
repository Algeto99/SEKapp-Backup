-- ============================================================================
-- Perfil Coordinador: columna de rol en users y tabla de ámbito.
--
-- La app crea esto sola al iniciar sesión (coordinador.asegurar_esquema), pero
-- ALTER TABLE users exige ser dueño de la tabla. Si en la instancia el rol de
-- la app no lo es, correr este script en Cloud SQL Studio con un usuario dueño
-- (producción SESURSA: base tz-prod-sesursa-database, NO `postgres`).
-- Idempotente, sin meta-comandos de psql. Termina con una grilla de verificación.
-- ============================================================================

ALTER TABLE users ADD COLUMN IF NOT EXISTS is_coordinador BOOLEAN NOT NULL DEFAULT FALSE;

CREATE TABLE IF NOT EXISTS coordinador_ambito (
    id                  SERIAL PRIMARY KEY,
    user_id             INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    customer_company_id INTEGER REFERENCES customer_companies(id) ON DELETE CASCADE,
    id_propiedad        INTEGER REFERENCES propiedades(id_propiedad) ON DELETE CASCADE,
    creado_en           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    creado_por          VARCHAR(255),
    CHECK (customer_company_id IS NOT NULL OR id_propiedad IS NOT NULL)
);
CREATE INDEX IF NOT EXISTS idx_coordinador_ambito_user ON coordinador_ambito (user_id);
CREATE UNIQUE INDEX IF NOT EXISTS uq_coordinador_ambito
    ON coordinador_ambito (user_id, COALESCE(customer_company_id, 0), COALESCE(id_propiedad, 0));

SELECT 'users.is_coordinador' AS objeto,
       CASE WHEN EXISTS (SELECT 1 FROM information_schema.columns
                          WHERE table_name = 'users' AND column_name = 'is_coordinador')
            THEN 'OK' ELSE 'FALTA' END AS estado
UNION ALL
SELECT 'coordinador_ambito', CASE WHEN to_regclass('coordinador_ambito') IS NULL THEN 'FALTA' ELSE 'OK' END
UNION ALL
SELECT 'coordinadores', COUNT(*)::text FROM users WHERE is_coordinador
UNION ALL
SELECT 'filas de ámbito', COUNT(*)::text FROM coordinador_ambito;
