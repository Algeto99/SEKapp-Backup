-- Onboarding Template for a New Tenant (e.g., SESURSA)
-- Use this script to register a new company, its internal client structures, properties, and administrator users.
-- Replace placeholders marked with <brackets> before running.

BEGIN;

-- ==========================================
-- 1. Create the Security Provider (Company)
-- ==========================================
-- 'slug' must be unique and is used for tenant routing/subdomains.
INSERT INTO companies (name, slug, is_active)
VALUES ('<COMPANY_NAME, e.g., SESURSA>', '<COMPANY_SLUG, e.g., sesursa>', TRUE)
ON CONFLICT (slug) DO UPDATE 
SET name = EXCLUDED.name, is_active = EXCLUDED.is_active;

-- ==========================================
-- 2. Create the Customer (Client Company)
-- ==========================================
-- Each company can serve one or more customer companies.
-- We use a CTE to retrieve the company_id dynamically by slug.
WITH new_company AS (
    SELECT id FROM companies WHERE slug = '<COMPANY_SLUG, e.g., sesursa>' LIMIT 1
)
INSERT INTO customer_companies (company_id, name, code, is_active)
SELECT 
    new_company.id, 
    '<CUSTOMER_NAME, e.g., SESURSA S.A. - Cliente Principal>', 
    '<CUSTOMER_CODE, e.g., SES>', 
    TRUE
FROM new_company
ON CONFLICT (company_id, name) DO NOTHING;

-- ==========================================
-- 3. Create the Physical Properties (Installations)
-- ==========================================
-- Installations map to 'propiedades'. They contain coordinates for geofencing.
WITH target_customer AS (
    SELECT cc.id 
    FROM customer_companies cc
    JOIN companies c ON cc.company_id = c.id
    WHERE c.slug = '<COMPANY_SLUG, e.g., sesursa>'
      AND cc.name = '<CUSTOMER_NAME, e.g., SESURSA S.A. - Cliente Principal>'
    LIMIT 1
)
INSERT INTO propiedades (nombre, descripcion, direccion, activa, customer_company_id, latitude, longitude)
SELECT 
    '<PROPERTY_NAME, e.g., Planta Central>', 
    '<PROPERTY_DESCRIPTION, e.g., Oficina y Centro de Almacenamiento>', 
    '<PROPERTY_ADDRESS, e.g., Av. Industrial 123, Sector 4>', 
    TRUE, 
    target_customer.id, 
    <LATITUDE, e.g., -12.046374>, 
    <LONGITUDE, e.g., -77.042793>
FROM target_customer;

-- ==========================================
-- 4. Pre-authorize Emails for Security
-- ==========================================
-- The registration process only allows users with pre-approved emails.
WITH new_company AS (
    SELECT id FROM companies WHERE slug = '<COMPANY_SLUG, e.g., sesursa>' LIMIT 1
)
INSERT INTO authorized_emails (email, authorized_by, is_active, notes, is_admin, company_id)
SELECT 
    x.email, 
    'System Onboarding', 
    TRUE, 
    x.notes, 
    x.is_admin, 
    new_company.id
FROM new_company
CROSS JOIN (
  VALUES
    ('<ADMIN_EMAIL, e.g., admin@sesursa.com>', 'Administrador de SESURSA', TRUE),
    ('<SUPERVISOR_EMAIL, e.g., supervisor@sesursa.com>', 'Supervisor de Operaciones', FALSE)
) AS x(email, notes, is_admin);

-- ==========================================
-- 5. Create the Initial Admin User
-- ==========================================
-- Password hash must be bcrypt. If password_hash is set, 
-- we enforce a password change upon first login via 'force_password_change = TRUE'.
WITH new_company AS (
    SELECT id FROM companies WHERE slug = '<COMPANY_SLUG, e.g., sesursa>' LIMIT 1
)
INSERT INTO users (password_hash, name, phone_number, email, is_admin, is_active, company_id, is_super_admin, force_password_change)
SELECT 
    -- Pre-generated Bcrypt hash for standard temporary password. Example: Tzolkin1!
    -- Hash: '$2b$12$R.HwP/hPsz/GZlqI.2tFse3jN2tM2W8T5Y0/Q0Wn2rO8GjN6fW3eK'
    '$2b$12$R.HwP/hPsz/GZlqI.2tFse3jN2tM2W8T5Y0/Q0Wn2rO8GjN6fW3eK', 
    '<ADMIN_FULL_NAME, e.g., Administrador General>', 
    '<ADMIN_PHONE, e.g., +51999888777>', 
    '<ADMIN_EMAIL, e.g., admin@sesursa.com>', 
    TRUE, -- is_admin
    TRUE, -- is_active
    new_company.id, 
    FALSE, -- is_super_admin (reserved for system-wide platform admins)
    TRUE   -- force_password_change (enforces prompt on first login)
FROM new_company
ON CONFLICT (email) DO NOTHING;

COMMIT;
