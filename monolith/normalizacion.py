"""Normalización de los campos de captura manual.

Unifica cómo se guarda lo que se escribe a mano en los formularios, para que un
mismo Oficial de Seguridad o un mismo activo no terminen partidos en registros
distintos sólo por diferencias de digitación.

Dos reglas, y nada más:

  · Nombres y denominaciones → MAYÚSCULAS, sin espacios al inicio ni al final y
    sin espacios dobles. Las tildes se conservan: "juan  pérez " → "JUAN PÉREZ".

  · Identificadores (número de empleado, documento, licencia, serie, matrícula)
    → MAYÚSCULAS y sin espacios. Al comparar se ignoran además guiones, puntos y
    barras, así que "eg-4251", "EG 4251" y "eg4251" son el mismo identificador.

Sólo se tocan los campos que identifican a una persona, un activo o un registro.
Las observaciones, descripciones, acciones tomadas y demás texto libre se guardan
exactamente como se escribieron.

Dos cosas quedan deliberadamente fuera:

  · Los catálogos que llegan del onboarding. `cliente_instalacion` y las placas de
    flota se eligen de una lista, no se digitan, y `_resolve_scope_fields` incluso
    reescribe `cliente_instalacion` con el nombre exacto de `propiedades`.
    Normalizarlos desalinearía los registros del catálogo del que salieron.

  · Los registros históricos. Nada de esto reescribe lo ya capturado: la
    normalización aplica de aquí en adelante. Para que las búsquedas igual
    reconozcan lo viejo, la comparación se normaliza del lado de la consulta con
    `sql_clave_identificador` y `sql_clave_nombre`.
"""

import re

_ESPACIOS = re.compile(r'\s+')

# Separadores que se ignoran al comparar identificadores. No se borran al
# guardar: "EG-4251" se conserva legible tal como lo escribió el Supervisor de
# Seguridad, y es la clave de comparación la que los descarta.
_SEPARADORES = ('-', '.', '/')


def normalizar_nombre(valor):
    """MAYÚSCULAS, sin espacios sobrantes, tildes intactas. Lo no-texto pasa igual."""
    if not isinstance(valor, str):
        return valor
    return _ESPACIOS.sub(' ', valor).strip().upper()


def normalizar_identificador(valor):
    """MAYÚSCULAS y sin espacios, conservando guiones, puntos y barras."""
    if not isinstance(valor, str):
        return valor
    return _ESPACIOS.sub('', valor).upper()


def clave_identificador(valor):
    """Clave de comparación: lo anterior, más guiones, puntos y barras fuera.

    Es lo que hace que "eg-4251" y "EG 4251" se reconozcan como el mismo
    identificador. Se usa para comparar, nunca para guardar.
    """
    texto = normalizar_identificador(valor)
    if not isinstance(texto, str):
        return texto
    for separador in _SEPARADORES:
        texto = texto.replace(separador, '')
    return texto


# ── Qué campo es qué ─────────────────────────────────────────────────────────
# Por (tabla, columna) y no sólo por nombre de columna: `nombre_responsable` es
# un nombre suelto en Incidentes y en las planillas, pero en el Acta de Visita es
# una cadena JSON con la lista de responsables. Normalizarla como texto
# uppercasearía las claves del JSON y rompería a quien lo lee.

NOMBRES = {
    'supervision_puesto': {
        'supervisor', 'nombre_guardia', 'nombre_guardia_firma',
        'marca_radio', 'tipo_radio',
    },
    'informe_novedades_disciplinario': {
        'nombre_responsable', 'realizado_por_cargo', 'dirigido_a',
        'empleado_nombre', 'empleado_cargo', 'nombre_testigo',
        'recibido_revisado_por_nombre', 'recibido_revisado_por_cargo',
        'sitio_ocurrencia', 'otras_personas_involucradas',
        'puesto_area_especifica',
    },
    'reportes_incidentes': {
        'nombre_responsable', 'nombre_persona_cubre',
        'nombre_responsable_plan', 'puesto_area_especifica',
    },
    # `responsable_asignado` queda fuera a proposito: no se digita, lo escribe el
    # flujo de "Asignar hallazgo" con el nombre que ya tiene el usuario en el
    # catalogo. Normalizarlo lo desalinearia de `users.name`.
    'reportes_incidentes_personas': {'persona_nombre'},
    'log_de_patrullas': {'id_guardia_nombre_guardia', 'sitio_ubicacion'},
    'registro_de_capacitaciones': {
        'nombre_responsable', 'cargo_responsable', 'nombre_capacitacion',
        'puesto_area_especifica',
    },
    'capacitacion_asistencia': {'nombre', 'cargo'},
    'registro_y_acta_de_visita': {
        'visita_realizada_por', 'nombre_visitante', 'cargo_visitante',
        'persona_atendio', 'cargo_atendio', 'puesto_area_especifica',
    },
    'medicion_experiencia_cliente': {'nombre_responsable', 'encuestado'},
    'checklist_cumplimiento': {
        'nombre_auditor', 'agente_nombre_completo', 'agente_cargo_rol',
        'agente_puesto', 'academia_certifica', 'puesto_area_especifica',
    },
    'confiabilidad_equipos': {
        'tecnico_mantenimiento', 'supervisor_seguridad', 'sitio',
    },
    'planilla_vehicular': {
        'nombre_responsable', 'oficial_operaciones_nombre',
        'puesto_area_especifica',
    },
    'planilla_motocicletas': {
        'nombre_responsable', 'oficial_operaciones_nombre',
        'puesto_area_especifica',
    },
}

IDENTIFICADORES = {
    'supervision_puesto': {
        'documento_guardia', 'numero_empleado', 'serie_arma',
        'matricula_arma', 'licencia_portar_arma', 'radio_asignado_serial',
    },
    'informe_novedades_disciplinario': {'empleado_documento', 'empleado_numero'},
    'reportes_incidentes': {
        'numero_empleado', 'numero_empleado_cubre', 'numero_reporte_autoridades',
    },
    'capacitacion_asistencia': {'documento', 'numero_empleado'},
    'checklist_cumplimiento': {
        'agente_numero_documento', 'agente_numero_empleado', 'nro_resolucion',
    },
    'log_de_patrullas': {'id_patrulla_consecutivo'},
    'planilla_vehicular': {'numero_empleado'},
    'planilla_motocicletas': {'numero_empleado'},
}

# Claves normalizadas dentro de las columnas JSON, para las listas que los
# formularios arman a mano (asistentes de Capacitación, participantes y
# responsables del Acta de Visita).
JSON_NOMBRES = ('nombre', 'cargo')
JSON_IDENTIFICADORES = ('documento', 'numero_empleado')


def normalizar_fila(tabla, datos):
    """Devuelve una copia de `datos` con los campos de captura manual normalizados.

    Lo que no esté en el registro de la tabla se copia sin tocar, igual que los
    valores que no son texto (`psycopg2.extras.Json`, números, fechas).
    """
    nombres = NOMBRES.get(tabla, ())
    identificadores = IDENTIFICADORES.get(tabla, ())
    if not nombres and not identificadores:
        return dict(datos)

    normalizados = {}
    for clave, valor in datos.items():
        if clave in identificadores:
            normalizados[clave] = normalizar_identificador(valor)
        elif clave in nombres:
            normalizados[clave] = normalizar_nombre(valor)
        else:
            normalizados[clave] = valor
    return normalizados


def normalizar_json_filas(filas):
    """Normaliza en sitio las listas de dicts que viajan dentro de una columna JSON.

    Las claves del dict no se tocan —sólo sus valores— para no romper a quien lee
    el JSON después.
    """
    if not isinstance(filas, list):
        return filas
    for fila in filas:
        if not isinstance(fila, dict):
            continue
        for clave in JSON_NOMBRES:
            if clave in fila:
                fila[clave] = normalizar_nombre(fila[clave])
        for clave in JSON_IDENTIFICADORES:
            if clave in fila:
                fila[clave] = normalizar_identificador(fila[clave])
    return filas


# ── Comparación del lado de la consulta ──────────────────────────────────────
# Los registros históricos no se reescriben, así que la consulta es la que tiene
# que poner a ambos lados en el mismo terreno. Sin esto, un "eg-4251" capturado
# el mes pasado seguiría contando como un activo distinto del "EG4251" de hoy.

def sql_clave_identificador(expresion):
    """SQL de `clave_identificador`: mayúsculas, sin espacios ni - . /

    Sólo usa UPPER/TRIM/REPLACE, disponibles tanto en PostgreSQL como en el
    SQLite con el que corren las pruebas de consolidación.
    """
    sql = f"UPPER(TRIM({expresion}))"
    for separador in (' ',) + _SEPARADORES:
        sql = f"REPLACE({sql}, '{separador}', '')"
    return sql


def sql_clave_nombre(expresion):
    """SQL de `normalizar_nombre`: mayúsculas y espacios internos colapsados."""
    return f"UPPER(BTRIM(REGEXP_REPLACE({expresion}::TEXT, '\\s+', ' ', 'g')))"
