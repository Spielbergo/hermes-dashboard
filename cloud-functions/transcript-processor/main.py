"""
Transcript Processor â€” GCP Cloud Function
==========================================
Runs on a Cloud Scheduler cron (e.g. every 5 minutes).
1. Polls a Google Drive folder for new .txt transcription files.
2. Sends each new transcript to the Hermes webhook for AI analysis.
3. Records processed file IDs in a GCS state bucket to avoid duplicates.

Required environment variables (set in Cloud Function config):
  DRIVE_FOLDER_ID         â€” Google Drive folder ID to watch
  HERMES_WEBHOOK_URL      â€” Full URL of your Hermes webhook endpoint
                            e.g. http://srv1694637.hstgr.cloud:8644/webhooks/call-transcription
  HERMES_WEBHOOK_SECRET   â€” HMAC secret shown when you ran: hermes webhook subscribe
  GCS_STATE_BUCKET        â€” GCS bucket name used to store processed file IDs
"""

import os
import json
import hmac
import hashlib
import time
import logging
from datetime import datetime, timezone

import requests
from googleapiclient.discovery import build
from google.cloud import storage as gcs

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

# â”€â”€ Config from environment â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
DRIVE_FOLDER_ID       = os.environ["DRIVE_FOLDER_ID"]
HERMES_WEBHOOK_URL    = os.environ["HERMES_WEBHOOK_URL"]
HERMES_WEBHOOK_SECRET = os.environ["HERMES_WEBHOOK_SECRET"]
GCS_STATE_BUCKET      = os.environ["GCS_STATE_BUCKET"]

PROCESSED_FILES_KEY = "processed_file_ids.json"


# â”€â”€ GCS state helpers â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

def load_processed_ids(bucket_name: str) -> set:
    client = gcs.Client()
    bucket = client.bucket(bucket_name)
    blob = bucket.blob(PROCESSED_FILES_KEY)
    if not blob.exists():
        return set()
    data = json.loads(blob.download_as_text())
    return set(data.get("ids", []))


def save_processed_ids(bucket_name: str, ids: set) -> None:
    client = gcs.Client()
    bucket = client.bucket(bucket_name)
    blob = bucket.blob(PROCESSED_FILES_KEY)
    blob.upload_from_string(
        json.dumps({"ids": list(ids), "updated_at": datetime.now(timezone.utc).isoformat()}),
        content_type="application/json",
    )


# â”€â”€ Drive helpers â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

def get_drive_service():
    """Build Drive API service using Application Default Credentials."""
    return build("drive", "v3", cache_discovery=False)


def list_txt_files(drive_service, folder_id: str) -> list[dict]:
    """Return all .txt files in the given Drive folder."""
    query = f"'{folder_id}' in parents and mimeType='text/plain' and trashed=false"
    result = drive_service.files().list(
        q=query,
        fields="files(id, name, createdTime, modifiedTime)",
        orderBy="createdTime desc",
        pageSize=50,
    ).execute()
    return result.get("files", [])


def read_file_content(drive_service, file_id: str) -> str:
    """Download a Drive file's text content."""
    content = drive_service.files().get_media(fileId=file_id).execute()
    return content.decode("utf-8") if isinstance(content, bytes) else content


# â”€â”€ Hermes webhook â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

def send_to_hermes(transcript: str, filename: str) -> bool:
    """POST the transcript to the Hermes webhook with HMAC-SHA256 signature."""
    payload = json.dumps({
        "event": "call-transcription",
        "text": transcript,
        "source": filename,
        "date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "timestamp": int(time.time()),
    })
    signature = hmac.new(
        HERMES_WEBHOOK_SECRET.encode(),
        payload.encode(),
        hashlib.sha256,
    ).hexdigest()

    try:
        resp = requests.post(
            HERMES_WEBHOOK_URL,
            data=payload,
            headers={
                "Content-Type": "application/json",
                "X-Hub-Signature-256": f"sha256={signature}",
            },
            timeout=30,
        )
        resp.raise_for_status()
        log.info("Hermes webhook accepted transcript from %s (HTTP %s)", filename, resp.status_code)
        return True
    except requests.RequestException as e:
        log.error("Failed to send to Hermes: %s", e)
        return False


# â”€â”€ Entry point â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

def process_transcripts(request):
    """
    HTTP Cloud Function entry point.
    Triggered by Cloud Scheduler (no request body needed).
    """
    log.info("Starting transcript processor run")

    drive_service = get_drive_service()
    processed_ids = load_processed_ids(GCS_STATE_BUCKET)

    files = list_txt_files(drive_service, DRIVE_FOLDER_ID)
    log.info("Found %d .txt files in Drive folder", len(files))

    new_count = 0
    for f in files:
        file_id = f["id"]
        if file_id in processed_ids:
            continue

        filename = f["name"]
        log.info("Processing new file: %s (%s)", filename, file_id)

        try:
            transcript = read_file_content(drive_service, file_id)
        except Exception as e:
            log.error("Failed to read file %s: %s", filename, e)
            continue

        sent = send_to_hermes(transcript, filename)
        if sent:
            processed_ids.add(file_id)
            new_count += 1
        else:
            log.warning("Skipping mark-as-processed for %s due to send failure", filename)

    if new_count:
        save_processed_ids(GCS_STATE_BUCKET, processed_ids)

    log.info("Run complete. Processed %d new file(s).", new_count)
    return {"status": "ok", "processed": new_count}, 200
