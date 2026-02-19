FROM python:3.9-slim

# Install ffmpeg
RUN apt-get update && \
    apt-get install -y ffmpeg && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .

RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Create temp directory
RUN mkdir -p temp_videos

# Expose port
EXPOSE 8080

# Command to run the application (Cloud Run requires listening on PORT env var, typically 8080)
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8080"]
