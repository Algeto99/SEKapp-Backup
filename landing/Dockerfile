```dockerfile
# landing_page_service/Dockerfile
# Use the official Python image as a base
FROM python:3.9-slim-buster

# Set the working directory in the container
WORKDIR /app

# Install any needed packages specified in requirements.txt (if any, though none for this simple service)
# COPY requirements.txt .
# RUN pip install --no-cache-dir -r requirements.txt

# Copy the application code into the container
COPY . .

# Expose the port the app will run on
EXPOSE 8080

# Run the application using Gunicorn, a production-ready WSGI HTTP server.
# The --bind 0.0.0.0:8080 tells Gunicorn to listen on all network interfaces on port 8080.
# The --workers 2 sets the number of worker processes (adjust based on your needs).
# The app:app refers to the 'app' Flask instance within the 'app.py' file.
CMD ["gunicorn", "--bind", "0.0.0.0:8080", "app:app"]

