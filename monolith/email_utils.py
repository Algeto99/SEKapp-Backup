import logging
import smtplib
import ssl
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from flask import current_app, url_for

logger = logging.getLogger(__name__)


def get_email_password():
    """Return the SMTP password from env or GCP Secret Manager."""
    password = (
        current_app.config.get('EMAIL_PASSWORD')
        or current_app.config.get('EMAIL_PASSWORD_SECRET')
    )
    if not password:
        password = (
            __import__('os').environ.get('EMAIL_PASSWORD')
            or __import__('os').environ.get('EMAIL_PASSWORD_SECRET')
        )
    if password:
        return password

    project_id = current_app.config.get('GCP_PROJECT_ID')
    secret_name = current_app.config.get('EMAIL_PASSWORD_SECRET_NAME')
    if not project_id or not secret_name:
        return None
    try:
        from google.cloud import secretmanager
        client = secretmanager.SecretManagerServiceClient()
        name = f"projects/{project_id}/secrets/{secret_name}/versions/latest"
        response = client.access_secret_version(request={"name": name})
        return response.payload.data.decode("UTF-8")
    except Exception as e:
        logger.warning(f"Could not retrieve email password from Secret Manager: {e}")
        return None


def send_email(to_emails, subject, body, is_html=False, cc_emails=None):
    """Send an email via SMTP.

    to_emails  – a single address string or a list of strings.
    cc_emails  – optional single address string or list of strings.
    Returns True on success, False on failure.
    """
    email_username = (
        current_app.config.get('SENDER_EMAIL')
        or current_app.config.get('EMAIL_USERNAME')
    )
    smtp_server = current_app.config.get('SMTP_SERVER')
    smtp_port = current_app.config.get('SMTP_PORT')
    email_password = get_email_password()

    if not all([email_username, email_password, smtp_server, smtp_port]):
        logger.warning("Email not fully configured — skipping send_email")
        return False

    recipients = [to_emails] if isinstance(to_emails, str) else list(to_emails)

    msg = MIMEMultipart()
    msg['From'] = email_username
    msg['To'] = ', '.join(recipients)
    msg['Subject'] = subject
    if cc_emails:
        cc_list = [cc_emails] if isinstance(cc_emails, str) else list(cc_emails)
        msg['Cc'] = ', '.join(cc_list)
        recipients = recipients + cc_list
    msg.attach(MIMEText(body, 'html' if is_html else 'plain'))

    try:
        context = ssl.create_default_context()
        with smtplib.SMTP(smtp_server, smtp_port, timeout=10) as server:
            server.ehlo()
            server.starttls(context=context)
            server.ehlo()
            server.login(email_username, email_password)
            server.send_message(msg, to_addrs=recipients)
        return True
    except Exception as e:
        logger.error(f"Email send failure to {recipients}: {e}", exc_info=True)
        return False


def send_password_reset_email(email, reset_token):
    reset_url = url_for('login_bp.reset_password', token=reset_token, _external=True)
    subject = "Restablecer Contraseña - Kanan SekApp"
    html_body = (
        f'<div style="font-family:sans-serif;">'
        f'<p>Haz clic en el siguiente enlace para restablecer tu contraseña:</p>'
        f'<p><a href="{reset_url}">Restablecer contraseña</a></p>'
        f'<p>Este enlace expira en 1 hora.</p>'
        f'</div>'
    )
    return send_email(email, subject, html_body, is_html=True)


def send_welcome_email(user_email, user_name, is_admin=False):
    subject = "¡Bienvenido a Kanan SekApp!"
    login_url = url_for('login_bp.login', _external=True)
    html_body = (
        f'<div style="font-family:sans-serif;">'
        f'<p>¡Hola {user_name}!</p>'
        f'<p>Tu cuenta ha sido creada exitosamente.</p>'
        f'<p><a href="{login_url}">Iniciar sesión</a></p>'
        f'</div>'
    )
    return send_email(user_email, subject, html_body, is_html=True)


def send_registration_notification(user_email, user_name, phone_number):
    admin_email = current_app.config.get('ADMIN_EMAIL')
    if admin_email:
        subject = f"Nuevo Usuario Registrado - {user_name}"
        html_body = (
            f'<div style="font-family:sans-serif;">'
            f'<p>Nuevo registro:</p>'
            f'<ul><li>Nombre: {user_name}</li>'
            f'<li>Correo: {user_email}</li>'
            f'<li>Teléfono: {phone_number or "N/A"}</li></ul>'
            f'</div>'
        )
        send_email(admin_email, subject, html_body, is_html=True)
    return send_welcome_email(user_email, user_name)
