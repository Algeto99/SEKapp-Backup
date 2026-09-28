"""Arranque común de las pruebas de SEKapp con el cliente de Flask.

Necesita un Postgres desechable en SEKAPP_TEST_DATABASE_URL (por defecto el
contenedor local `docker run -p 54329:5432 postgres:14`). `recrear_base()`
BORRA el esquema público y lo vuelve a crear desde sql/schema.sql: nunca
apuntar estas pruebas a una base real.
"""
import logging
import os
import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]
MONOLITH = RAIZ / 'monolith'
sys.path.insert(0, str(MONOLITH))

DB_URL = os.environ.get('SEKAPP_TEST_DATABASE_URL',
                        os.environ.get('AUDITORIA_TEST_DATABASE_URL',
                                       'postgresql://sekapp:sekapp@127.0.0.1:54329/sekapp'))
os.environ['DATABASE_URL'] = DB_URL
os.environ.setdefault('FLASK_SECRET_KEY', 'clave-de-pruebas-suficientemente-larga-00')
os.environ.setdefault('JWT_SECRET_KEY', 'jwt-de-pruebas-suficientemente-largo-000000')
os.environ['SMTP_SERVER'] = '127.0.0.1'   # que cualquier correo falle rápido
os.environ['SMTP_PORT'] = '1'

import psycopg2  # noqa: E402
from psycopg2 import extras  # noqa: E402

logging.disable(logging.WARNING)

import app as A  # noqa: E402
from extensions import limiter  # noqa: E402
import auditoria  # noqa: E402
import coordinador  # noqa: E402

CLAVE = 'Clave-Segura-123'
ZONA = 'America/Bogota'


def conectar():
    c = psycopg2.connect(DB_URL)
    c.autocommit = False
    return c


def sql(consulta, params=None, uno=False):
    """Ejecuta y devuelve filas como dicts (o una sola con uno=True). Hace commit."""
    conn = conectar()
    try:
        cur = conn.cursor(cursor_factory=extras.RealDictCursor)
        cur.execute(consulta, params or [])
        filas = [dict(r) for r in cur.fetchall()] if cur.description else None
        conn.commit()
        return (filas[0] if filas else None) if uno else filas
    finally:
        conn.close()


def eventos(**filtros):
    """Filas de eventos_auditoria que cumplen las igualdades dadas, por id."""
    where = ' AND '.join(f'{k} = %s' for k in filtros) or 'TRUE'
    return sql(f"SELECT * FROM eventos_auditoria WHERE {where} ORDER BY id", list(filtros.values()))


def configurar_app():
    A.app.config.update(TESTING=True, WTF_CSRF_ENABLED=False, JWT_COOKIE_CSRF_PROTECT=False)
    limiter.enabled = False


def recrear_base():
    """Esquema público desde cero + reset de los caches por proceso de la app."""
    conn = conectar()
    try:
        cur = conn.cursor()
        cur.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
        cur.execute((RAIZ / 'sql' / 'schema.sql').read_text())
        conn.commit()
    finally:
        conn.close()
    auditoria._tabla_lista = False
    coordinador._esquema_listo = False
    coordinador._rol_disponible = None


def crear_empresa(nombre='Kanan Sentinel Pruebas'):
    return sql("INSERT INTO companies (name, slug, is_active, enabled_modules) "
               "VALUES (%s, 'pruebas', TRUE, '[]'::jsonb) RETURNING id", [nombre], uno=True)['id']


def crear_usuario(email, nombre, company_id, is_admin=False, is_super_admin=False, is_coordinador=False):
    hash_ = A.bcrypt.generate_password_hash(CLAVE).decode('utf-8')
    return sql("INSERT INTO users (name, email, password_hash, is_admin, is_super_admin, is_coordinador, "
               "is_active, company_id) VALUES (%s, %s, %s, %s, %s, %s, TRUE, %s) RETURNING id",
               [nombre, email, hash_, is_admin, is_super_admin, is_coordinador, company_id], uno=True)['id']


def login(cliente, email, clave=CLAVE, **extra):
    datos = {'username': email, 'password': clave}
    datos.update(extra)
    return cliente.post('/login', data=datos)
