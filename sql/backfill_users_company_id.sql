-- Backfill de company_id para usuarios existentes.
--
-- Contexto: el registro (login_bp.register) no asignaba company_id, dejando a los
-- usuarios con company_id NULL. Eso provocaba "Acceso denegado" al actualizar el
-- estado de compromisos/incidentes y rompía la carga de módulos por empresa
-- (companies.enabled_modules).
--
-- SEKapp es single-tenant: se asigna la única empresa activa.
-- NOTA: intencionalmente NO se toca is_active para no reactivar cuentas que un
-- administrador haya desactivado a propósito.

UPDATE users
SET company_id = (SELECT id FROM companies WHERE is_active = TRUE ORDER BY id LIMIT 1),
    updated_at = NOW()
WHERE company_id IS NULL;

-- Verificación: no deberían quedar usuarios sin empresa.
-- SELECT id, email, is_active, company_id FROM users WHERE company_id IS NULL;
