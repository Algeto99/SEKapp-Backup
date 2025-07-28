import os
import sys
import logging
import re
from datetime import timedelta, datetime, timezone

from flask import Flask, render_template, request, jsonify, Response, flash, session, redirect, url_for
from flask_jwt_extended import JWTManager, jwt_required, get_jwt_identity, get_jwt, unset_jwt_cookies
from flask_cors import CORS
from google.cloud import secretmanager
from google.api_core.exceptions import NotFound

import google.auth.transport.requests
import google.oauth2.id_token
import requests

import psycopg2
from psycopg2 import extras

# --- Configure Logging ---
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)
app_logger = logging.getLogger(__name__)

# --- Initialize Flask App ---
app = Flask(__name__)
is_production = os.environ.get('K_SERVICE') is not None
app_logger.info(f"Starting Dashboard Service in {'production' if is_production else 'development'} mode")

# --- Secret Manager Client ---
secret_manager_client = secretmanager.SecretManagerServiceClient()

# Helper to determine if a string is a full secret path
def is_full_secret_path(s, project_id):
    """
    Checks if a given string is a well-formed full Google Secret Manager path.
    This helps differentiate between a secret name and a full path.
    """
    if not s or not project_id:
        return False
    return s.startswith(f"projects/{project_id}/secrets/") and "/versions/" in s

# Function to get secret
def get_secret(project_id, secret_name_or_path):
    try:
        if is_full_secret_path(secret_name_or_path, project_id):
            name = secret_name_or_path
        else:
            name = f"projects/{project_id}/secrets/{secret_name_or_path}/versions/latest"
        response = secret_manager_client.access_secret_version(request={"name": name})
        app_logger.info(f"Successfully accessed secret: {secret_name_or_path}")
        return response.payload.data.decode("UTF-8")
    except NotFound:
        app_logger.error(f"Secret '{secret_name_or_path}' not found in project '{project_id}'.")
        return None
    except Exception as e:
        app_logger.error(f"Error accessing secret '{secret_name_or_path}': {e}", exc_info=True)
        return None

# Load JWT_SECRET_KEY from Secret Manager
project_id = os.environ.get('GCP_PROJECT_ID')
if not project_id:
    app_logger.error("GCP_PROJECT_ID environment variable not set.")
    project_id = "your-gcp-project-id"  # Placeholder for local testing if not set

jwt_secret = get_secret(project_id, 'jwt-secret-key')

if not jwt_secret:
    raise ValueError(f"Secret 'jwt-secret-key' not found or accessible from Secret Manager for project {project_id}. Ensure GOOGLE_APPLICATION_CREDENTIALS is set and has Secret Manager Accessor role.")

app.config['JWT_SECRET_KEY'] = jwt_secret
app.config['JWT_TOKEN_LOCATION'] = ['cookies']
app.config['JWT_COOKIE_SECURE'] = True if is_production else False
app.config['JWT_COOKIE_CSRF_PROTECT'] = False
app.config['JWT_ACCESS_TOKEN_EXPIRES'] = timedelta(hours=1)
app.config['JWT_REFRESH_TOKEN_EXPIRES'] = timedelta(days=30)
app.config['JWT_COOKIE_DOMAIN'] = os.environ.get('JWT_COOKIE_DOMAIN', '.tzolkintech.com')

jwt = JWTManager(app)
CORS(app)

# DB Config
DATABASE_URL = os.environ.get('DATABASE_URL')
if not DATABASE_URL:
    app_logger.critical("DATABASE_URL environment variable is not set. Database connection will fail.")

# --- Database Helper Functions ---
def get_db_connection():
    """Establishes and returns a database connection."""
    conn = None
    if not DATABASE_URL:
        app_logger.error("Attempted to connect to DB, but DATABASE_URL is not set.")
        return None
    try:
        app_logger.info(f"Attempting to connect to database using DSN (first 20 chars): {DATABASE_URL[:20]}...")
        conn = psycopg2.connect(DATABASE_URL)
        app_logger.info("Successfully connected to the database.")
        return conn
    except psycopg2.OperationalError as e:
        app_logger.error(f"PostgreSQL Operational Error connecting to database: {e}", exc_info=True)
        if "timeout" in str(e).lower():
            app_logger.error("Possible timeout. Check firewall, Cloud SQL Auth Proxy, or network configuration.")
        elif "no such file or directory" in str(e).lower() and "cloudsql" in str(e).lower():
             app_logger.error("Could not connect to Cloud SQL instance. Ensure 'ADD_CLOUDSQL_INSTANCES' is correctly configured in Cloud Run deployment.")
        return None
    except Exception as e:
        app_logger.error(f"General Error connecting to database: {e}", exc_info=True)
        return None

def get_incidents_by_week():
    """Get incident counts grouped by week"""
    conn = None
    cur = None
    try:
        conn = get_db_connection()
        if not conn:
            app_logger.error("Failed to get database connection in get_incidents_by_week.")
            return []

        cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
        
        query = """
            SELECT 
                DATE_TRUNC('week', fecha_incidente) as week_start,
                COUNT(*) as incident_count
            FROM reportes_incidentes 
            WHERE fecha_incidente IS NOT NULL
            GROUP BY DATE_TRUNC('week', fecha_incidente)
            ORDER BY week_start DESC
            LIMIT 12;
        """
        
        cur.execute(query)
        rows = cur.fetchall()
        
        data = []
        for row in rows:
            week_start = row['week_start']
            week_end = week_start + timedelta(days=6)
            data.append({
                'period': f"{week_start.strftime('%d/%m')} - {week_end.strftime('%d/%m/%Y')}",
                'count': row['incident_count']
            })
        
        return list(reversed(data))  # Reverse to show chronological order
        
    except Exception as e:
        app_logger.error(f"Error in get_incidents_by_week: {e}", exc_info=True)
        return []
    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()

def get_incidents_by_month():
    """Get incident counts grouped by month"""
    conn = None
    cur = None
    try:
        conn = get_db_connection()
        if not conn:
            app_logger.error("Failed to get database connection in get_incidents_by_month.")
            return []

        cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
        
        query = """
            SELECT 
                DATE_TRUNC('month', fecha_incidente) as month_start,
                COUNT(*) as incident_count
            FROM reportes_incidentes 
            WHERE fecha_incidente IS NOT NULL
            GROUP BY DATE_TRUNC('month', fecha_incidente)
            ORDER BY month_start DESC
            LIMIT 12;
        """
        
        cur.execute(query)
        rows = cur.fetchall()
        
        # Spanish month names
        month_names = {
            1: 'Enero', 2: 'Febrero', 3: 'Marzo', 4: 'Abril',
            5: 'Mayo', 6: 'Junio', 7: 'Julio', 8: 'Agosto',
            9: 'Septiembre', 10: 'Octubre', 11: 'Noviembre', 12: 'Diciembre'
        }
        
        data = []
        for row in rows:
            month_start = row['month_start']
            month_name = month_names[month_start.month]
            data.append({
                'period': f"{month_name} {month_start.year}",
                'count': row['incident_count']
            })
        
        return list(reversed(data))  # Reverse to show chronological order
        
    except Exception as e:
        app_logger.error(f"Error in get_incidents_by_month: {e}", exc_info=True)
        return []
    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()

def get_incidents_by_year():
    """Get incident counts grouped by year"""
    conn = None
    cur = None
    try:
        conn = get_db_connection()
        if not conn:
            app_logger.error("Failed to get database connection in get_incidents_by_year.")
            return []

        cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
        
        query = """
            SELECT 
                EXTRACT(YEAR FROM fecha_incidente) as year,
                COUNT(*) as incident_count
            FROM reportes_incidentes 
            WHERE fecha_incidente IS NOT NULL
            GROUP BY EXTRACT(YEAR FROM fecha_incidente)
            ORDER BY year;
        """
        
        cur.execute(query)
        rows = cur.fetchall()
        
        data = []
        for row in rows:
            data.append({
                'period': str(int(row['year'])),
                'count': row['incident_count']
            })
        
        return data
        
    except Exception as e:
        app_logger.error(f"Error in get_incidents_by_year: {e}", exc_info=True)
        return []
    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()

# --- Routes ---
@app.route('/')
@jwt_required()
def dashboard():
    user_email = get_jwt_identity()
    user_name = user_email.split('@')[0]  # Default fallback to email prefix

    conn = None
    cur = None
    try:
        conn = get_db_connection()
        if conn:
            cur = conn.cursor()
            app_logger.info(f"Attempting to fetch user name for email: {user_email}")
            cur.execute('SELECT "name" FROM "users" WHERE email = %s', (user_email,))
            user_row = cur.fetchone()
            if user_row and user_row[0]:
                user_name = user_row[0]
                app_logger.info(f"User found in DB: {user_name}")
            else:
                app_logger.warning(f"User {user_email} not found in 'users' table or 'name' field is empty. Displaying email prefix as name: {user_name}")
        else:
            app_logger.error("No database connection available for fetching user name.")

    except psycopg2.Error as e:
        app_logger.error(f"PostgreSQL Error getting user name: {e}", exc_info=True)
    except Exception as e:
        app_logger.error(f"An unexpected error occurred getting user name: {e}", exc_info=True)
    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()
            app_logger.info("Database connection closed after fetching user name.")

    app_logger.info(f"Rendering dashboard.html with user_name: {user_name}")
    return render_template("dashboard.html", current_user=user_email, user_name=user_name)

@app.route('/dashboard')
@jwt_required()
def dashboard_redirect():
    """Redirect /dashboard to root for compatibility"""
    return redirect('/')

@app.route('/api/incidents/weekly')
@jwt_required()
def api_incidents_weekly():
    """API endpoint for weekly incident data"""
    data = get_incidents_by_week()
    return jsonify(data)

@app.route('/api/incidents/monthly')
@jwt_required()
def api_incidents_monthly():
    """API endpoint for monthly incident data"""
    data = get_incidents_by_month()
    return jsonify(data)

@app.route('/api/incidents/yearly')
@jwt_required()
def api_incidents_yearly():
    """API endpoint for yearly incident data"""
    data = get_incidents_by_year()
    return jsonify(data)

@app.route('/logout')
def logout():
    try:
        app_logger.info("User logout requested")
        response = redirect('https://secapp.tzolkintech.com')
        unset_jwt_cookies(response)
        app_logger.info("JWT cookies cleared, redirecting to login service")
        return response
    except Exception as e:
        app_logger.error(f"Error during logout: {e}", exc_info=True)
        return redirect('https://secapp.tzolkintech.com')

if __name__ == "__main__":
    port = int(os.environ.get('PORT', 8080))
    app_logger.info(f"Starting Flask app on port {port}")
    app.run(debug=True, host='0.0.0.0', port=port)