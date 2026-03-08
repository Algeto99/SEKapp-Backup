# dashboard/Dockerfile

# Use a slim Python base image for smaller deployments
FROM python:3.9-slim-buster

# Set the working directory inside the container
WORKDIR /app

# Install Python dependencies
# Copy only the requirements file first to leverage Docker layer caching.
# This ensures that if only your Python code changes (but not dependencies),
# this layer won't be rebuilt.
COPY requirements.txt .

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of your application code (including app.py and the 'templates' folder)
# into the working directory. This assumes all necessary files are in the build context.
COPY . .

# Expose the port Cloud Run expects
EXPOSE 8080

# Launch the app with Gunicorn
# 'app:app' refers to the 'app' Flask instance within the 'app.py' file.
CMD ["gunicorn", "--bind", "0.0.0.0:8080", "--workers", "1", "--threads", "8", "--timeout", "0", "app:app"]
