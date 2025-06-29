# Use the official Python image as a base
FROM python:3.9-slim-buster

# Set the working directory in the container
WORKDIR /app

# Copy the requirements file into the container
COPY requirements.txt .

# Install any needed packages specified in requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of the application code
COPY . .

# Expose the port that the Flask app will listen on
EXPOSE 8080

# Run the Flask application using Gunicorn for production
# Gunicorn is recommended for production deployments of Flask apps
CMD ["gunicorn", "--bind", "0.0.0.0:8080", "main:app"]