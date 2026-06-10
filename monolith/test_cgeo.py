cliente = "Bodegas San Ignacio"
start_date = "2026-03-12"
end_date = "2026-06-10"

def _where(conds):
    return ("WHERE " + " AND ".join(conds)) if conds else ""

def _add_cliente(conds, params, cliente, alias=''):
    if not cliente:
        return
    try:
        conds.append(f"{alias}id_propiedad = %s")
        params.append(int(cliente))
    except (ValueError, TypeError):
        conds.append(f"TRIM({alias}cliente_instalacion) = %s")
        params.append(str(cliente).strip())

def _date_conds(date_col, conds, params):
    if start_date:
        conds.append(f"{date_col} >= %s")
        params.append(start_date)
    if end_date:
        conds.append(f"{date_col} <= %s")
        params.append(end_date)

inc_conds, inc_params = [], []
_add_cliente(inc_conds, inc_params, cliente)
_date_conds("fecha_hora", inc_conds, inc_params)
inc_where = _where(inc_conds)

query = f"""
            SELECT
                COUNT(*) AS total,
                SUM(CASE WHEN LOWER(TRIM(nivel_severidad)) IN ('crítico','critico') THEN 1 ELSE 0 END) AS criticos,
                SUM(CASE WHEN LOWER(TRIM(nivel_severidad)) = 'alto' THEN 1 ELSE 0 END) AS altos,
                SUM(CASE WHEN LOWER(TRIM(nivel_severidad)) IN ('medio','moderado') THEN 1 ELSE 0 END) AS medios,
                SUM(CASE WHEN LOWER(TRIM(nivel_severidad)) = 'bajo' THEN 1 ELSE 0 END) AS bajos
            FROM reportes_incidentes
            {inc_where}
        """

print("QUERY:")
print(query)
print("PARAMS:", tuple(inc_params))
print("NUM %s:", query.count("%s"))
print("NUM PARAMS:", len(inc_params))
print("ANY OTHER %:", query.replace("%s", "").count("%"))
