FROM python:3.9-slim

# Install system dependencies, including FFmpeg and dependencies for Pillow and pyheif
RUN apt-get update && apt-get install -y \
    libheif1 \
    libheif-dev \
    libffi-dev \
    build-essential \
    s3cmd \
    s3fs \
    vim \
    ffmpeg \
    libjpeg-dev \
    zlib1g-dev \
    libpng-dev \
    libtiff-dev \
    libopenjp2-7-dev \
    libwebp-dev \
    && rm -rf /var/lib/apt/lists/*  # Clean up APT cache to reduce image size

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
