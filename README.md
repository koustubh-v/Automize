# Video Compression Microservice

A Python FastAPI microservice designed to handle video uploads, compress them using FFmpeg, and forward the results to an LMS backend. It is optimized for Google Cloud Run deployment.

## Features

- **Direct Upload**: Handles large video file uploads.
- **Compression**: Compresses videos to web-optimized MP4 (h.264 / AAC) in the background.
- **LMS Callback**: Automatically pushes the compressed video to your LMS server.
- **Auto-Cleanup**: Deletes input and output videos after successful processing to save costs.
- **Scalable**: Stateless design (with optional Redis support) suitable for serverless deployment.

## Prerequisites

- **Google Cloud Platform Account**
- **Google Cloud CLI (`gcloud`)** installed and authenticated.
- **Docker** (optional, for local testing).

## Configuration

The service uses the following environment variables:

| Variable | Description | Required |
|----------|-------------|----------|
| `INTERNAL_SERVICE_KEY` | A secret key to secure your API endpoints. | Yes |
| `LMS_STORE_URL` | The endpoint on your LMS to receive compressed videos. | Yes |
| `REDIS_URL` | Redis connection URL (e.g., `redis://host:port`). | No (Optional) |
| `LOG_LEVEL` | Logging level (e.g., `INFO`, `DEBUG`). | No (Default: INFO) |

## API Endpoints

### 1. Upload Video
**POST** `/video/receive`

Accepts a video file and queues it for processing.

**Headers:**
- `x-internal-service-key`: <YOUR_KEY>

**Form Data:**
- `file`: (File) Video file (max 2GB by default).
- `video_id`: (String) Unique ID for the video.
- `organization_id`: (String) Org ID.

**Response:**
```json
{
  "status": "queued",
  "video_id": "12345",
  "message": "Video accepted for processing"
}
```

### 2. Check Status
**GET** `/video/status/{video_id}`

**Headers:**
- `x-internal-service-key`: <YOUR_KEY>

---

## Deployment Guide: Google Cloud Run

Follow these steps to deploy this service to Google Cloud Run.

### Step 1: Initialize Google Cloud
Login to your GCP account if you haven't already:
```bash
gcloud auth login
gcloud config set project <YOUR_PROJECT_ID>
```

### Step 2: Build and Deploy
You can build and deploy directly from the source using a single command. Cloud Run will use the `Dockerfile` provided.

```bash
gcloud run deploy video-compression-service \
  --source . \
  --region us-central1 \
  --allow-unauthenticated \
  --min-instances 0 \
  --max-instances 10 \
  --memory 2Gi \
  --cpu 2 \
  --timeout 3600 \
  --set-env-vars="INTERNAL_SERVICE_KEY=your-secret-key,LMS_STORE_URL=https://your-lms.com/api/video/callback"
```

**Parameters Explained:**
- `--memory 2Gi`: Allocates 2GB RAM (Important for video processing).
- `--cpu 2`: Allocates 2 vCPUs (FFmpeg benefits from multi-core).
- `--timeout 3600`: Sets timeout to 1 hour (Uploads/Compression can take time).
- `--allow-unauthenticated`: Allows public access (protected by your `INTERNAL_SERVICE_KEY`).
- `--set-env-vars`: Sets your production environment variables.

### Step 3: Verify Deployment
Once the deployment finishes, Cloud Run will provide a Service URL (e.g., `https://video-compression-service-xyz-uc.a.run.app`).

Test it using curl:
```bash
curl -X GET https://<YOUR_URL>/health
```

## Local Development

1. **Install Dependencies**
   ```bash
   pip install -r requirements.txt
   ```
   *Note: You must have `ffmpeg` installed on your system.*

2. **Run Server**
   ```bash
   export INTERNAL_SERVICE_KEY=dev-key
   export LMS_STORE_URL=http://localhost:3000/callback
   uvicorn app:app --reload
   ```
