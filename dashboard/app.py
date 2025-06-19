# dashboard/app.py
import os
from flask import Flask, render_template
import psycopg2

app = Flask(__name__)

# Database connection details from environment variables
DB_HOST = os.environ.get('DB_HOST')
DB_NAME = os.environ.get('DB_NAME')
DB_USER = os.environ.get('DB_USER')
DB_PASSWORD = os.environ.get('DB_PASSWORD')
DB_PORT = os.environ.get('DB_PORT', '5432') # Default PostgreSQL port

def get_db_connection():
    """
    Establishes and returns a connection to the PostgreSQL database.
    """
    conn = None
    try:
        conn = psycopg2.connect(
            host=DB_HOST,
            database=DB_NAME,
            user=DB_USER,
            password=DB_PASSWORD,
            port=DB_PORT
        )
        print("Database connection successful.")
        return conn
    except Exception as e:
        print(f"Error connecting to database: {e}")
        return None

@app.route('/')
def index():
    return redirect(url_for('show_dashboard'))


@app.route('/dashboard', methods=['GET'])
def show_dashboard():
    """
    Renders the dashboard page, fetching all submitted data from the database.
    """
    conn = None
    submissions = []
    try:
        conn = get_db_connection()
        if conn is None:
            return "Failed to connect to database for dashboard.", 500

        cur = conn.cursor()
        # Fetch all submissions, ordered by submission time
        cur.execute("SELECT id, name, email, message, submission_time FROM form_submissions ORDER BY submission_time DESC;")
        submissions = cur.fetchall()
        cur.close()
    except psycopg2.Error as e:
        print(f"Database error fetching data: {e}")
        return f"An error occurred while fetching data: {e}", 500
    except Exception as e:
        print(f"An unexpected error occurred: {e}")
        return f"An unexpected error occurred: {e}", 500
    finally:
        if conn:
            conn.close()

    return render_template('dashboard.html', submissions=submissions)

if __name__ == '__main__':
    # This is for local development only.
    # Cloud Run will run the app using Gunicorn or similar.
    app.run(host='0.0.0.0', port=8080, debug=True)

