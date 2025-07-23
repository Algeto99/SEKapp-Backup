# Use Python 3.9 slim image with Bullseye as the base
FROM python:3.9-slim-bullseye

# Set the working directory inside the container
WORKDIR /app

# Install system dependencies.
# gcc is needed for building some Python packages.
# libpq-dev is for PostgreSQL client libraries.
RUN apt-get update && apt-get install -y \
    gcc \
    libpq-dev \
    # Added for Gunicorn in some environments, though might not be strictly needed for slim
    # libffi-dev \
    # libssl-dev \
    && rm -rf /var/lib/apt/lists/*

# Copy the requirements.txt file into the container first.
COPY requirements.txt .

# Install Python dependencies from requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of your application code (including app.py and the 'templates' folder)
COPY . .

# Create a non-root user for security best practices.
RUN useradd --create-home --shell /bin/bash app && chown -R app:app /app
USER app

# Expose port 8080.
EXPOSE 8080

# Set environment variables.
ENV PYTHONUNBUFFERED=1
# Cloud Run automatically sets the PORT env variable; your app should listen on it.
# We'll rely on Gunicorn to pick it up.
# ENV PORT=8080 # This line is often not strictly needed, Gunicorn will use $PORT

# Command to run your application using Gunicorn.
# 'app:app' refers to the 'app' Flask instance within the 'app.py' file.
# The --timeout 0 flag is often useful for Cloud Run to prevent Gunicorn from
# prematurely killing connections if initial processing is slow.
CMD gunicorn --bind 0.0.0.0:${PORT} --timeout 0 app:app