"""
Transcript Processor - GCP Cloud Function
==========================================
Runs on a Cloud Scheduler cron (e.g. every 5 minutes).
1. Polls a Google Drive folder for new .txt transcription files.
2. Sends each new transcript to the Hermes webhook for AI analysis.
3. Records processed file IDs in a GCS state bucket to avoid duplicates.

Management actions (require X-Hub-Signature-256 HMAC header with HERMES_WEBHOOK_SECRET):
  GET  ?action=status          - returns { enabled, processed_count }
  POST ?action=enable          - enables Drive polling
  POST ?action=disable         - disables Drive polling
  POST ?action=run             - triggers an immediate poll regardless of enabled state

Required environment variables (set in Cloud Function config):
  DRIVE_FOLDER_ID         - Google Drive folder ID to watch
  HERMES_WEBHOOK_URL      - Full URL of your Hermes webhook endpoint
  HERMES_WEBHOOK_SECRET   - HMAC secret used to sign webhook payloads + authenticate management calls
  GCS_STATE_BUCKET        - GCS bucket name used to store processed file IDs
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
POLLING_STATE_KEY   = "polling_state.json"


# ── Auth helper ──────────────────────────────────────────────────────────────

def _verify_hmac(request) -> bool:
    """Return True if the request carries a valid HMAC-SHA256 signature."""
    sig_header = request.headers.get("X-Hub-Signature-256", "")
    if not sig_header.startswith("sha256="):
        return False
    body = request.get_data()
    expected = "sha256=" + hmac.new(
        HERMES_WEBHOOK_SECRET.encode(), body, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, sig_header)


# ── Polling-enabled state (stored in GCS) ────────────────────────────────────

def get_polling_enabled() -> bool:
    """Read the polling-enabled flag from GCS. Defaults to False."""
    try:
        client = gcs.Client()
        blob = client.bucket(GCS_STATE_BUCKET).blob(POLLING_STATE_KEY)
        if not blob.exists():
            return False
        data = json.loads(blob.download_as_text())
        return bool(data.get("enabled", False))
    except Exception as e:
        log.warning("Could not read polling state: %s", e)
        return False


def set_polling_enabled(enabled: bool) -> None:
    """Write the polling-enabled flag to GCS."""
    client = gcs.Client()
    blob = client.bucket(GCS_STATE_BUCKET).blob(POLLING_STATE_KEY)
    blob.upload_from_string(
        json.dumps({"enabled": enabled, "updated_at": datetime.now(timezone.utc).isoformat()}),
        content_type="application/json",
    )


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
    Triggered by Cloud Scheduler (no request body needed), or called manually.
    Also handles management actions via ?action= query parameter.
    """
    action = request.args.get("action", "")

    # ── Management actions ──────────────────────────────────────────────────
    if action == "status":
        enabled = get_polling_enabled()
        try:
            ids = load_processed_ids(GCS_STATE_BUCKET)
            count = len(ids)
        except Exception:
            count = -1
        return {"enabled": enabled, "processed_count": count}, 200

    if action in ("enable", "disable", "run"):
        if not _verify_hmac(request):
            return {"error": "Unauthorized"}, 401
        if action == "enable":
            set_polling_enabled(True)
            log.info("Drive polling enabled via management API")
            return {"ok": True, "enabled": True}, 200
        if action == "disable":
            set_polling_enabled(False)
            log.info("Drive polling disabled via management API")
            return {"ok": True, "enabled": False}, 200
        # action == "run": fall through to run the poll now regardless of flag

    # ── Scheduled / manual run ──────────────────────────────────────────────
    if action == "" and not get_polling_enabled():
        log.info("Drive polling is disabled — skipping run")
        return {"status": "disabled"}, 200

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
