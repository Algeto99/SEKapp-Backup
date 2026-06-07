import re

with open("cgeo_bp.py", "r") as f:
    content = f.read()

# We need to replace all SELECTs that grab cliente_instalacion to grab p.nombre
# and add the LEFT JOIN.
# Due to the complexity of SQL strings in Python, we will manually define the replacements for the known ones.

replacements = [
    (
        """SELECT
                id,
                'Certificación' AS tipo,
                COALESCE(NULLIF(TRIM(curso_certificacion), ''), 'Certificación #' || id::text) AS elemento,
                cliente_instalacion AS cliente,
                'Vencida' AS estado,
                vigencia_hasta AS vencimiento,
                NULL::text AS extra
            FROM checklist_cumplimiento""",
        """SELECT
                c.id,
                'Certificación' AS tipo,
                COALESCE(NULLIF(TRIM(c.curso_certificacion), ''), 'Certificación #' || c.id::text) AS elemento,
                p.nombre AS cliente,
                'Vencida' AS estado,
                c.vigencia_hasta AS vencimiento,
                NULL::text AS extra
            FROM checklist_cumplimiento c
            LEFT JOIN propiedades p ON p.id_propiedad = c.id_propiedad"""
    ),
    (
        """SELECT
                id_planilla_vehicular,
                'Vehículo' AS tipo,
                COALESCE(NULLIF(TRIM(placa_vehiculo), ''), 'Vehículo #' || id_planilla_vehicular::text) AS elemento,
                cliente_instalacion AS cliente,
                'No apto' AS estado,
                NULL::date AS vencimiento
            FROM planilla_vehicular""",
        """SELECT
                v.id_planilla_vehicular,
                'Vehículo' AS tipo,
                COALESCE(NULLIF(TRIM(v.placa_vehiculo), ''), 'Vehículo #' || v.id_planilla_vehicular::text) AS elemento,
                p.nombre AS cliente,
                'No apto' AS estado,
                NULL::date AS vencimiento
            FROM planilla_vehicular v
            LEFT JOIN propiedades p ON p.id_propiedad = v.id_propiedad"""
    ),
    (
        """SELECT
                c.id,
                'Equipo' AS tipo,
                COALESCE(NULLIF(TRIM(elem->>'equipo'), ''),
                         NULLIF(TRIM(elem->>'tipo_equipo'), ''),
                         'Equipo') AS elemento,
                c.cliente_instalacion AS cliente,
                'Fuera de servicio' AS estado
            FROM confiabilidad_equipos c,""",
        """SELECT
                c.id,
                'Equipo' AS tipo,
                COALESCE(NULLIF(TRIM(elem->>'equipo'), ''),
                         NULLIF(TRIM(elem->>'tipo_equipo'), ''),
                         'Equipo') AS elemento,
                p.nombre AS cliente,
                'Fuera de servicio' AS estado
            FROM confiabilidad_equipos c
            LEFT JOIN propiedades p ON p.id_propiedad = c.id_propiedad,"""
    ),
    (
        """SELECT
                id,
                COALESCE(NULLIF(TRIM(curso_certificacion),''), 'Certificación #' || id::text) AS cert,
                cliente_instalacion AS cliente,
                vigencia_hasta,
                (vigencia_hasta - CURRENT_DATE) AS dias_restantes
            FROM checklist_cumplimiento""",
        """SELECT
                c.id,
                COALESCE(NULLIF(TRIM(c.curso_certificacion),''), 'Certificación #' || c.id::text) AS cert,
                p.nombre AS cliente,
                c.vigencia_hasta,
                (c.vigencia_hasta - CURRENT_DATE) AS dias_restantes
            FROM checklist_cumplimiento c
            LEFT JOIN propiedades p ON p.id_propiedad = c.id_propiedad"""
    ),
    (
        """SELECT
                cliente_instalacion AS instalacion,
                MAX(fecha) AS ultimo_reg,
                (CURRENT_DATE - MAX(fecha)) AS dias
            FROM confiabilidad_equipos""",
        """SELECT
                p.nombre AS instalacion,
                MAX(c.fecha) AS ultimo_reg,
                (CURRENT_DATE - MAX(c.fecha)) AS dias
            FROM confiabilidad_equipos c
            LEFT JOIN propiedades p ON p.id_propiedad = c.id_propiedad"""
    ),
    (
        """SELECT
                TRIM(placa_vehiculo) AS placa,
                cliente_instalacion AS cliente,
                MAX(COALESCE(fecha_hora, creado_en)) AS ultimo_preop,
                EXTRACT(EPOCH FROM (NOW() - MAX(COALESCE(fecha_hora, creado_en)))) / 3600 AS horas
            FROM planilla_vehicular""",
        """SELECT
                TRIM(v.placa_vehiculo) AS placa,
                p.nombre AS cliente,
                MAX(COALESCE(v.fecha_hora, v.creado_en)) AS ultimo_preop,
                EXTRACT(EPOCH FROM (NOW() - MAX(COALESCE(v.fecha_hora, v.creado_en)))) / 3600 AS horas
            FROM planilla_vehicular v
            LEFT JOIN propiedades p ON p.id_propiedad = v.id_propiedad"""
    ),
    (
        """SELECT
                id,
                cliente_instalacion AS instalacion,
                vigencia_hasta,
                (CURRENT_DATE - vigencia_hasta) AS dias_vencido,
                COALESCE(NULLIF(TRIM(curso_certificacion),''), 'Certificación #' || id::text) AS elemento
            FROM checklist_cumplimiento""",
        """SELECT
                c.id,
                p.nombre AS instalacion,
                c.vigencia_hasta,
                (CURRENT_DATE - c.vigencia_hasta) AS dias_vencido,
                COALESCE(NULLIF(TRIM(c.curso_certificacion),''), 'Certificación #' || c.id::text) AS elemento
            FROM checklist_cumplimiento c
            LEFT JOIN propiedades p ON p.id_propiedad = c.id_propiedad"""
    ),
    (
        """SELECT
                id_reporte_incidente AS id,
                CAST(COALESCE(fecha_hora, creado_en) AS date) AS fecha,
                COALESCE(NULLIF(TRIM(descripcion_incidente),''), tipo_incidente, 'Incidente') AS incidente,
                cliente_instalacion AS cliente,
                nivel_severidad AS severidad,
                COALESCE(estado, 'Abierto') AS estado,
                impacto
            FROM reportes_incidentes""",
        """SELECT
                r.id_reporte_incidente AS id,
                CAST(COALESCE(r.fecha_hora, r.creado_en) AS date) AS fecha,
                COALESCE(NULLIF(TRIM(r.descripcion_incidente),''), r.tipo_incidente, 'Incidente') AS incidente,
                p.nombre AS cliente,
                r.nivel_severidad AS severidad,
                COALESCE(r.estado, 'Abierto') AS estado,
                r.impacto
            FROM reportes_incidentes r
            LEFT JOIN propiedades p ON p.id_propiedad = r.id_propiedad"""
    ),
    (
        """SELECT
                    id_informe AS id,
                    'Novedad Disciplinaria' AS tipo,
                    CAST(COALESCE(fecha_hora, creado_en) AS date) AS fecha_compromiso,
                    COALESCE(NULLIF(TRIM(tipo_novedad),''), 'Novedad') AS compromiso,
                    cliente_instalacion AS cliente,
                    'Abierto' AS estado,
                    (CURRENT_DATE - CAST(COALESCE(fecha_hora, creado_en) AS date)) AS dias_retraso
                FROM informe_novedades_disciplinario""",
        """SELECT
                    i.id_informe AS id,
                    'Novedad Disciplinaria' AS tipo,
                    CAST(COALESCE(i.fecha_hora, i.creado_en) AS date) AS fecha_compromiso,
                    COALESCE(NULLIF(TRIM(i.tipo_novedad),''), 'Novedad') AS compromiso,
                    p.nombre AS cliente,
                    'Abierto' AS estado,
                    (CURRENT_DATE - CAST(COALESCE(i.fecha_hora, i.creado_en) AS date)) AS dias_retraso
                FROM informe_novedades_disciplinario i
                LEFT JOIN propiedades p ON p.id_propiedad = i.id_propiedad"""
    ),
    (
        """SELECT
                    id_visita AS id,
                    'Acta Visita' AS tipo,
                    CAST(COALESCE(fecha_hora, creado_en) AS date) AS fecha_compromiso,
                    COALESCE(NULLIF(TRIM(motivo_visita),''), 'Visita') AS compromiso,
                    cliente_instalacion AS cliente,
                    COALESCE(estado, 'Pendiente') AS estado,
                    (CURRENT_DATE - CAST(COALESCE(fecha_hora, creado_en) AS date)) AS dias_retraso
                FROM registro_y_acta_de_visita""",
        """SELECT
                    v.id_visita AS id,
                    'Acta Visita' AS tipo,
                    CAST(COALESCE(v.fecha_hora, v.creado_en) AS date) AS fecha_compromiso,
                    COALESCE(NULLIF(TRIM(v.motivo_visita),''), 'Visita') AS compromiso,
                    p.nombre AS cliente,
                    COALESCE(v.estado, 'Pendiente') AS estado,
                    (CURRENT_DATE - CAST(COALESCE(v.fecha_hora, v.creado_en) AS date)) AS dias_retraso
                FROM registro_y_acta_de_visita v
                LEFT JOIN propiedades p ON p.id_propiedad = v.id_propiedad"""
    )
]

for old, new in replacements:
    content = content.replace(old, new)

content = content.replace("TRIM(cliente_instalacion) AS puesto", "p.nombre AS puesto")
content = content.replace("GROUP BY TRIM(cliente_instalacion)", "GROUP BY p.nombre")
content = content.replace("TRIM(cliente_instalacion) AS cliente", "p.nombre AS cliente")

with open("cgeo_bp.py", "w") as f:
    f.write(content)

