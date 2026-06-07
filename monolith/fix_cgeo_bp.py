import re

with open("cgeo_bp.py", "r") as f:
    code = f.read()

# 1. Update cgeo_api_filtros
filtros_old = """        _FILTROS_PAIRS = {
            ("confiabilidad_equipos",  "cliente_instalacion"),
            ("planilla_vehicular",     "cliente_instalacion"),
            ("checklist_cumplimiento", "cliente_instalacion"),
            ("reportes_incidentes",    "cliente_instalacion"),
            ("supervision_puesto",     "cliente_instalacion"),
        }
        clientes = set()
        for tbl, col in _FILTROS_PAIRS:
            try:
                if (tbl, col) not in _FILTROS_PAIRS:
                    raise ValueError(f"Identifier ({tbl}, {col}) not in allowlist")
                if company_id is not None:
                    query = sql.SQL(
                        "SELECT DISTINCT TRIM({col}) AS c FROM {tbl}"
                        " WHERE {col} IS NOT NULL AND TRIM({col}) <> ''"
                        " AND company_id = %s ORDER BY c"
                    ).format(col=sql.Identifier(col), tbl=sql.Identifier(tbl))
                    cur.execute(query, (company_id,))
                else:
                    query = sql.SQL(
                        "SELECT DISTINCT TRIM({col}) AS c FROM {tbl}"
                        " WHERE {col} IS NOT NULL AND TRIM({col}) <> '' ORDER BY c"
                    ).format(col=sql.Identifier(col), tbl=sql.Identifier(tbl))
                    cur.execute(query)
                clientes.update(r["c"] for r in cur.fetchall())
            except Exception:
                pass
        return jsonify({"clientes": sorted(clientes)})"""

filtros_new = """        query = \"\"\"
            SELECT p.id_propiedad AS id, p.nombre AS name
            FROM propiedades p
            LEFT JOIN customer_companies cc ON p.customer_company_id = cc.id
            WHERE p.activa = TRUE OR p.activa IS NULL
        \"\"\"
        params = []
        if company_id is not None:
            query += " AND cc.company_id = %s"
            params.append(company_id)
            
        query += " ORDER BY p.nombre"
        
        cur.execute(query, tuple(params))
        clientes = [{"id": r["id"], "name": r["name"]} for r in cur.fetchall()]
        
        return jsonify({"clientes": clientes})"""

code = code.replace(filtros_old, filtros_new)

# 2. Update all the WHERE clauses from cliente_instalacion to id_propiedad
# We need to be careful with aliases.
code = code.replace("eq_conds.append(\"c.cliente_instalacion = %s\")", "eq_conds.append(\"c.id_propiedad = %s\")")
code = code.replace("veh_conds.append(\"cliente_instalacion = %s\")", "veh_conds.append(\"id_propiedad = %s\")")
code = code.replace("cum_conds.append(\"cliente_instalacion = %s\")", "cum_conds.append(\"id_propiedad = %s\")")
code = code.replace("inc_conds.append(\"cliente_instalacion = %s\")", "inc_conds.append(\"id_propiedad = %s\")")
code = code.replace("sat_conds.append(\"cliente_instalacion = %s\")", "sat_conds.append(\"id_propiedad = %s\")")
code = code.replace("sup_conds.append(\"cliente_instalacion = %s\")", "sup_conds.append(\"id_propiedad = %s\")")
code = code.replace("cap_conds.append(\"cliente_instalacion = %s\")", "cap_conds.append(\"id_propiedad = %s\")")
code = code.replace("disc_conds.append(\"cliente_instalacion = %s\")", "disc_conds.append(\"id_propiedad = %s\")")
code = code.replace("vis_conds.append(\"cliente_instalacion = %s\")", "vis_conds.append(\"id_propiedad = %s\")")

# For the dynamic _cp helpers in cgeo_bp.py:
# def _cp(col="cliente_instalacion"):
code = code.replace("def _cp(col=\"cliente_instalacion\"):", "def _cp(col=\"id_propiedad\"):")
code = code.replace("_cp(\"cliente_instalacion\")", "_cp(\"id_propiedad\")")

# 3. Update cliente parsing to handle 'Todos' correctly
code = code.replace("cliente = request.args.get(\"cliente\") or None", "cliente = request.args.get(\"cliente\")\n    if cliente in ('Todos', ''):\n        cliente = None")


with open("cgeo_bp.py", "w") as f:
    f.write(code)

