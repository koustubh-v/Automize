import os
import uuid
from dotenv import load_dotenv

load_dotenv()
import subprocess
import requests
import time
import logging
import threading
import shutil
import json
import socket
import hashlib
from typing import Optional, Dict, Any

import redis
from pythonjsonlogger.json import JsonFormatter

from fastapi import FastAPI, UploadFile, File, Form, HTTPException, BackgroundTasks, Header
from fastapi.middleware.cors import CORSMiddleware

INTERNAL_SERVICE_KEY = os.getenv("INTERNAL_SERVICE_KEY")
LMS_STORE_URL = os.getenv("LMS_STORE_URL")
REDIS_URL = os.getenv("REDIS_URL")

if not INTERNAL_SERVICE_KEY:
    raise RuntimeError("INTERNAL_SERVICE_KEY must be set")

if not LMS_STORE_URL:
    raise RuntimeError("LMS_STORE_URL must be set")

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

logger = logging.getLogger("media-compressor")
logger.setLevel(LOG_LEVEL)

handler = logging.StreamHandler()

formatter = JsonFormatter(
    "%(asctime)s %(levelname)s %(name)s %(message)s"
)

handler.setFormatter(formatter)

logger.handlers = []
logger.addHandler(handler)
logger.propagate = False


def log_event(event: str, level: str = "info", **kwargs):
    data = {"event": event, **kwargs}
    if level == "error":
        logger.error(data)
    else:
        logger.info(data)


app = FastAPI()

# Add CORS Middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Allows all origins
    allow_credentials=True,
    allow_methods=["*"],  # Allows all methods
    allow_headers=["*"],  # Allows all headers
)

VIDEO_DIR = "temp_videos"
os.makedirs(VIDEO_DIR, exist_ok=True)

MAX_VIDEO_SIZE_MB = 2000
ALLOWED_VIDEO_EXTENSIONS = {".mp4", ".mov", ".mkv", ".avi", ".webm"}
REDIS_TTL = 7200

class JobManager:
    def __init__(self, redis_url: Optional[str]):
        self.use_redis = bool(redis_url)
        self.redis_client = None
        self.memory_store = {}
        self.memory_lock = threading.Lock()

        if self.use_redis:
            try:
                self.redis_client = redis.from_url(redis_url, decode_responses=True)
                self.redis_client.ping()
                log_event("redis_connected")
            except Exception as e:
                log_event("redis_connection_failed", error=str(e))
                self.use_redis = False
        
        if not self.use_redis:
            log_event("memory_mode_enabled")

    def set_job(self, video_id: str, data: Dict[str, Any]):
        if self.use_redis:
            try:
                self.redis_client.setex(
                    f"job:{video_id}",
                    REDIS_TTL,
                    json.dumps(data)
                )
            except Exception:
                pass
        else:
            with self.memory_lock:
                self.memory_store[video_id] = data

    def get_job(self, video_id: str) -> Optional[Dict[str, Any]]:
        if self.use_redis:
            try:
                raw = self.redis_client.get(f"job:{video_id}")
                return json.loads(raw) if raw else None
            except Exception:
                return None
        else:
            with self.memory_lock:
                return self.memory_store.get(video_id)

    def update_status(self, video_id: str, status: str, extra: Dict[str, Any] = None):
        job = self.get_job(video_id)
        if not job:
            return

        job["status"] = status
        if extra:
            job.update(extra)

        self.set_job(video_id, job)


job_manager = JobManager(REDIS_URL)


def cleanup_orphan_files():
    now = time.time()
    expiration = 2 * 60 * 60

    for filename in os.listdir(VIDEO_DIR):
        path = os.path.join(VIDEO_DIR, filename)
        if os.path.isfile(path):
            if now - os.path.getmtime(path) > expiration:
                try:
                    os.remove(path)
                    log_event("orphan_file_deleted", file=filename)
                except Exception:
                    pass


def calculate_file_hash(path: str) -> str:
    sha256_hash = hashlib.sha256()
    with open(path, "rb") as f:
        for byte_block in iter(lambda: f.read(4096), b""):
            sha256_hash.update(byte_block)
    return sha256_hash.hexdigest()


def compress_video_ffmpeg(input_path, output_path):
    command = [
        "ffmpeg",
        "-y",
        "-i", input_path,
        "-vf", "scale=-2:'min(720,ih)'",
        "-c:v", "libx264",
        "-crf", "28",
        "-preset", "veryfast",
        "-c:a", "aac",
        "-b:a", "128k",
        "-movflags", "+faststart",
        output_path
    ]
    subprocess.run(command, check=True)


def send_to_lms(video_id, organization_id, compressed_path):
    retries = 3
    backoff = 2

    for attempt in range(1, retries + 1):
        try:
            with open(compressed_path, "rb") as f:
                files = {"file": (f"{video_id}_compressed.mp4", f, "video/mp4")}
                data = {
                    "video_id": video_id,
                    "organization_id": organization_id
                }
                
                log_event("sending_to_lms_start", video_id=video_id, attempt=attempt)
                
                response = requests.post(
                    LMS_STORE_URL,
                    files=files,
                    data=data,
                    timeout=600
                )

            if response.status_code >= 200 and response.status_code < 300:
                log_event("callback_success", video_id=video_id)
                return True
            else:
                 log_event("callback_failed_status", video_id=video_id, status_code=response.status_code, response=response.text)

        except Exception as e:
            log_event("callback_error", level="error", video_id=video_id, error=str(e))

        if attempt < retries:
            time.sleep(backoff)
            backoff *= 2

    return False


def background_process_video(video_id, organization_id, input_path, file_hash, input_ext):
    compressed_filename = f"{file_hash}_compressed.mp4"
    output_path = os.path.join(VIDEO_DIR, compressed_filename)
    
    # We rename input_path to be hash-based if it's not already
    hashed_input_path = os.path.join(VIDEO_DIR, f"{file_hash}_raw{input_ext}")
    
    if os.path.exists(input_path) and input_path != hashed_input_path:
        # If hash collision (rare) or just renaming, overwrite
        shutil.move(input_path, hashed_input_path)
    
    # Use the hashed path from now on
    input_path = hashed_input_path

    try:
        job_manager.update_status(video_id, "processing")
        log_event("processing_started", video_id=video_id, hash=file_hash)

        # 1. Check Deduplication
        if os.path.exists(output_path):
            log_event("deduplication_hit", video_id=video_id, hash=file_hash)
            # Skip compression
        else:
            # Compress
            start = time.time()
            try:
                compress_video_ffmpeg(input_path, output_path)
            except Exception as e:
                 log_event("ffmpeg_error", level="error", video_id=video_id, error=str(e))
                 job_manager.update_status(video_id, "failed", {"error": "Compression failed"})
                 # On compression failure, we might as well keep input for retry? 
                 # Or assumption is ffmpeg will fail again. Let's keep input for manual inspection/retry.
                 return
            duration = time.time() - start
            log_event("compression_completed", video_id=video_id, duration=duration)

        # 2. Send to LMS
        job_manager.update_status(video_id, "uploading_to_lms")
        success = send_to_lms(video_id, organization_id, output_path)

        if success:
            job_manager.update_status(video_id, "completed")
            log_event("job_completed_successfully", video_id=video_id)
            
            # 3. Cleanup Cleanly (ONLY ON SUCCESS)
            if os.path.exists(input_path): 
                os.remove(input_path)
                log_event("cleanup_input_file", video_id=video_id)
            
            if os.path.exists(output_path): 
                os.remove(output_path)
                log_event("cleanup_output_file", video_id=video_id)
                
        else:
            job_manager.update_status(video_id, "failed_upload_to_lms")
            log_event("retalining_files_for_retry", video_id=video_id)
            # FILES ARE KEPT ON DISK

    except Exception as e:
        log_event("processing_unexpected_error", level="error", video_id=video_id, error=str(e))
        job_manager.update_status(video_id, "failed", {"error": str(e)})


@app.get("/health")
def health():
    return {
        "status": "ok",
        "redis_enabled": job_manager.use_redis,
        "host": socket.gethostname()
    }


@app.post("/video/receive")
async def receive_video(
    background_tasks: BackgroundTasks,
    video_id: str = Form(...),
    organization_id: str = Form(...),
    file: UploadFile = File(...),
    x_internal_service_key: str = Header(None)
):
    if x_internal_service_key != INTERNAL_SERVICE_KEY:
        raise HTTPException(status_code=401, detail="Unauthorized")

    cleanup_orphan_files()

    ext = os.path.splitext(file.filename)[1].lower()
    if ext not in ALLOWED_VIDEO_EXTENSIONS:
        raise HTTPException(status_code=400, detail=f"Unsupported format. Allowed: {ALLOWED_VIDEO_EXTENSIONS}")

    # Temporary UUID name for ingestion
    temp_id = str(uuid.uuid4())
    input_filename = f"{temp_id}_ingest{ext}"
    input_path = os.path.join(VIDEO_DIR, input_filename)

    try:
        with open(input_path, "wb") as buffer:
            shutil.copyfileobj(file.file, buffer)
    except Exception as e:
        log_event("file_save_error", level="error", error=str(e))
        raise HTTPException(status_code=500, detail="Failed to save file")

    file_size_mb = os.path.getsize(input_path) / (1024 * 1024)
    if file_size_mb > MAX_VIDEO_SIZE_MB:
        os.remove(input_path)
        raise HTTPException(status_code=400, detail=f"File too large. Limit: {MAX_VIDEO_SIZE_MB}MB")

    # Calculate Hash for Identification
    file_hash = calculate_file_hash(input_path)
    
    log_event("video_received", video_id=video_id, hash=file_hash)

    job_data = {
        "status": "queued",
        "created_at": time.time(),
        "video_id": video_id,
        "org_id": organization_id,
        "file_hash": file_hash
    }

    job_manager.set_job(video_id, job_data)

    background_tasks.add_task(
        background_process_video,
        video_id,
        organization_id,
        input_path,
        file_hash,
        ext
    )
    
    return {
        "status": "queued", 
        "video_id": video_id, 
        "message": "Video accepted for processing"
    }


@app.get("/video/status/{video_id}")
def status(video_id: str, x_internal_service_key: str = Header(None)):
    if x_internal_service_key != INTERNAL_SERVICE_KEY:
        raise HTTPException(status_code=401, detail="Unauthorized")

    job = job_manager.get_job(video_id)
    return {
        "video_id": video_id,
        "status": job["status"] if job else "not_found"
    }

