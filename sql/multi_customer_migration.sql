BEGIN;

CREATE TABLE IF NOT EXISTS companies (
    id SERIAL PRIMARY KEY,
    name VARCHAR(255) NOT NULL,
    slug VARCHAR(255) UNIQUE,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS customer_companies (
    id SERIAL PRIMARY KEY,
    company_id INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    name VARCHAR(255) NOT NULL,
    code VARCHAR(100),
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (company_id, name)
);

ALTER TABLE users ADD COLUMN IF NOT EXISTS company_id INTEGER REFERENCES companies(id);
ALTER TABLE authorized_emails ADD COLUMN IF NOT EXISTS company_id INTEGER REFERENCES companies(id);
ALTER TABLE propiedades ADD COLUMN IF NOT EXISTS customer_company_id INTEGER REFERENCES customer_companies(id);

ALTER TABLE reportes_incidentes ADD COLUMN IF NOT EXISTS company_id INTEGER REFERENCES companies(id);
ALTER TABLE reportes_incidentes ADD COLUMN IF NOT EXISTS customer_company_id INTEGER REFERENCES customer_companies(id);

ALTER TABLE medicion_experiencia_cliente ADD COLUMN IF NOT EXISTS company_id INTEGER REFERENCES companies(id);
ALTER TABLE medicion_experiencia_cliente ADD COLUMN IF NOT EXISTS customer_company_id INTEGER REFERENCES customer_companies(id);
ALTER TABLE medicion_experiencia_cliente ADD COLUMN IF NOT EXISTS id_propiedad INTEGER REFERENCES propiedades(id_propiedad);

ALTER TABLE supervision_puesto ADD COLUMN IF NOT EXISTS company_id INTEGER REFERENCES companies(id);
ALTER TABLE supervision_puesto ADD COLUMN IF NOT EXISTS customer_company_id INTEGER REFERENCES customer_companies(id);
ALTER TABLE supervision_puesto ADD COLUMN IF NOT EXISTS id_propiedad INTEGER REFERENCES propiedades(id_propiedad);

ALTER TABLE informe_novedades_disciplinario ADD COLUMN IF NOT EXISTS company_id INTEGER REFERENCES companies(id);
ALTER TABLE informe_novedades_disciplinario ADD COLUMN IF NOT EXISTS customer_company_id INTEGER REFERENCES customer_companies(id);
ALTER TABLE informe_novedades_disciplinario ADD COLUMN IF NOT EXISTS id_propiedad INTEGER REFERENCES propiedades(id_propiedad);

ALTER TABLE log_de_patrullas ADD COLUMN IF NOT EXISTS company_id INTEGER REFERENCES companies(id);

ALTER TABLE registro_de_capacitaciones ADD COLUMN IF NOT EXISTS company_id INTEGER REFERENCES companies(id);
ALTER TABLE registro_de_capacitaciones ADD COLUMN IF NOT EXISTS customer_company_id INTEGER REFERENCES customer_companies(id);
ALTER TABLE registro_de_capacitaciones ADD COLUMN IF NOT EXISTS id_propiedad INTEGER REFERENCES propiedades(id_propiedad);

ALTER TABLE registro_y_acta_de_visita ADD COLUMN IF NOT EXISTS company_id INTEGER REFERENCES companies(id);
ALTER TABLE registro_y_acta_de_visita ADD COLUMN IF NOT EXISTS customer_company_id INTEGER REFERENCES customer_companies(id);
ALTER TABLE registro_y_acta_de_visita ADD COLUMN IF NOT EXISTS id_propiedad INTEGER REFERENCES propiedades(id_propiedad);

ALTER TABLE planilla_vehicular ADD COLUMN IF NOT EXISTS company_id INTEGER REFERENCES companies(id);
ALTER TABLE planilla_vehicular ADD COLUMN IF NOT EXISTS customer_company_id INTEGER REFERENCES customer_companies(id);
ALTER TABLE planilla_vehicular ADD COLUMN IF NOT EXISTS id_propiedad INTEGER REFERENCES propiedades(id_propiedad);

ALTER TABLE planilla_motocicletas ADD COLUMN IF NOT EXISTS company_id INTEGER REFERENCES companies(id);
ALTER TABLE planilla_motocicletas ADD COLUMN IF NOT EXISTS customer_company_id INTEGER REFERENCES customer_companies(id);
ALTER TABLE planilla_motocicletas ADD COLUMN IF NOT EXISTS id_propiedad INTEGER REFERENCES propiedades(id_propiedad);

ALTER TABLE checklist_cumplimiento ADD COLUMN IF NOT EXISTS company_id INTEGER REFERENCES companies(id);
ALTER TABLE checklist_cumplimiento ADD COLUMN IF NOT EXISTS customer_company_id INTEGER REFERENCES customer_companies(id);
ALTER TABLE checklist_cumplimiento ADD COLUMN IF NOT EXISTS id_propiedad INTEGER REFERENCES propiedades(id_propiedad);

ALTER TABLE confiabilidad_equipos ADD COLUMN IF NOT EXISTS company_id INTEGER REFERENCES companies(id);
ALTER TABLE confiabilidad_equipos ADD COLUMN IF NOT EXISTS customer_company_id INTEGER REFERENCES customer_companies(id);
ALTER TABLE confiabilidad_equipos ADD COLUMN IF NOT EXISTS id_propiedad INTEGER REFERENCES propiedades(id_propiedad);

CREATE INDEX IF NOT EXISTS idx_users_company_id ON users(company_id);
CREATE INDEX IF NOT EXISTS idx_authorized_emails_company_id ON authorized_emails(company_id);
CREATE INDEX IF NOT EXISTS idx_customer_companies_company_id ON customer_companies(company_id);
CREATE INDEX IF NOT EXISTS idx_propiedades_customer_company_id ON propiedades(customer_company_id);

CREATE INDEX IF NOT EXISTS idx_reportes_incidentes_company_id ON reportes_incidentes(company_id);
CREATE INDEX IF NOT EXISTS idx_medicion_experiencia_cliente_company_id ON medicion_experiencia_cliente(company_id);
CREATE INDEX IF NOT EXISTS idx_supervision_puesto_company_id ON supervision_puesto(company_id);
CREATE INDEX IF NOT EXISTS idx_informe_novedades_disciplinario_company_id ON informe_novedades_disciplinario(company_id);
CREATE INDEX IF NOT EXISTS idx_log_de_patrullas_company_id ON log_de_patrullas(company_id);
CREATE INDEX IF NOT EXISTS idx_registro_de_capacitaciones_company_id ON registro_de_capacitaciones(company_id);
CREATE INDEX IF NOT EXISTS idx_registro_y_acta_de_visita_company_id ON registro_y_acta_de_visita(company_id);

-- Commitment status overrides: JSON object {"block_idx": "cumplido|pendiente|vencido"}
ALTER TABLE registro_y_acta_de_visita ADD COLUMN IF NOT EXISTS compromisos_estados TEXT DEFAULT NULL;
CREATE INDEX IF NOT EXISTS idx_planilla_vehicular_company_id ON planilla_vehicular(company_id);
CREATE INDEX IF NOT EXISTS idx_planilla_motocicletas_company_id ON planilla_motocicletas(company_id);
CREATE INDEX IF NOT EXISTS idx_checklist_cumplimiento_company_id ON checklist_cumplimiento(company_id);
CREATE INDEX IF NOT EXISTS idx_confiabilidad_equipos_company_id ON confiabilidad_equipos(company_id);

COMMIT;

-- Ensure every tenant has at least one customer row.
INSERT INTO customer_companies (company_id, name, code, is_active)
SELECT c.id, c.name || ' - Cliente Principal', 'DEFAULT', TRUE
FROM companies c
WHERE NOT EXISTS (
    SELECT 1
    FROM customer_companies cc
    WHERE cc.company_id = c.id
);

-- ------------------------------------------------------------------
-- Example seed data. Replace these names with your real companies.
-- ------------------------------------------------------------------

INSERT INTO companies (name, slug)
VALUES ('Company A', 'company-a')
ON CONFLICT (slug) DO UPDATE SET name = EXCLUDED.name;

INSERT INTO customer_companies (company_id, name, code)
SELECT c.id, x.name, x.code
FROM companies c
CROSS JOIN (
    VALUES
        ('Company B', 'B'),
        ('Company C', 'C')
) AS x(name, code)
WHERE c.slug = 'company-a'
ON CONFLICT (company_id, name) DO NOTHING;

-- Assign tenant ownership to users and authorized emails.
-- Replace the sample emails with the real users that belong to Company A.
UPDATE users
SET company_id = c.id
FROM companies c
WHERE c.slug = 'company-a'
  AND email IN ('admin@company-a.com', 'ops@company-a.com');

UPDATE authorized_emails
SET company_id = c.id
FROM companies c
WHERE c.slug = 'company-a'
  AND email IN ('admin@company-a.com', 'ops@company-a.com');

-- Map properties to customer companies.
-- Replace the property names with the real records already in propiedades.
UPDATE propiedades p
SET customer_company_id = cc.id
FROM customer_companies cc
JOIN companies c ON c.id = cc.company_id
WHERE c.slug = 'company-a'
  AND (
      (cc.name = 'Company B' AND p.nombre IN ('B - Site 1', 'B - Site 2'))
      OR
      (cc.name = 'Company C' AND p.nombre IN ('C - Main Site'))
  );

-- ------------------------------------------------------------------
-- Backfill existing records from the submitting user and property text.
-- ------------------------------------------------------------------

UPDATE reportes_incidentes t
SET company_id = u.company_id
FROM users u
WHERE t.user_email = u.email
  AND t.company_id IS NULL;

UPDATE medicion_experiencia_cliente t
SET company_id = u.company_id
FROM users u
WHERE t.submitted_by_email = u.email
  AND t.company_id IS NULL;

UPDATE supervision_puesto t
SET company_id = u.company_id
FROM users u
WHERE t.submitted_by_email = u.email
  AND t.company_id IS NULL;

UPDATE informe_novedades_disciplinario t
SET company_id = u.company_id
FROM users u
WHERE t.submitted_by_email = u.email
  AND t.company_id IS NULL;

UPDATE log_de_patrullas t
SET company_id = u.company_id
FROM users u
WHERE t.submitted_by_email = u.email
  AND t.company_id IS NULL;

UPDATE registro_de_capacitaciones t
SET company_id = u.company_id
FROM users u
WHERE t.submitted_by_email = u.email
  AND t.company_id IS NULL;

UPDATE registro_y_acta_de_visita t
SET company_id = u.company_id
FROM users u
WHERE t.submitted_by_email = u.email
  AND t.company_id IS NULL;

UPDATE planilla_vehicular t
SET company_id = u.company_id
FROM users u
WHERE t.submitted_by_email = u.email
  AND t.company_id IS NULL;

UPDATE planilla_motocicletas t
SET company_id = u.company_id
FROM users u
WHERE t.submitted_by_email = u.email
  AND t.company_id IS NULL;

UPDATE checklist_cumplimiento t
SET company_id = u.company_id
FROM users u
WHERE t.submitted_by_email = u.email
  AND t.company_id IS NULL;

UPDATE confiabilidad_equipos t
SET company_id = u.company_id
FROM users u
WHERE t.submitted_by_email = u.email
  AND t.company_id IS NULL;

-- Property and customer backfill based on the legacy text labels.
UPDATE reportes_incidentes t
SET id_propiedad = p.id_propiedad,
    customer_company_id = p.customer_company_id
FROM propiedades p
WHERE LOWER(TRIM(t.cliente_instalacion)) = LOWER(TRIM(p.nombre))
  AND (t.id_propiedad IS NULL OR t.customer_company_id IS NULL);

UPDATE medicion_experiencia_cliente t
SET id_propiedad = p.id_propiedad,
    customer_company_id = p.customer_company_id
FROM propiedades p
WHERE LOWER(TRIM(t.cliente_instalacion)) = LOWER(TRIM(p.nombre))
  AND (t.id_propiedad IS NULL OR t.customer_company_id IS NULL);

UPDATE supervision_puesto t
SET id_propiedad = p.id_propiedad,
    customer_company_id = p.customer_company_id
FROM propiedades p
WHERE LOWER(TRIM(t.cliente)) = LOWER(TRIM(p.nombre))
  AND (t.id_propiedad IS NULL OR t.customer_company_id IS NULL);

UPDATE informe_novedades_disciplinario t
SET id_propiedad = p.id_propiedad,
    customer_company_id = p.customer_company_id
FROM propiedades p
WHERE LOWER(TRIM(t.cliente_instalacion)) = LOWER(TRIM(p.nombre))
  AND (t.id_propiedad IS NULL OR t.customer_company_id IS NULL);

UPDATE registro_de_capacitaciones t
SET id_propiedad = p.id_propiedad,
    customer_company_id = p.customer_company_id
FROM propiedades p
WHERE LOWER(TRIM(t.cliente_instalacion)) = LOWER(TRIM(p.nombre))
  AND (t.id_propiedad IS NULL OR t.customer_company_id IS NULL);

UPDATE registro_y_acta_de_visita t
SET id_propiedad = p.id_propiedad,
    customer_company_id = p.customer_company_id
FROM propiedades p
WHERE LOWER(TRIM(t.cliente_instalacion)) = LOWER(TRIM(p.nombre))
  AND (t.id_propiedad IS NULL OR t.customer_company_id IS NULL);

UPDATE planilla_vehicular t
SET id_propiedad = p.id_propiedad,
    customer_company_id = p.customer_company_id
FROM propiedades p
WHERE LOWER(TRIM(t.cliente_instalacion)) = LOWER(TRIM(p.nombre))
  AND (t.id_propiedad IS NULL OR t.customer_company_id IS NULL);

UPDATE planilla_motocicletas t
SET id_propiedad = p.id_propiedad,
    customer_company_id = p.customer_company_id
FROM propiedades p
WHERE LOWER(TRIM(t.cliente_instalacion)) = LOWER(TRIM(p.nombre))
  AND (t.id_propiedad IS NULL OR t.customer_company_id IS NULL);

UPDATE checklist_cumplimiento t
SET id_propiedad = p.id_propiedad,
    customer_company_id = p.customer_company_id
FROM propiedades p
WHERE LOWER(TRIM(t.cliente_instalacion)) = LOWER(TRIM(p.nombre))
  AND (t.id_propiedad IS NULL OR t.customer_company_id IS NULL);

UPDATE confiabilidad_equipos t
SET id_propiedad = p.id_propiedad,
    customer_company_id = p.customer_company_id
FROM propiedades p
WHERE LOWER(TRIM(t.cliente_instalacion)) = LOWER(TRIM(p.nombre))
  AND (t.id_propiedad IS NULL OR t.customer_company_id IS NULL);

-- If a record matched a customer name instead of a property name, backfill customer only.
UPDATE reportes_incidentes t
SET customer_company_id = cc.id
FROM customer_companies cc
WHERE LOWER(TRIM(t.cliente_instalacion)) = LOWER(TRIM(cc.name))
  AND t.customer_company_id IS NULL;

UPDATE medicion_experiencia_cliente t
SET customer_company_id = cc.id
FROM customer_companies cc
WHERE LOWER(TRIM(t.cliente_instalacion)) = LOWER(TRIM(cc.name))
  AND t.customer_company_id IS NULL;

UPDATE supervision_puesto t
SET customer_company_id = cc.id
FROM customer_companies cc
WHERE LOWER(TRIM(t.cliente)) = LOWER(TRIM(cc.name))
  AND t.customer_company_id IS NULL;

UPDATE informe_novedades_disciplinario t
SET customer_company_id = cc.id
FROM customer_companies cc
WHERE LOWER(TRIM(t.cliente_instalacion)) = LOWER(TRIM(cc.name))
  AND t.customer_company_id IS NULL;

UPDATE registro_de_capacitaciones t
SET customer_company_id = cc.id
FROM customer_companies cc
WHERE LOWER(TRIM(t.cliente_instalacion)) = LOWER(TRIM(cc.name))
  AND t.customer_company_id IS NULL;

UPDATE registro_y_acta_de_visita t
SET customer_company_id = cc.id
FROM customer_companies cc
WHERE LOWER(TRIM(t.cliente_instalacion)) = LOWER(TRIM(cc.name))
  AND t.customer_company_id IS NULL;

UPDATE planilla_vehicular t
SET customer_company_id = cc.id
FROM customer_companies cc
WHERE LOWER(TRIM(t.cliente_instalacion)) = LOWER(TRIM(cc.name))
  AND t.customer_company_id IS NULL;

UPDATE planilla_motocicletas t
SET customer_company_id = cc.id
FROM customer_companies cc
WHERE LOWER(TRIM(t.cliente_instalacion)) = LOWER(TRIM(cc.name))
  AND t.customer_company_id IS NULL;

UPDATE checklist_cumplimiento t
SET customer_company_id = cc.id
FROM customer_companies cc
WHERE LOWER(TRIM(t.cliente_instalacion)) = LOWER(TRIM(cc.name))
  AND t.customer_company_id IS NULL;

UPDATE confiabilidad_equipos t
SET customer_company_id = cc.id
FROM customer_companies cc
WHERE LOWER(TRIM(t.cliente_instalacion)) = LOWER(TRIM(cc.name))
  AND t.customer_company_id IS NULL;
