#!/bin/bash

# Startup script for Cloud Run
echo "Starting SMT SecApp Login Service..."

# Print environment info
echo "PORT: $PORT"
echo "DATABASE_URL: ${DATABASE_URL:0:20}..." # Only show first 20 chars for security
echo "EMAIL_USERNAME: $EMAIL_USERNAME"
echo "LANDING_SERVICE_URL: $LANDING_SERVICE_URL"
echo "LOGIN_SERVICE_URL: $LOGIN_SERVICE_URL"

# Test database connection
echo "Testing database connection..."
python -c "
import os
import psycopg2
try:
    conn = psycopg2.connect(os.environ.get('DATABASE_URL'))
    print('Database connection: SUCCESS')
    conn.close()
except Exception as e:
    print(f'Database connection: FAILED - {e}')
"

# Start the application
echo "Starting Flask application..."
exec python main.py