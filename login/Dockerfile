# Use an official Python runtime as a parent image
# python:3.9-slim-buster is a good choice for smaller image size.
# Choose a Python version that matches your development and deployment environment.
FROM python:3.11-slim-bookworm

# Set the working directory in the container
WORKDIR /app

# Install system dependencies if any (e.g., for psycopg2-binary)
# This step is crucial for libraries with C extensions.
# For psycopg2-binary, you might need libpq-dev and gcc.
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        gcc \
        libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# Copy the requirements file into the working directory
COPY requirements.txt .

# Install any needed packages specified in requirements.txt
# Use --no-cache-dir for smaller image size.
RUN pip install --no-cache-dir -r requirements.txt

# Copy the entire application code into the working directory
# This should be done after installing dependencies to leverage Docker's layer caching.
COPY . .

# Set environment variables for Flask (replace with your actual values or manage with secrets)
# These should ideally be managed via Kubernetes secrets or Cloud Run environment variables,
# but can be set here for local development/testing or as defaults.
# ENV FLASK_APP=main.py
# ENV FLASK_DEBUG=0 # Set to 0 for production

# Expose the port your Flask app will run on
# This should match the port your Gunicorn server listens on.
EXPOSE 8080

# Run the application using Gunicorn
# Using gunicorn is highly recommended for production deployments of Flask applications.
# Adjust the number of workers based on your instance's CPU cores (2*CPU + 1 is a common heuristic).
# Bind to 0.0.0.0 to listen on all network interfaces.
# Use the PORT environment variable provided by Cloud Run (or similar platforms).
# The --timeout parameter prevents requests from hanging indefinitely.
# The --access-logfile and --error-logfile can be set to '-' to output to stdout/stderr for container logging.
CMD ["gunicorn", "--bind", "0.0.0.0:8080", "--workers", "1", "--threads", "8", "--timeout", "0", "main:app"]

# Alternative CMD for development (not recommended for production)
# CMD ["python", "main.py"]