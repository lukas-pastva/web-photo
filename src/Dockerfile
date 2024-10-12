# Use the official Python 3.9 slim image as the base
FROM python:3.9-slim

# Install system dependencies, including FFmpeg
RUN apt-get update && apt-get install -y \
    libheif1 \
    libheif-dev \
    libffi-dev \
    build-essential \
    s3cmd \
    s3fs \
    vim \
    ffmpeg \ 
    && rm -rf /var/lib/apt/lists/*

# Set work directory
WORKDIR /app

# Copy requirements
COPY requirements.txt .

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .

# Expose port
EXPOSE 5000

# Run the application
CMD ["python", "app.py"]
