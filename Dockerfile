# Use an official Python 3.10 runtime as a parent image
FROM python:3.11-slim

# Set the working directory in the container to /app
WORKDIR /app

# Install system dependencies.
# - git: needed to install tvdatafeed from GitHub
# - build-essential & libta-lib-dev: needed to compile the TA-Lib python wrapper
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libta-lib-dev \
    git \
    && rm -rf /var/lib/apt/lists/*

# Copy the requirements file into the container at /app
COPY requirements.txt .

# Install the python packages specified in requirements.txt
# The --no-cache-dir flag keeps the image size smaller
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of your application's code from your local machine to /app in the container
COPY . .

# Tell Docker that the container will listen on port 8000 at runtime
EXPOSE 8000

# Define the command to run your application using Gunicorn (a production-grade server)
# This will run 4 worker processes to handle incoming requests.
# It points to the 'app' object inside your 'handler.py' file.
CMD ["gunicorn", "--workers", "4", "--bind", "0.0.0.0:8000", "handler:app"]