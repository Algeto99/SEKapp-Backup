"""Resolución del bucket de subidas según el proyecto GCP donde corre la instancia.

Cada cliente se despliega en su propio proyecto con su propio bucket. El bucket
estaba hardcodeado como 'smt-uploads', así que los despliegues de otros clientes
intentaban escribir en el bucket del cliente original y recibían 403.

Convención vigente: un proyecto `tz-<entorno>-<cliente>` usa el bucket
`tz-<cliente>-uploads` (tz-prod-sesursa → tz-sesursa-uploads). El despliegue
original es la única excepción histórica: tz-dev-secapp usa 'smt-uploads'.

Orden de resolución:

  1. GCS_BUCKET_NAME — override explícito por despliegue; siempre gana.
  2. Excepción legada — el proyecto original conserva su bucket histórico.
  3. Convención — derivada del nombre del proyecto. No requiere permisos extra,
     que es lo que la hace viable: las service accounts de los clientes suelen
     tener sólo roles/storage.objectViewer, que no incluye storage.buckets.list.
  4. Descubrimiento — para proyectos que no siguen la convención: el único
     bucket del proyecto activo cuyo nombre termina en 'uploads'.

Si nada resuelve, devuelve None y las subidas quedan deshabilitadas con un error
explícito. Deliberadamente NO cae a 'smt-uploads' fuera de su propio proyecto:
ese fallback es el bug que originó los 403 y, con permisos más amplios, habría
escrito evidencia de un cliente en el bucket de otro.

Nunca lanza excepción: se resuelve al importar y un fallo dejaría la app sin
arrancar.
"""
import logging
import os
import re
import threading

app_logger = logging.getLogger(__name__)

# Excepción histórica: el despliegue original precede a la convención de nombres.
LEGACY_BUCKET = 'smt-uploads'
LEGACY_PROJECT = 'tz-dev-secapp'

# tz-<entorno>-<cliente>  →  tz-<cliente>-uploads
_PROJECT_RE = re.compile(r'^tz-[a-z0-9]+-(?P<company>[a-z0-9][a-z0-9-]*)$')
_BUCKET_TEMPLATE = 'tz-{company}-uploads'

_BUCKET_SUFFIX = 'uploads'
_ENV_VAR = 'GCS_BUCKET_NAME'

# Centinela: None es un resultado válido (bucket indeterminado) y debe cachearse
# igual, para no repetir la resolución en cada subida.
_UNSET = object()
_resolved = _UNSET
_lock = threading.Lock()


def detect_project():
    """Proyecto GCP activo: primero el entorno, luego las credenciales por defecto
    (que en Cloud Run consultan el metadata server)."""
    # GCP_PROJECT_ID es el que setea scripts/deploy_env.sh; los otros son los
    # nombres estándar que usan las librerías de Google.
    for var in ('GCP_PROJECT_ID', 'GOOGLE_CLOUD_PROJECT', 'GCLOUD_PROJECT', 'GCP_PROJECT'):
        value = os.environ.get(var)
        if value:
            return value
    try:
        import google.auth
        _, project = google.auth.default()
        return project
    except Exception as e:
        app_logger.warning(f"No se pudo detectar el proyecto GCP: {e}")
        return None


def bucket_from_project(project):
    """Aplica la convención de nombres. None si el proyecto no la sigue."""
    match = _PROJECT_RE.match(project or '')
    if not match:
        return None
    return _BUCKET_TEMPLATE.format(company=match.group('company'))


def _discover_bucket(client, project):
    """Busca en el proyecto activo el bucket de subidas.

    Exige coincidencia única: si hay varios candidatos el nombre correcto es
    ambiguo, y elegir uno al azar mandaría evidencia al bucket equivocado.
    """
    try:
        names = sorted(
            b.name for b in client.list_buckets()
            if b.name.endswith(_BUCKET_SUFFIX)
        )
    except Exception as e:
        app_logger.warning(
            f"No se pudo listar buckets del proyecto {project} ({e}). "
            f"Definí {_ENV_VAR} en el servicio para fijar el bucket explícitamente."
        )
        return None

    if len(names) == 1:
        return names[0]
    if not names:
        app_logger.warning(f"Ningún bucket termina en '{_BUCKET_SUFFIX}' en el proyecto {project}.")
    else:
        app_logger.warning(
            f"Varios buckets candidatos en {project}: {names}. "
            f"Definí {_ENV_VAR} para desambiguar."
        )
    return None


def _resolve(client):
    override = os.environ.get(_ENV_VAR)
    if override:
        app_logger.info(f"Bucket de subidas por {_ENV_VAR}: {override}")
        return override

    project = detect_project()

    if project == LEGACY_PROJECT:
        app_logger.info(f"Bucket de subidas del despliegue original: {LEGACY_BUCKET}")
        return LEGACY_BUCKET

    if project:
        conventional = bucket_from_project(project)
        if conventional:
            app_logger.info(f"Bucket de subidas por convención en {project}: {conventional}")
            return conventional

        if client is None:
            try:
                from google.cloud import storage
                client = storage.Client()
            except Exception as e:
                app_logger.warning(f"No se pudo crear el cliente GCS para descubrir el bucket: {e}")

        if client is not None:
            discovered = _discover_bucket(client, project)
            if discovered:
                app_logger.info(f"Bucket de subidas descubierto en {project}: {discovered}")
                return discovered

    if project is None:
        app_logger.warning(
            f"Proyecto GCP indeterminado; usando el bucket por defecto: {LEGACY_BUCKET}"
        )
        return LEGACY_BUCKET

    app_logger.error(
        f"No se pudo determinar el bucket de subidas del proyecto {project}. "
        f"Las subidas quedarán deshabilitadas hasta que se defina {_ENV_VAR} en el "
        f"servicio. No se usa {LEGACY_BUCKET} porque pertenece a otro cliente."
    )
    return None


def resolve_upload_bucket(client=None):
    """Nombre del bucket de subidas de este despliegue. Se resuelve una sola vez
    por proceso; `client` permite reutilizar un cliente GCS ya inicializado."""
    global _resolved
    if _resolved is not _UNSET:
        return _resolved
    with _lock:
        if _resolved is _UNSET:
            try:
                _resolved = _resolve(client)
            except Exception as e:
                app_logger.error(f"Error resolviendo el bucket de subidas: {e}", exc_info=True)
                _resolved = None
    return _resolved
