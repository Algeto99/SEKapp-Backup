# dashboard/Dockerfile
FROM python:3.9-slim-buster

WORKDIR /app

# Install system packages required for psycopg2 (non-binary)
RUN apt-get update && apt-get install -y \
    gcc \
    libpq-dev \
    build-essential \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy app source code
COPY . .

# Expose the port Cloud Run expects
EXPOSE 8080

# Launch the app with Gunicorn
CMD ["gunicorn", "--bind", "0.0.0.0:8080", "app:app"]
