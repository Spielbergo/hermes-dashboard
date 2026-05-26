"""
Transcript Processor — GCP Cloud Function
==========================================
Runs on a Cloud Scheduler cron (e.g. every 5 minutes).
1. Polls a Google Drive folder for new .txt transcription files.
2. Sends each new transcript to the Hermes webhook for AI analysis.
3. Records processed file IDs in a GCS state bucket to avoid duplicates.

Required environment variables (set in Cloud Function config):
  DRIVE_FOLDER_ID         — Google Drive folder ID to watch
  HERMES_WEBHOOK_URL      — Full URL of your Hermes webhook endpoint
                            e.g. http://srv1694637.hstgr.cloud:8644/webhooks/call-transcription
  HERMES_WEBHOOK_SECRET   — HMAC secret shown when you ran: hermes webhook subscribe
  GCS_STATE_BUCKET        — GCS bucket name used to store processed file IDs
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

# ── Config from environment ──────────────────────────────────────────────────
DRIVE_FOLDER_ID       = os.environ["DRIVE_FOLDER_ID"]
HERMES_WEBHOOK_URL    = os.environ["HERMES_WEBHOOK_URL"]
HERMES_WEBHOOK_SECRET = os.environ["HERMES_WEBHOOK_SECRET"]
GCS_STATE_BUCKET      = os.environ["GCS_STATE_BUCKET"]

PROCESSED_FILES_KEY = "processed_file_ids.json"


# ── GCS state helpers ────────────────────────────────────────────────────────

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


# ── Drive helpers ────────────────────────────────────────────────────────────

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


# ── Hermes webhook ───────────────────────────────────────────────────────────

def send_to_hermes(transcript: str, filename: str) -> bool:
    """POST the transcript to the Hermes webhook with HMAC-SHA256 signature."""
    payload = json.dumps({
        "event": "call-transcription",
        "text": transcript,
        "source": filename,
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
                "X-Hermes-Signature": f"sha256={signature}",
            },
            timeout=30,
        )
        resp.raise_for_status()
        log.info("Hermes webhook accepted transcript from %s (HTTP %s)", filename, resp.status_code)
        return True
    except requests.RequestException as e:
        log.error("Failed to send to Hermes: %s", e)
        return False


# ── Entry point ──────────────────────────────────────────────────────────────

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

import os
import json
import hmac
import hashlib
import time
import logging
from datetime import datetime, timezone

import requests
import google.generativeai as genai
from google.oauth2 import service_account
from googleapiclient.discovery import build
from google.cloud import storage as gcs

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

# ── Config from environment ──────────────────────────────────────────────────
DRIVE_FOLDER_ID       = os.environ["DRIVE_FOLDER_ID"]
HERMES_WEBHOOK_URL    = os.environ["HERMES_WEBHOOK_URL"]
HERMES_WEBHOOK_SECRET = os.environ["HERMES_WEBHOOK_SECRET"]
GCS_STATE_BUCKET      = os.environ["GCS_STATE_BUCKET"]
GEMINI_API_KEY        = os.environ["GEMINI_API_KEY"]
GOOGLE_TASKS_LIST_ID  = os.environ.get("GOOGLE_TASKS_LIST_ID", "@default")
TASKS_DELIVERY_URL    = os.environ.get("TASKS_DELIVERY_URL", "")
TASKS_DELIVERY_TOKEN  = os.environ.get("TASKS_DELIVERY_TOKEN", "")

PROCESSED_FILES_KEY = "processed_file_ids.json"

TASK_EXTRACTION_PROMPT = """
You are analyzing a work call transcript to extract actionable daily tasks.
Extract all tasks, assignments, and action items mentioned in the transcript.

Return ONLY valid JSON in this exact format (no markdown, no explanation):
{
  "date": "YYYY-MM-DD",
  "tasks": [
    {
      "priority": "high|medium|low",
      "title": "Short task name",
      "context": "Context or notes from the call",
      "assigned_to": "Name or null",
      "due": "Due date/time or null"
    }
  ]
}

Transcript:
{transcript}
"""


# ── GCS state helpers ────────────────────────────────────────────────────────

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


# ── Drive helpers ────────────────────────────────────────────────────────────

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


# ── Hermes webhook ───────────────────────────────────────────────────────────

def send_to_hermes(transcript: str, filename: str) -> bool:
    """POST the transcript to the Hermes webhook with HMAC-SHA256 signature."""
    payload = json.dumps({
        "event": "call-transcription",
        "text": transcript,
        "source": filename,
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
                "X-Hermes-Signature": f"sha256={signature}",
            },
            timeout=30,
        )
        resp.raise_for_status()
        log.info("Hermes webhook accepted transcript from %s", filename)
        return True
    except requests.RequestException as e:
        log.error("Failed to send to Hermes: %s", e)
        return False


# ── Gemini task extraction ───────────────────────────────────────────────────

def extract_tasks(transcript: str, date_str: str) -> dict | None:
    """Use Gemini to extract structured tasks from the transcript."""
    genai.configure(api_key=GEMINI_API_KEY)
    model = genai.GenerativeModel("gemini-2.0-flash")
    prompt = TASK_EXTRACTION_PROMPT.replace("{transcript}", transcript)

    try:
        response = model.generate_content(prompt)
        raw = response.text.strip()
        # Strip markdown code fences if Gemini wraps it
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        data = json.loads(raw)
        if "date" not in data or not data.get("date"):
            data["date"] = date_str
        return data
    except Exception as e:
        log.error("Gemini task extraction failed: %s", e)
        return None


# ── Google Tasks ─────────────────────────────────────────────────────────────

def create_google_tasks(tasks_data: dict) -> None:
    """Create tasks in Google Tasks from extracted task data."""
    tasks_service = build("tasks", "v1", cache_discovery=False)
    date_str = tasks_data.get("date", datetime.now(timezone.utc).strftime("%Y-%m-%d"))

    for task in tasks_data.get("tasks", []):
        priority_emoji = {"high": "🔴", "medium": "🟡", "low": "🟢"}.get(
            task.get("priority", "medium"), "🟡"
        )
        title = f"{priority_emoji} {task.get('title', 'Untitled task')}"
        notes_parts = []
        if task.get("context"):
            notes_parts.append(task["context"])
        if task.get("assigned_to"):
            notes_parts.append(f"Assigned to: {task['assigned_to']}")
        if task.get("due"):
            notes_parts.append(f"Due: {task['due']}")
        notes_parts.append(f"From call transcript — {date_str}")

        body = {
            "title": title,
            "notes": "\n".join(notes_parts),
            "status": "needsAction",
        }
        try:
            tasks_service.tasks().insert(
                tasklist=GOOGLE_TASKS_LIST_ID, body=body
            ).execute()
            log.info("Created Google Task: %s", title)
        except Exception as e:
            log.error("Failed to create Google Task '%s': %s", title, e)


# ── Dashboard delivery ───────────────────────────────────────────────────────

def send_to_dashboard(tasks_data: dict, filename: str) -> None:
    """POST extracted tasks to the dashboard /api/ingest/tasks endpoint."""
    if not TASKS_DELIVERY_URL:
        return
    try:
        resp = requests.post(
            TASKS_DELIVERY_URL,
            json={**tasks_data, "source_file": filename},
            headers={"Authorization": f"Bearer {TASKS_DELIVERY_TOKEN}"},
            timeout=10,
        )
        resp.raise_for_status()
        log.info("Tasks delivered to dashboard")
    except requests.RequestException as e:
        log.warning("Dashboard delivery failed (non-fatal): %s", e)


# ── Entry point ──────────────────────────────────────────────────────────────

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
        date_str = f.get("createdTime", "")[:10] or datetime.now(timezone.utc).strftime("%Y-%m-%d")
        log.info("Processing new file: %s (%s)", filename, file_id)

        try:
            transcript = read_file_content(drive_service, file_id)
        except Exception as e:
            log.error("Failed to read file %s: %s", filename, e)
            continue

        # 1. Send full transcript to Hermes (Telegram delivery + LLM analysis)
        send_to_hermes(transcript, filename)

        # 2. Extract structured tasks via Gemini
        tasks_data = extract_tasks(transcript, date_str)
        if tasks_data:
            # 3. Create in Google Tasks
            create_google_tasks(tasks_data)
            # 4. Send to dashboard
            send_to_dashboard(tasks_data, filename)

        processed_ids.add(file_id)
        new_count += 1

    if new_count:
        save_processed_ids(GCS_STATE_BUCKET, processed_ids)

    log.info("Processed %d new transcript(s)", new_count)
    return json.dumps({"processed": new_count}), 200, {"Content-Type": "application/json"}
