import os
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from flask import Flask, render_template, request, redirect, url_for, flash, make_response
from flask_bcrypt import Bcrypt
from flask_jwt_extended import (
    create_access_token, create_refresh_token,
    unset_jwt_cookies, set_access_cookies, set_refresh_cookies,
    jwt_required, get_jwt_identity, JWTManager
)
import psycopg2
import psycopg2.extras
from datetime import timedelta, datetime
from google.cloud import secretmanager
import traceback

# --- App Setup ---
app = Flask(__name__)
app.config.update({
    'SECRET_KEY': os.environ.get('FLASK_SECRET_KEY', 'dev-secret'),
    'JWT_SECRET_KEY': os.environ.get('JWT_SECRET_KEY', 'dev-jwt'),
    'JWT_ACCESS_TOKEN_EXPIRES': timedelta(hours=1),
    'JWT_REFRESH_TOKEN_EXPIRES': timedelta(days=30),
    'JWT_TOKEN_LOCATION': ['cookies'],
    'JWT_COOKIE_SECURE': True,
    'JWT_COOKIE_SAMESITE': 'None',
    'JWT_COOKIE_DOMAIN': os.environ.get('JWT_COOKIE_DOMAIN', '.run.app'),
})

bcrypt = Bcrypt(app)
jwt = JWTManager(app)

app.config['SMTP_SERVER'] = os.environ.get('SMTP_SERVER', 'mail.tzolkintech.com')
app.config['SMTP_PORT'] = int(os.environ.get('SMTP_PORT', 587))
app.config['EMAIL_USERNAME'] = os.environ.get('EMAIL_USERNAME', 'no-reply@tzolkintech.com')
app.config['ADMIN_EMAIL'] = os.environ.get('ADMIN_EMAIL', 'rcanton@tzolkintech.com')
app.config['PROJECT_ID'] = os.environ.get('PROJECT_ID', 'tz-dev-secapp')
app.config['SECRET_NAME'] = os.environ.get('SECRET_NAME', 'admin-email-pass')

# --- Secret Manager Utilities ---
def get_secret_value(secret_name, project_id=None):
    try:
        if not project_id:
            project_id = app.config.get('PROJECT_ID')
        client = secretmanager.SecretManagerServiceClient()
        name = f"projects/{project_id}/secrets/{secret_name}/versions/latest"
        response = client.access_secret_version(request={"name": name})
        return response.payload.data.decode("UTF-8")
    except Exception as e:
        app.logger.error(f"Error retrieving secret {secret_name}: {e}")
        return None

def get_email_password():
    pw = os.environ.get('EMAIL_PASSWORD')
    if pw:
        return pw
    return get_secret_value(app.config['SECRET_NAME'])

# --- Email Utility ---
def send_email(to_email, subject, body, is_html=False):
    try:
        email_username = app.config['EMAIL_USERNAME']
        email_password = get_email_password()
        server = smtplib.SMTP(app.config['SMTP_SERVER'], app.config['SMTP_PORT'])
        server.starttls()
        server.login(email_username, email_password)

        msg = MIMEMultipart()
        msg['From'] = email_username
        msg['To'] = to_email
        msg['Subject'] = subject
        msg.attach(MIMEText(body, 'html' if is_html else 'plain'))
        server.sendmail(email_username, to_email, msg.as_string())
        server.quit()
        return True
    except Exception:
        app.logger.error(f"Error sending email to {to_email}", exc_info=True)
        return False

# --- DB Utility ---
def get_db_connection():
    try:
        return psycopg2.connect(os.environ['DATABASE_URL'])
    except Exception as e:
        app.logger.error(f"DB connection error: {e}")
        return None

# --- Routes (login, logout, etc.) ---
@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        email = request.form.get('username')
        password = request.form.get('password')
        conn = get_db_connection()
        if not conn:
            flash('DB error', 'danger')
            return render_template('login.html', username=email)
        try:
            cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
            cur.execute("SELECT password_hash FROM users WHERE email = %s", (email,))
            u = cur.fetchone()
            cur.close()
            conn.close()
            if u and bcrypt.check_password_hash(u['password_hash'], password):
                access = create_access_token(identity=email)
                refresh = create_refresh_token(identity=email)
                resp = make_response(redirect(os.environ.get('LANDING_SERVICE_URL', '/')))
                set_access_cookies(resp, access)
                set_refresh_cookies(resp, refresh)
                return resp
            flash('Invalid credentials', 'danger')
        except:
            app.logger.error("Login failed", exc_info=True)
            flash('Login error', 'danger')
    return render_template('login.html')

@app.route('/register', methods=['GET', 'POST'])
def register():
    # (Your full registration logic unchanged, with notifications, DB insert, etc.)
    pass

@app.route('/logout')
def logout():
    resp = make_response(redirect(url_for('login')))
    unset_jwt_cookies(resp)
    flash('Logged out', 'info')
    return resp

@app.route('/health')
def health():
    status = {'status': 'healthy', 'timestamp': datetime.utcnow().isoformat()}
    try:
        conn = get_db_connection()
        status['db'] = 'connected' if conn else 'disconnected'
    except:
        status['db'] = 'error'
    return status, 200

@app.route('/startup')
def startup():
    return {'status': 'ready', 'service': 'login-service', 'timestamp': datetime.utcnow().isoformat()}, 200

# --- Main ---
if __name__ == '__main__':
    for var in ('FLASK_SECRET_KEY', 'JWT_SECRET_KEY', 'DATABASE_URL', 'JWT_COOKIE_DOMAIN', 'LANDING_SERVICE_URL'):
        os.environ.setdefault(var, os.getenv(var, ''))
    app.run(host='0.0.0.0', port=int(os.getenv('PORT', 8080)), threaded=True)
