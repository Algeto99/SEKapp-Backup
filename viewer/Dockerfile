# Use Python 3.9 slim image as the base
FROM python:3.9-slim-buster

# Set the working directory inside the container
WORKDIR /app

# Install system dependencies.
# gcc is needed for building some Python packages.
# libpq-dev is for PostgreSQL client libraries, included for consistency with your other services,
# although this specific mock-data service doesn't directly use psycopg2.
RUN apt-get update && apt-get install -y \
    gcc \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# Copy the requirements.txt file into the container first.
# This allows Docker to cache this layer, so if only your Python code changes,
# dependencies won't need to be reinstalled every time.
COPY requirements.txt .

# Install Python dependencies from requirements.txt
# --no-cache-dir ensures that pip does not store downloaded packages, reducing image size.
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of your application code (including app.py and the 'templates' folder)
# into the working directory.
COPY . .

# Create a non-root user for security best practices.
# This user 'app' will own the /app directory and run the application.
RUN useradd --create-home --shell /bin/bash app && chown -R app:app /app
USER app

# Expose port 8080. Cloud Run services listen for requests on the port defined
# by the PORT environment variable, which defaults to 8080.
EXPOSE 8080

# Set environment variables.
# PYTHONUNBUFFERED=1 prevents Python from buffering stdout/stderr,
# which is good for seeing logs in real-time in container environments.
ENV PYTHONUNBUFFERED=1
ENV PORT=8080

# Command to run your application.
# We're using 'flask run' with host 0.0.0.0 to listen on all network interfaces
# and port 8080. For production, consider a WSGI server like Gunicorn,
# but 'flask run' is often sufficient for Cloud Run's managed environment.
CMD ["flask", "run", "--host", "0.0.0.0", "--port", "8080"]
