"""Registro de auditoría (Log de Eventos) de SEKapp.

Pedido de KANAN el 2026-09-28: dejar constancia de quién hizo qué, sobre qué
registro y cuándo, y poder consultarlo en Administración → Auditoría.

Cómo funciona
-------------
- Un hook `after_request` (registrado en app.py) busca el endpoint de la petición
  en CATALOGO. Si está, inserta una fila en `eventos_auditoria` con el usuario del
  JWT, el módulo, el tipo de evento, la acción, el registro afectado y el resultado
  (status HTTP). Lo que no está en el catálogo no se registra: así quedan fuera el
  favicon, el service worker, el health check, el token CSRF, el polling del
  contador de hallazgos y las llamadas de datos y filtros de los dashboards.
- Los handlers que saben algo que la petición no dice (el id del registro recién
  insertado, el estado anterior, el alcance de un QR) lo agregan con `anotar()`.
- La escritura es síncrona y en su propia conexión: con minScale 0 y sin afinidad
  de sesión en Cloud Run, una cola en memoria perdería eventos. Nunca interrumpe
  la petición: cualquier fallo se anota en el log de aplicación y se sigue.
- La tabla se crea sola la primera vez que un proceso escribe (producción no tiene
  acceso directo a la base) y también está en sql/schema.sql y en
  sql/create_eventos_auditoria.sql. Dos triggers rechazan UPDATE, DELETE y
  TRUNCATE: la app nunca modifica ni borra eventos.
- Las fechas se guardan en UTC (TIMESTAMPTZ) y la pantalla las muestra en la zona
  horaria de Umbrales KPI, igual que el PDF del Morning Briefing.
"""
import logging
import time
from datetime import datetime, timedelta

import psycopg2
from psycopg2 import extras
from flask import current_app, g, has_request_context, request

from db import get_db_connection

app_logger = logging.getLogger(__name__)

TABLA = 'eventos_auditoria'
ORIGEN_APP = 'app'
ORIGEN_RETROACTIVO = 'retroactivo'

# --- Vocabulario -------------------------------------------------------------

TIPOS_EVENTO = {
    'sesion':     'Sesión',
    'consulta':   'Consulta',
    'envio':      'Envío de formulario',
    'edicion':    'Edición',
    'estado':     'Cambio de estado',
    'asignacion': 'Gestión de hallazgos',
    'descarga':   'Descarga / exportación',
    'admin':      'Administración',
    'publico':    'Vista pública (QR)',
}

ESTADOS = ('Exitoso', 'Rechazado', 'No encontrado', 'Error', 'Pendiente')

# Nombres tal como aparecen en el selector de formularios.
FORMULARIOS = {
    'supervision_puesto':              'Control de Supervisión',
    'reporte_incidente':               'Reporte de Incidente',
    'log_de_patrullas':                'Log de Patrullas',
    'confiabilidad_equipos':           'Confiabilidad de Equipos',
    'planilla_vehicular':              'Planilla Pre-Operacional Vehicular',
    'planilla_motocicletas':           'Planilla Pre-Operacional de Motocicletas',
    'registro_de_capacitaciones':      'Control de Capacitaciones',
    'informe_novedades_disciplinario': 'Reporte Disciplinario',
    'registro_y_acta_de_visita':       'Acta de Visita a Cliente',
    'checklist_cumplimiento':          'Checklist de Cumplimiento Normativo',
    'medicion_experiencia_cliente':    'Encuesta de Cliente',
    'asistencia_qr':                   'Asistencia por QR',
}

# Acciones que sólo aparecen vía anotar() y deben salir igual en el filtro.
ACCIONES_EXTRA = (
    'Inicio de sesión fallido',
    'Aviso de sesión activa',
)

# --- Catálogo de endpoints ---------------------------------------------------
#
# Clave: "endpoint#METODO". Sólo lo que está aquí se registra.
#   modulo      sección de SEKapp tal como la ve el usuario
#   tipo        clave de TIPOS_EVENTO
#   accion      texto fijo del evento
#   formulario  form_type cuando el endpoint es de un formulario concreto
#   args        True → los parámetros de la URL van a detalle['filtros']
#   form        campos del formulario HTML que van a detalle ('*' = todos)
#   json        campos del cuerpo JSON que van a detalle ('*' = todos)

CATALOGO = {}


def _e(modulo, tipo, accion, formulario=None, args=False, form=(), json=()):
    return {'modulo': modulo, 'tipo': tipo, 'accion': accion, 'formulario': formulario,
            'args': args, 'form': tuple(form) if form != '*' else '*',
            'json': tuple(json) if json != '*' else '*'}


def _reg(endpoint, entrada, metodos=('GET',)):
    for m in metodos:
        CATALOGO[f'{endpoint}#{m}'] = entrada


# Acceso
_reg('login_bp.login',           _e('Acceso', 'sesion', 'Inicio de sesión'), ('POST',))
_reg('login_bp.logout',          _e('Acceso', 'sesion', 'Cierre de sesión'))
_reg('viewer.logout',            _e('Acceso', 'sesion', 'Cierre de sesión'))
_reg('dashboard_bp.logout',      _e('Acceso', 'sesion', 'Cierre de sesión'))
_reg('forms_bp.logout',          _e('Acceso', 'sesion', 'Cierre de sesión'))
_reg('login_bp.change_password', _e('Acceso', 'sesion', 'Cambio de contraseña', form=('email',)), ('POST',))
_reg('login_bp.forgot_password', _e('Acceso', 'sesion', 'Solicitud de restablecimiento de contraseña',
                                    form=('email',)), ('POST',))
_reg('login_bp.reset_password',  _e('Acceso', 'sesion', 'Restablecimiento de contraseña'), ('POST',))
_reg('login_bp.register',        _e('Acceso', 'sesion', 'Registro de usuario', form=('email', 'name')), ('POST',))

# Inicio
_reg('landing_bp.landing_page',  _e('Inicio', 'consulta', 'Acceso a la plataforma'))

# Formularios: página, envío, página de edición y envío de edición por formulario.
_FORMS = {
    'reporte_incidente':               ('reporte_incidente_form', 'submit_incident_report',
                                        'reporte_incidente_editar_form', 'submit_incident_report_editar'),
    'medicion_experiencia_cliente':    ('medicion_experiencia_cliente_form', 'submit_medicion_experiencia_cliente',
                                        'medicion_experiencia_cliente_editar_form', 'submit_medicion_experiencia_cliente_editar'),
    'supervision_puesto':              ('supervision_puesto_form', 'submit_supervision_puesto',
                                        'supervision_puesto_editar_form', 'submit_supervision_puesto_editar'),
    'informe_novedades_disciplinario': ('informe_novedades_disciplinario_form', 'submit_informe_novedades_disciplinario',
                                        'informe_novedades_disciplinario_editar_form', 'submit_informe_novedades_disciplinario_editar'),
    'log_de_patrullas':                ('log_de_patrullas_form', 'submit_log_de_patrullas',
                                        'log_de_patrullas_editar_form', 'submit_log_de_patrullas_editar'),
    'registro_de_capacitaciones':      ('registro_de_capacitaciones_form', 'submit_registro_de_capacitaciones',
                                        'registro_de_capacitaciones_editar_form', 'submit_registro_de_capacitaciones_editar'),
    'registro_y_acta_de_visita':       ('registro_y_acta_de_visita_form', 'submit_registro_y_acta_de_visita',
                                        'registro_y_acta_de_visita_editar_form', 'submit_registro_y_acta_de_visita_editar'),
    'planilla_vehicular':              ('planilla_vehicular_form', 'submit_planilla_vehicular',
                                        'planilla_vehicular_editar_form', 'submit_planilla_vehicular_editar'),
    'planilla_motocicletas':           ('planilla_motocicletas_form', 'submit_planilla_motocicletas',
                                        'planilla_motocicletas_editar_form', 'submit_planilla_motocicletas_editar'),
    'checklist_cumplimiento':          ('checklist_cumplimiento', 'submit_checklist_cumplimiento',
                                        'checklist_cumplimiento_editar_form', 'submit_checklist_cumplimiento_editar'),
    'confiabilidad_equipos':           ('confiabilidad_equipos_form', 'submit_confiabilidad_equipos',
                                        'confiabilidad_equipos_editar_form', 'submit_confiabilidad_equipos_editar'),
}
_reg('forms_bp.select_form', _e('Formularios', 'consulta', 'Consulta del módulo Formularios'))
for _ft, (_pag, _env, _pag_ed, _env_ed) in _FORMS.items():
    _reg(f'forms_bp.{_pag}',    _e('Formularios', 'consulta', 'Apertura de formulario', _ft))
    # Dos páginas de formulario aceptan POST y delegan en su submit_*: el evento es el envío.
    if _pag in ('checklist_cumplimiento', 'registro_de_capacitaciones_form'):
        _reg(f'forms_bp.{_pag}', _e('Formularios', 'envio', 'Envío de formulario', _ft), ('POST',))
    _reg(f'forms_bp.{_env}',    _e('Formularios', 'envio', 'Envío de formulario', _ft), ('POST',))
    _reg(f'forms_bp.{_pag_ed}', _e('Formularios', 'consulta', 'Apertura de registro para edición', _ft))
    _reg(f'forms_bp.{_env_ed}', _e('Formularios', 'edicion', 'Edición de registro', _ft,
                                   form=('motivo', 'motivo_detalle')), ('POST',))
_reg('forms_bp.get_my_reports',        _e('Formularios', 'consulta', 'Consulta de mis reportes', args=True))
_reg('forms_bp.get_my_report_details', _e('Formularios', 'consulta', 'Visualización de detalle', 'reporte_incidente'))
_reg('forms_bp.asistencia_qr_form',    _e('Vista pública (QR)', 'publico', 'Apertura de asistencia por QR', 'asistencia_qr'))
_reg('forms_bp.submit_asistencia_qr',  _e('Vista pública (QR)', 'publico', 'Registro de asistencia por QR', 'asistencia_qr',
                                          form=('nombre', 'cargo', 'numero_empleado')), ('POST',))

# Reportes (viewer)
_reg('viewer.index',                      _e('Reportes', 'consulta', 'Consulta del módulo Reportes'))
_reg('viewer.get_more_reports',           _e('Reportes', 'consulta', 'Consulta de listado de reportes', args=True))
_reg('viewer.get_single_report',          _e('Reportes', 'consulta', 'Visualización de detalle'))
_reg('viewer.export_excel',               _e('Reportes', 'descarga', 'Exportación a Excel',
                                             json=('reports', 'report_ids')), ('POST',))
_reg('viewer.generate_pdf',               _e('Reportes', 'descarga', 'Generación de PDF',
                                             json=('reports', 'report_ids')), ('POST',))
_reg('viewer.email_selected_reports_api', _e('Reportes', 'descarga', 'Envío de reportes por correo',
                                             json=('reports', 'report_ids', 'recipient_email')), ('POST',))
_reg('viewer.generar_backup',             _e('Reportes', 'descarga', 'Generación de backup',
                                             json=('start_date', 'end_date', 'cliente', 'property_id')), ('POST',))

# Dashboards: una consulta por carga de página y una visualización por modal de detalle.
_DASHBOARDS = {
    'incidentes':   ('Dashboards: Incidentes',        'reporte_incidente'),
    'satisfaccion': ('Dashboards: Satisfacción',      'medicion_experiencia_cliente'),
    'supervision':  ('Dashboards: Supervisión',       'supervision_puesto'),
    'cumplimiento': ('Dashboards: Cumplimiento',      'checklist_cumplimiento'),
    'capacitacion': ('Dashboards: Capacitación',      'registro_de_capacitaciones'),
    'disciplina':   ('Dashboards: Disciplina',        'informe_novedades_disciplinario'),
    'visitas':      ('Dashboards: Visitas',           'registro_y_acta_de_visita'),
    'vehiculos':    ('Dashboards: Vehículos',         'planilla_vehicular'),
    'motocicletas': ('Dashboards: Motocicletas',      'planilla_motocicletas'),
    'equipos':      ('Dashboards: Equipos',           'confiabilidad_equipos'),
}
_reg('dashboard_bp.dashboard_home',    _e('Dashboards', 'consulta', 'Consulta del módulo Dashboards'))
_reg('dashboard_bp.dashboard_gestion', _e('Dashboards: Estatus de Cliente', 'consulta', 'Consulta de módulo'))
for _clave, (_mod, _ft) in _DASHBOARDS.items():
    _reg(f'dashboard_bp.dashboard_{_clave}',    _e(_mod, 'consulta', 'Consulta de módulo'))
    _reg(f'dashboard_bp.api_{_clave}_detalles', _e(_mod, 'consulta', 'Visualización de detalle', _ft, args=True))
_reg('dashboard_bp.api_incidentes_update_estado', _e('Dashboards: Incidentes', 'estado', 'Cambio de estado',
                                                     'reporte_incidente'), ('PUT',))
_reg('dashboard_bp.api_visitas_update_estado',    _e('Dashboards: Visitas', 'estado', 'Cambio de estado de compromiso',
                                                     'registro_y_acta_de_visita'), ('PUT',))
_reg('dashboard_bp.api_incidentes_historial',     _e('Dashboards: Incidentes', 'consulta', 'Consulta de historial de ediciones',
                                                     'reporte_incidente'))
_reg('dashboard_bp.api_visitas_historial',        _e('Dashboards: Visitas', 'consulta', 'Consulta de historial de ediciones',
                                                     'registro_y_acta_de_visita'))
_reg('dashboard_bp.api_report_details',           _e('Dashboards', 'consulta', 'Visualización de detalle', 'reporte_incidente'))
_reg('dashboard_bp.dashboard_bases_de_datos',     _e('Bases de Datos', 'consulta', 'Consulta de módulo'))

# Centro de gestión (cgeo)
_reg('cgeo_bp.cgeo_hub',                  _e('Centro de Gestión', 'consulta', 'Consulta de módulo'))
_reg('cgeo_bp.cgeo_recursos',             _e('Recursos y Confiabilidad', 'consulta', 'Consulta de módulo'))
_reg('cgeo_bp.cgeo_operacion',            _e('Operación e Incidentes', 'consulta', 'Consulta de módulo'))
_reg('cgeo_bp.cgeo_morning_briefing',     _e('Morning Briefing', 'consulta', 'Consulta de módulo'))
_reg('cgeo_bp.cgeo_morning_briefing_pdf', _e('Morning Briefing', 'descarga', 'Generación de PDF'), ('POST',))
_reg('cgeo_bp.cgeo_api_operacion_qr_url', _e('Operación e Incidentes', 'descarga', 'Generación de enlace público (QR)', args=True))
_reg('cgeo_bp.cgeo_operacion_publica',    _e('Vista pública (QR)', 'publico', 'Consulta pública de Operación e Incidentes'))
_reg('cgeo_bp.mis_hallazgos',             _e('Hallazgos', 'consulta', 'Consulta de hallazgos asignados'))
_reg('cgeo_bp.ver_hallazgo',              _e('Hallazgos', 'consulta', 'Visualización de hallazgo'))
_reg('cgeo_bp.asignar_hallazgo',          _e('Hallazgos', 'asignacion', 'Asignación de hallazgo',
                                             json=('form_type', 'record_id', 'asignado_a', 'asignado_email',
                                                   'fecha_limite', 'hallazgo_ref', 'hallazgo_titulo')), ('POST',))
_reg('cgeo_bp.gestionar_asignacion',      _e('Hallazgos', 'asignacion', 'Gestión de hallazgo', json=('nota',)), ('POST',))
_reg('cgeo_bp.revisar_alerta_seguridad',  _e('Morning Briefing', 'estado', 'Revisión de alerta de seguridad'), ('POST',))

# Expediente
_reg('expediente.expediente_index',          _e('Expediente', 'consulta', 'Consulta de módulo'))
_reg('expediente.api_feed',                  _e('Expediente', 'consulta', 'Consulta de línea de tiempo', args=True))
_reg('expediente.generate_supervision_qr',   _e('Expediente', 'descarga', 'Generación de código QR de supervisión',
                                                'supervision_puesto'))
_reg('expediente.api_qr_url',                _e('Expediente', 'descarga', 'Generación de enlace público de supervisión',
                                                'supervision_puesto'))
_reg('expediente.api_qr_expediente_url',     _e('Expediente', 'descarga', 'Generación de enlace público de expediente', args=True))
_reg('expediente.api_qr_expediente_email',   _e('Expediente', 'descarga', 'Envío de expediente por correo',
                                                json=('cliente', 'to_email')), ('POST',))
_reg('expediente.public_expediente_viewer',  _e('Vista pública (QR)', 'publico', 'Consulta pública de expediente'))
_reg('expediente.public_evidence_viewer',    _e('Vista pública (QR)', 'publico', 'Consulta pública de supervisión',
                                                'supervision_puesto'))

# Matrices
_reg('matrices_bp.matrices_hub', _e('Consultar Matrices', 'consulta', 'Consulta de módulo'))

# Administración
_reg('admin_bp.panel',                 _e('Panel de Administración', 'consulta', 'Consulta del panel'))
_reg('admin_bp.create_user',           _e('Panel de Administración', 'admin', 'Creación de usuario',
                                          form=('email', 'name', 'is_admin', 'company_id', 'force_password_change')), ('POST',))
_reg('admin_bp.toggle_admin',          _e('Panel de Administración', 'admin', 'Cambio de rol de administrador'), ('POST',))
_reg('admin_bp.toggle_company_module', _e('Panel de Administración', 'admin', 'Cambio de módulo de licencia',
                                          form=('module_key',)), ('POST',))
_reg('admin_bp.toggle_active',         _e('Panel de Administración', 'admin', 'Activación o desactivación de usuario'), ('POST',))
_reg('admin_bp.assign_company_all',    _e('Panel de Administración', 'admin', 'Asignación de empresa a usuarios'), ('POST',))
_reg('admin_bp.toggle_force_password', _e('Panel de Administración', 'admin', 'Cambio de forzar contraseña'), ('POST',))
_reg('admin_bp.reset_password',        _e('Panel de Administración', 'admin', 'Restablecimiento de contraseña de usuario'), ('POST',))
_reg('admin_bp.thresholds',            _e('Umbrales KPI', 'consulta', 'Consulta de Umbrales KPI'))
_reg('admin_bp.save_thresholds',       _e('Umbrales KPI', 'admin', 'Modificación de Umbrales KPI', form='*'), ('POST',))
_reg('admin_bp.auditoria',             _e('Auditoría', 'consulta', 'Apertura de Auditoría', args=True))
_reg('admin_bp.api_auditoria',         _e('Auditoría', 'consulta', 'Consulta del log de eventos', args=True))
_reg('admin_bp.api_auditoria_export',  _e('Auditoría', 'descarga', 'Exportación del log de eventos a Excel', args=True))


def modulos():
    return sorted({v['modulo'] for v in CATALOGO.values()})


def acciones():
    return sorted({v['accion'] for v in CATALOGO.values()} | set(ACCIONES_EXTRA))


# --- Anotaciones desde los handlers -----------------------------------------

def anotar(**campos):
    """Agrega datos al evento de la petición en curso.

    Campos: usuario_email, usuario_nombre, sesion_jti, tipo_evento, modulo, accion,
    formulario, registro_id, estado, detalle (dict, se fusiona). `omitir=True`
    evita registrar la petición. Fuera de una petición no hace nada.
    """
    if not has_request_context():
        return
    datos = getattr(g, '_auditoria', None)
    if datos is None:
        datos = g._auditoria = {}
    detalle = campos.pop('detalle', None)
    if detalle:
        datos.setdefault('detalle', {}).update(detalle)
    datos.update({k: v for k, v in campos.items() if v is not None})


def anotar_registro(registro_id):
    """Para envíos que insertan una o varias filas: la primera queda como
    registro_id y, si hay más, todas van en detalle['registros']."""
    if not has_request_context() or registro_id is None:
        return
    datos = getattr(g, '_auditoria', None)
    if datos is None:
        datos = g._auditoria = {}
    ids = datos.setdefault('_registros', [])
    ids.append(int(registro_id))
    datos['registro_id'] = ids[0]
    if len(ids) > 1:
        datos.setdefault('detalle', {})['registros'] = list(ids)


# --- Hook de respuesta -------------------------------------------------------

_CLAVES_ID = ('registro_id', 'record_id', 'id', 'report_id', 'id_reporte', 'id_visita',
              'id_supervision', 'asignacion_id', 'alerta_id', 'user_id', 'company_id')
_CLAVES_SECRETAS = ('password', 'contrase', 'csrf', 'firma', 'signature')
_MAX_TEXTO = 300
_MAX_LISTA = 200
_MAX_CLAVES = 40


def registrar_respuesta(response):
    """after_request: registra la petición si su endpoint está en el catálogo."""
    try:
        _registrar_desde_peticion(response)
    except Exception as e:  # nunca interrumpir la respuesta
        try:
            current_app.logger.warning(f"Auditoría omitida en {request.method} {request.path}: {e}")
        except Exception:
            pass
    return response


def _registrar_desde_peticion(response):
    endpoint = request.endpoint
    if not endpoint:
        return
    entrada = CATALOGO.get(f'{endpoint}#{request.method}')
    if not entrada:
        return
    datos = dict(getattr(g, '_auditoria', None) or {})
    if datos.pop('omitir', False):
        return

    usuario_email, usuario_nombre, jti = _identidad(datos)
    if entrada['tipo'] == 'publico' and not usuario_email:
        usuario_nombre = usuario_nombre or 'Público (QR)'

    detalle = {}
    if entrada['args'] and request.args:
        detalle['filtros'] = _compactar(request.args.to_dict(flat=True))
    if entrada['form']:
        detalle.update(_campos(request.form, entrada['form']))
    if entrada['json']:
        cuerpo = request.get_json(silent=True) if request.is_json else None
        if isinstance(cuerpo, dict):
            detalle.update(_campos(cuerpo, entrada['json']))
    if datos.get('detalle'):
        detalle.update(_compactar(datos['detalle']))

    registro_id = datos.get('registro_id')
    if registro_id is None:
        registro_id = _registro_desde_peticion()
    formulario = datos.get('formulario') or entrada['formulario'] or _formulario_desde_peticion()

    from login_bp import _dispositivo_desde_user_agent, _ip_cliente  # import perezoso: login_bp importa este módulo
    ua = request.user_agent.string or ''
    registrar_evento(
        tipo_evento=datos.get('tipo_evento') or entrada['tipo'],
        modulo=datos.get('modulo') or entrada['modulo'],
        accion=datos.get('accion') or entrada['accion'],
        usuario_email=usuario_email,
        usuario_nombre=usuario_nombre,
        sesion_jti=jti,
        formulario=formulario,
        registro_id=registro_id,
        detalle=detalle or None,
        estado=datos.get('estado') or _estado_por_status(response.status_code),
        http_status=response.status_code,
        metodo_ruta=f'{request.method} {request.path}',
        dispositivo=_dispositivo_desde_user_agent(ua),
        user_agent=ua,
        ip=_ip_cliente(),
    )


def _identidad(datos):
    email = datos.get('usuario_email')
    nombre = datos.get('usuario_nombre')
    jti = datos.get('sesion_jti')
    claims = {}
    try:
        from flask_jwt_extended import get_jwt, verify_jwt_in_request
        verify_jwt_in_request(optional=True)
        claims = get_jwt() or {}
    except Exception:
        claims = {}
    return (email or claims.get('sub'), nombre or claims.get('name'), jti or claims.get('jti'))


def _estado_por_status(status):
    if status < 400:
        return 'Exitoso'
    if status == 404:
        return 'No encontrado'
    if status < 500:
        return 'Rechazado'
    return 'Error'


def _registro_desde_peticion():
    va = request.view_args or {}
    for clave in _CLAVES_ID:
        valor = va.get(clave)
        if isinstance(valor, int):
            return valor
    cuerpo = request.get_json(silent=True) if request.is_json else None
    fuente = cuerpo if isinstance(cuerpo, dict) else request.form
    for clave in ('record_id', 'registro_id'):
        try:
            return int(fuente.get(clave))
        except (TypeError, ValueError):
            continue
    return None


def _formulario_desde_peticion():
    for fuente in (request.args, request.form):
        valor = fuente.get('form_type') or fuente.get('formType')
        if valor:
            return str(valor)[:60]
    cuerpo = request.get_json(silent=True) if request.is_json else None
    if isinstance(cuerpo, dict):
        valor = cuerpo.get('form_type') or cuerpo.get('formType')
        if valor:
            return str(valor)[:60]
    return None


def _es_secreta(clave):
    c = str(clave).lower()
    return any(s in c for s in _CLAVES_SECRETAS)


def _campos(fuente, claves):
    """Copia campos de un formulario o cuerpo JSON, sin contraseñas ni firmas."""
    if fuente is None:
        return {}
    if claves == '*':
        if hasattr(fuente, 'to_dict'):
            crudo = fuente.to_dict(flat=False)
            crudo = {k: (v[0] if len(v) == 1 else v) for k, v in crudo.items()}
        else:
            crudo = dict(fuente)
        return _compactar({k: v for k, v in crudo.items() if not _es_secreta(k)})
    salida = {}
    for clave in claves:
        if _es_secreta(clave):
            continue
        valor = fuente.get(clave)
        if valor not in (None, ''):
            salida[clave] = _compactar(valor)
    return salida


def _compactar(valor, nivel=0):
    """Acota lo que va al JSONB: textos, listas largas y diccionarios anidados."""
    if valor is None or isinstance(valor, (bool, int, float)):
        return valor
    if isinstance(valor, str):
        return valor if len(valor) <= _MAX_TEXTO else valor[:_MAX_TEXTO] + '…'
    if isinstance(valor, dict):
        if nivel >= 3:
            return f'{{…{len(valor)} claves}}'
        items = list(valor.items())[:_MAX_CLAVES]
        return {str(k)[:60]: _compactar(v, nivel + 1) for k, v in items}
    if isinstance(valor, (list, tuple, set)):
        lista = list(valor)
        salida = [_compactar(v, nivel + 1) for v in lista[:_MAX_LISTA]]
        if len(lista) > _MAX_LISTA:
            salida.append(f'…(+{len(lista) - _MAX_LISTA})')
        return salida
    if isinstance(valor, (datetime,)):
        return valor.isoformat()
    return _compactar(str(valor), nivel)


# --- Escritura ---------------------------------------------------------------

_tabla_lista = False
_ddl_reintento_en = 0.0
_DDL_ESPERA_SEGUNDOS = 600

DDL = f"""
CREATE TABLE IF NOT EXISTS {TABLA} (
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
CREATE INDEX IF NOT EXISTS idx_{TABLA}_usuario_fecha ON {TABLA} (usuario_email, fecha_hora DESC);
CREATE INDEX IF NOT EXISTS idx_{TABLA}_fecha         ON {TABLA} (fecha_hora DESC);
CREATE INDEX IF NOT EXISTS idx_{TABLA}_registro      ON {TABLA} (formulario, registro_id);
CREATE INDEX IF NOT EXISTS idx_{TABLA}_modulo_tipo   ON {TABLA} (modulo, tipo_evento, fecha_hora DESC);

CREATE OR REPLACE FUNCTION {TABLA}_inmutable() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION '{TABLA} es un registro de auditoría: no se permite %', TG_OP;
END;
$$ LANGUAGE plpgsql;
"""

_TRIGGERS = (
    (f'trg_{TABLA}_inmutable_fila',
     f'CREATE TRIGGER trg_{TABLA}_inmutable_fila BEFORE UPDATE OR DELETE ON {TABLA} '
     f'FOR EACH ROW EXECUTE FUNCTION {TABLA}_inmutable()'),
    (f'trg_{TABLA}_inmutable_truncate',
     f'CREATE TRIGGER trg_{TABLA}_inmutable_truncate BEFORE TRUNCATE ON {TABLA} '
     f'FOR EACH STATEMENT EXECUTE FUNCTION {TABLA}_inmutable()'),
)


def asegurar_tabla(conn):
    """Crea tabla, índices y triggers si faltan. Idempotente; una vez por proceso."""
    global _tabla_lista, _ddl_reintento_en
    if _tabla_lista or time.monotonic() < _ddl_reintento_en:
        return _tabla_lista
    try:
        cur = conn.cursor()
        cur.execute(DDL)
        for nombre, sentencia in _TRIGGERS:
            cur.execute("SELECT 1 FROM pg_trigger WHERE tgname = %s AND NOT tgisinternal", (nombre,))
            if not cur.fetchone():
                cur.execute(sentencia)
        conn.commit()
        cur.close()
        _tabla_lista = True
    except Exception as e:
        conn.rollback()
        _ddl_reintento_en = time.monotonic() + _DDL_ESPERA_SEGUNDOS
        app_logger.error(f"No se pudo asegurar la tabla {TABLA}: {e}")
    return _tabla_lista


def registrar_evento(tipo_evento, modulo, accion, usuario_email=None, usuario_nombre=None,
                     sesion_jti=None, formulario=None, registro_id=None, detalle=None,
                     estado='Exitoso', http_status=None, metodo_ruta=None, dispositivo=None,
                     user_agent=None, ip=None, origen=ORIGEN_APP, conn=None):
    """Inserta un evento. Con `conn` usa esa conexión sin hacer commit; sin ella
    abre y cierra la suya. Devuelve el id insertado o None si no se pudo."""
    propia = conn is None
    if propia:
        conn = get_db_connection()
        if not conn:
            app_logger.error("Auditoría sin conexión a la base: evento perdido "
                             f"({tipo_evento} {modulo} {accion} {usuario_email})")
            return None
    try:
        asegurar_tabla(conn)
        cur = conn.cursor()
        cur.execute(f"""
            INSERT INTO {TABLA}
                (usuario_email, usuario_nombre, licencia, sesion_jti, tipo_evento, modulo, accion,
                 formulario, registro_id, detalle, estado, http_status, metodo_ruta,
                 dispositivo, user_agent, ip, origen)
            VALUES (%s, %s, (SELECT name FROM companies ORDER BY id LIMIT 1), %s, %s, %s, %s,
                    %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s)
            RETURNING id
        """, (
            (usuario_email or None) and str(usuario_email)[:255],
            (usuario_nombre or None) and str(usuario_nombre)[:255],
            (sesion_jti or None) and str(sesion_jti)[:64],
            str(tipo_evento)[:40], str(modulo)[:60], str(accion)[:120],
            (formulario or None) and str(formulario)[:60],
            registro_id if isinstance(registro_id, int) else None,
            extras.Json(detalle) if detalle else None,
            str(estado)[:20], http_status,
            (metodo_ruta or None) and str(metodo_ruta)[:200],
            (dispositivo or None) and str(dispositivo)[:20],
            (user_agent or None) and str(user_agent)[:300],
            (ip or None) and str(ip)[:64],
            origen,
        ))
        nuevo = cur.fetchone()[0]
        cur.close()
        if propia:
            conn.commit()
        return nuevo
    except Exception as e:
        if propia:
            conn.rollback()
        app_logger.error(f"Auditoría: no se pudo registrar {tipo_evento}/{accion}: {e}")
        return None
    finally:
        if propia:
            conn.close()


# --- Consulta (pantalla Administración → Auditoría) --------------------------

FILTROS = ('usuario', 'desde', 'hasta', 'modulo', 'tipo', 'formulario', 'accion',
           'registro_id', 'estado', 'q')
COLUMNAS = ('id', 'fecha_hora', 'usuario_email', 'usuario_nombre', 'licencia', 'tipo_evento',
            'modulo', 'accion', 'formulario', 'registro_id', 'detalle', 'estado', 'http_status',
            'metodo_ruta', 'dispositivo', 'ip', 'origen')


def leer_filtros(args):
    return {k: (args.get(k) or '').strip() for k in FILTROS}


def _limite_dia(texto, zona, dias=0):
    """'YYYY-MM-DD' en la zona de la operación → instante TIMESTAMPTZ. Las fronteras
    de fecha se resuelven en la zona configurada, no en UTC (ver fechas frontera)."""
    d = datetime.strptime(texto, '%Y-%m-%d')
    return datetime(d.year, d.month, d.day, tzinfo=zona) + timedelta(days=dias)


def construir_where(filtros, zona):
    condiciones, params = [], []
    if filtros.get('usuario'):
        condiciones.append("usuario_email = %s")
        params.append(filtros['usuario'])
    if filtros.get('desde'):
        condiciones.append("fecha_hora >= %s")
        params.append(_limite_dia(filtros['desde'], zona))
    if filtros.get('hasta'):
        condiciones.append("fecha_hora < %s")
        params.append(_limite_dia(filtros['hasta'], zona, dias=1))
    for campo, columna in (('modulo', 'modulo'), ('tipo', 'tipo_evento'),
                           ('formulario', 'formulario'), ('accion', 'accion'), ('estado', 'estado')):
        if filtros.get(campo):
            condiciones.append(f"{columna} = %s")
            params.append(filtros[campo])
    if filtros.get('registro_id'):
        try:
            condiciones.append("registro_id = %s")
            params.append(int(filtros['registro_id']))
        except ValueError:
            condiciones.append("FALSE")
    if filtros.get('q'):
        patron = f"%{filtros['q']}%"
        condiciones.append("(usuario_email ILIKE %s OR usuario_nombre ILIKE %s OR accion ILIKE %s "
                           "OR metodo_ruta ILIKE %s OR detalle::text ILIKE %s)")
        params.extend([patron] * 5)
    where = ('WHERE ' + ' AND '.join(condiciones)) if condiciones else ''
    return where, params


def consultar_eventos(cur, filtros, zona, pagina=1, por_pagina=50):
    """Devuelve (total, filas) de la página pedida, de más reciente a más antiguo."""
    where, params = construir_where(filtros, zona)
    cur.execute(f"SELECT COUNT(*) FROM {TABLA} {where}", params)
    total = cur.fetchone()[0]
    offset = max(pagina - 1, 0) * por_pagina
    cur.execute(f"SELECT {', '.join(COLUMNAS)} FROM {TABLA} {where} "
                f"ORDER BY fecha_hora DESC, id DESC LIMIT %s OFFSET %s",
                params + [por_pagina, offset])
    return total, [dict(zip(COLUMNAS, fila)) for fila in cur.fetchall()]


def iterar_eventos(cur, filtros, zona, maximo=50000):
    """Recorre los eventos filtrados (para exportar), acotado a `maximo` filas."""
    where, params = construir_where(filtros, zona)
    cur.execute(f"SELECT {', '.join(COLUMNAS)} FROM {TABLA} {where} "
                f"ORDER BY fecha_hora DESC, id DESC LIMIT %s", params + [maximo])
    for fila in cur:
        yield dict(zip(COLUMNAS, fila))
