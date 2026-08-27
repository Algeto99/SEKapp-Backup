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


# ─── URL Signing ─────────────────────────────────────────────────────────────

from datetime import timedelta
from google.cloud import storage
import google.auth
from google.auth.transport import requests as google_auth_requests
from google.oauth2 import service_account

_signing_credentials = None
_storage_client = None


def _get_storage_client():
    global _storage_client
    if _storage_client is None:
        try:
            _storage_client = storage.Client()
        except Exception as e:
            app_logger.warning(f"Could not initialize storage.Client(): {e}")
            return None
    return _storage_client


def _signing_kwargs():
    """Extra arguments that let generate_signed_url actually sign on Cloud Run via IAM SignBlob API."""
    global _signing_credentials

    if _signing_credentials is None:
        try:
            _signing_credentials, _ = google.auth.default()
        except Exception as e:
            app_logger.warning(f"Could not load default google auth credentials: {e}")
            return {}

    if isinstance(_signing_credentials, service_account.Credentials):
        return {}

    try:
        if hasattr(_signing_credentials, 'valid') and not _signing_credentials.valid:
            _signing_credentials.refresh(google_auth_requests.Request())
    except Exception as e:
        app_logger.warning(f"Could not refresh credentials for signing: {e}")

    email = getattr(_signing_credentials, 'service_account_email', None)
    if not email or email == 'default' or not getattr(_signing_credentials, 'token', None):
        return {}

    return {
        'service_account_email': email,
        'access_token': _signing_credentials.token,
    }


def generate_signed_url(gcs_url, expiration_minutes=120):
    """Generates a v4 signed URL for a GCS blob (default 2 hours expiration)."""
    try:
        if not gcs_url or not isinstance(gcs_url, str):
            return gcs_url

        clean_url = gcs_url.strip()
        if 'storage.googleapis.com' not in clean_url and not clean_url.startswith('gs://'):
            return clean_url

        # Remove existing query parameters if any
        clean_url = clean_url.split('?')[0]

        if clean_url.startswith('gs://'):
            parts = clean_url[5:].split('/', 1)
        elif 'storage.googleapis.com/' in clean_url:
            parts = clean_url.split('storage.googleapis.com/', 1)[1].split('/', 1)
        else:
            return gcs_url

        if len(parts) != 2:
            return gcs_url

        bucket_name, blob_name = parts
        if not bucket_name or not blob_name:
            return gcs_url

        client = _get_storage_client()
        if not client:
            return gcs_url

        bucket = client.bucket(bucket_name)
        blob = bucket.blob(blob_name)

        url = blob.generate_signed_url(
            version="v4",
            expiration=timedelta(minutes=expiration_minutes),
            method="GET",
            **_signing_kwargs()
        )
        return url
    except Exception as e:
        app_logger.error(f"Error generating signed URL for {gcs_url}: {e}", exc_info=True)
        return gcs_url


import base64
import hashlib
import hmac


def _get_secret_key(secret_key=None):
    if secret_key:
        return secret_key
    try:
        from flask import current_app
        if current_app and current_app.config.get('SECRET_KEY'):
            return current_app.config['SECRET_KEY']
    except Exception:
        pass
    return os.environ.get('SECRET_KEY', 'sekapp-media-secret')


def build_media_token(gcs_url, secret_key=None):
    """Encodes a GCS URL into a signed token for public streaming."""
    if not gcs_url:
        return ''
    clean = gcs_url.split('?')[0]
    secret = _get_secret_key(secret_key).encode()
    encoded = base64.urlsafe_b64encode(clean.encode()).decode().rstrip('=')
    sig = hmac.new(secret, clean.encode(), hashlib.sha256).hexdigest()[:24]
    return f"{encoded}.{sig}"


def verify_media_token(token, secret_key=None):
    """Verifies a media token and returns the clean GCS URL, or None if invalid."""
    try:
        if not token or '.' not in token:
            return None
        parts = token.rsplit('.', 1)
        if len(parts) != 2:
            return None
        encoded, sig = parts
        padded = encoded + '=' * (4 - len(encoded) % 4)
        gcs_url = base64.urlsafe_b64decode(padded).decode()
        secret = _get_secret_key(secret_key).encode()
        expected = hmac.new(secret, gcs_url.encode(), hashlib.sha256).hexdigest()[:24]
        if not hmac.compare_digest(sig, expected):
            return None
        return gcs_url
    except Exception:
        return None


def get_public_media_url(gcs_url, expiration_minutes=120, secret_key=None):
    """
    Returns a publicly accessible URL for a GCS image/document.
    First tries to generate a GCS v4 signed URL.
    If signed URL cannot be generated (e.g. missing IAM signing permissions or local dev),
    it generates a secure signed proxy URL pointing to /expediente/media/<token>.
    """
    if not gcs_url or not isinstance(gcs_url, str):
        return gcs_url

    clean_url = gcs_url.strip()
    if 'storage.googleapis.com' not in clean_url and not clean_url.startswith('gs://'):
        return clean_url

    # Try standard v4 signed URL first
    signed = generate_signed_url(clean_url, expiration_minutes=expiration_minutes)
    if signed and signed != clean_url and ('X-Goog-Signature' in signed or 'Signature=' in signed):
        return signed

    # Fallback to HMAC-signed proxy URL
    token = build_media_token(clean_url, secret_key=secret_key)
    return f"/expediente/media/{token}"


