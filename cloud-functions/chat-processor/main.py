"""
Google Chat Processor -- GCP Cloud Function
============================================
Runs on a Cloud Scheduler cron (e.g. daily at 01:00).
1. Reads messages from a specific Google Chat DM space for the previous day.
2. Formats them as a readable transcript (Speaker: message).
3. Sends to the Hermes webhook for AI analysis (same format as call transcripts).
4. Tracks processed dates in GCS to avoid duplicates.

--- Setup ---

1. Create a service account in the GCP Console (or reuse the one from transcript-processor).

2. Grant Domain-Wide Delegation (DWD) in Google Workspace Admin:
   Admin Console -> Security -> Access and data control -> API controls
   -> Domain-wide delegation -> Add new
   Client ID: <your service account's OAuth Client ID (numeric)>
   Scopes (comma-separated):
     https://www.googleapis.com/auth/chat.messages.readonly,
     https://www.googleapis.com/auth/chat.spaces.readonly

3. Download the service account's JSON key and store it as an environment variable:
   SERVICE_ACCOUNT_JSON = <entire JSON key file contents as a single-line string>
   (Use Cloud Run Secrets or Cloud Function secret env vars for security)

4. Find your CHAT_SPACE_ID:
   - Go to Google Chat in a browser, open the DM thread with your boss
   - Look at the URL: chat.google.com/dm/XXXXXXXXXXX
   - Your space ID is: "spaces/XXXXXXXXXXX"
   - Or: deploy this function and call the /list_spaces endpoint once to see all
     DM spaces your IMPERSONATE_USER has access to.

5. Set all required environment variables (see below).

6. Create Cloud Scheduler job:
   gcloud scheduler jobs create http chat-processor-scheduler \
     --location=us-central1 \
     --schedule="0 1 * * *" \
     --uri="https://<region>-<project>.cloudfunctions.net/chat-processor" \
     --http-method=POST \
     --oidc-service-account-email=<invoker-sa>@<project>.iam.gserviceaccount.com

Required environment variables:
  CHAT_SPACE_ID          -- e.g. "spaces/AAAAxxxxxxx"
  IMPERSONATE_USER       -- Email of one of the DM members (e.g. "you@company.com")
  SERVICE_ACCOUNT_JSON   -- Full service account JSON key as a string
  HERMES_WEBHOOK_URL     -- e.g. http://srv1694637.hstgr.cloud:8644/webhooks/call-transcription
  HERMES_WEBHOOK_SECRET  -- HMAC secret from: hermes webhook subscribe
  GCS_STATE_BUCKET       -- GCS bucket name for state storage (same as transcript-processor)

Optional environment variables:
  LOOKBACK_DAYS          -- Days to look back on first run (default: 7)
  MIN_MESSAGES           -- Skip days with fewer messages than this (default: 2)
"""

import os
import json
import hmac
import hashlib
import time
import logging
from datetime import datetime, timezone, timedelta

import functions_framework
import requests
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from google.oauth2 import service_account
from google.cloud import storage as gcs

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

CHAT_SCOPES = [
    "https://www.googleapis.com/auth/chat.messages.readonly",
    "https://www.googleapis.com/auth/chat.spaces.readonly",
]

PROCESSED_DATES_KEY = "processed_chat_dates.json"

# -- Config from environment ---------------------------------------------------

CHAT_SPACE_ID       = os.environ.get("CHAT_SPACE_ID", "")
IMPERSONATE_USER    = os.environ.get("IMPERSONATE_USER", "")
SA_JSON_STR         = os.environ.get("SERVICE_ACCOUNT_JSON", "")
HERMES_WEBHOOK_URL  = os.environ.get("HERMES_WEBHOOK_URL", "")
HERMES_WEBHOOK_SECRET = os.environ.get("HERMES_WEBHOOK_SECRET", "")
GCS_STATE_BUCKET    = os.environ.get("GCS_STATE_BUCKET", "")
LOOKBACK_DAYS       = int(os.environ.get("LOOKBACK_DAYS", "7"))
MIN_MESSAGES        = int(os.environ.get("MIN_MESSAGES", "2"))


# -- Credentials ---------------------------------------------------------------

def get_chat_service():
    """Build the Chat API client using DWD to impersonate IMPERSONATE_USER."""
    if not SA_JSON_STR:
        raise RuntimeError("SERVICE_ACCOUNT_JSON env var is not set")
    if not IMPERSONATE_USER:
        raise RuntimeError("IMPERSONATE_USER env var is not set")

    sa_info = json.loads(SA_JSON_STR)
    creds = service_account.Credentials.from_service_account_info(
        sa_info, scopes=CHAT_SCOPES
    )
    delegated = creds.with_subject(IMPERSONATE_USER)
    return build("chat", "v1", credentials=delegated, cache_discovery=False)


# -- GCS state helpers ---------------------------------------------------------

def load_processed_dates(bucket_name: str) -> set:
    client = gcs.Client()
    bucket = client.bucket(bucket_name)
    blob = bucket.blob(PROCESSED_DATES_KEY)
    if not blob.exists():
        return set()
    data = json.loads(blob.download_as_text())
    return set(data.get("dates", []))


def save_processed_dates(bucket_name: str, dates: set) -> None:
    client = gcs.Client()
    bucket = client.bucket(bucket_name)
    blob = bucket.blob(PROCESSED_DATES_KEY)
    blob.upload_from_string(
        json.dumps({"dates": sorted(dates), "updated_at": datetime.now(timezone.utc).isoformat()}),
        content_type="application/json",
    )


# -- Chat API helpers ----------------------------------------------------------

def get_space_members(service, space_id: str) -> dict:
    """Return a map of user resource name -> display name."""
    members = {}
    page_token = None
    while True:
        params = {"parent": space_id, "pageSize": 100}
        if page_token:
            params["pageToken"] = page_token
        try:
            resp = service.spaces().members().list(**params).execute()
        except HttpError as e:
            log.warning("Could not list members: %s", e)
            break
        for m in resp.get("memberships", []):
            member = m.get("member", {})
            name = member.get("name", "")
            display_name = member.get("displayName") or member.get("name", "Unknown")
            if name:
                members[name] = display_name
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return members


def list_messages_for_day(service, space_id: str, date_str: str) -> list:
    """Return all messages in the space for the given date (YYYY-MM-DD UTC)."""
    day_start = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    day_end = day_start + timedelta(days=1)

    # Chat API filter uses RFC3339
    start_ts = day_start.strftime("%Y-%m-%dT%H:%M:%SZ")
    end_ts = day_end.strftime("%Y-%m-%dT%H:%M:%SZ")
    msg_filter = f'createTime >= "{start_ts}" AND createTime < "{end_ts}"'

    messages = []
    page_token = None
    while True:
        params = {
            "parent": space_id,
            "filter": msg_filter,
            "pageSize": 1000,
            "orderBy": "createTime asc",
        }
        if page_token:
            params["pageToken"] = page_token
        try:
            resp = service.spaces().messages().list(**params).execute()
        except HttpError as e:
            log.error("Chat API error listing messages: %s", e)
            break
        messages.extend(resp.get("messages", []))
        page_token = resp.get("nextPageToken")
        if not page_token:
            break

    return messages


def format_transcript(messages: list, members: dict, date_str: str) -> str:
    """Format a list of Chat messages as a readable transcript."""
    lines = [f"Google Chat Transcript -- {date_str}", "=" * 50, ""]
    for msg in messages:
        sender_name = msg.get("sender", {}).get("name", "")
        display = members.get(sender_name) or sender_name.split("/")[-1]

        # Parse timestamp
        create_time = msg.get("createTime", "")
        try:
            ts = datetime.fromisoformat(create_time.replace("Z", "+00:00"))
            time_str = ts.strftime("%H:%M")
        except Exception:
            time_str = ""

        text = msg.get("text", "").strip()
        if not text:
            # Could be a card/attachment -- note it
            if msg.get("cards") or msg.get("cardsV2"):
                text = "[attachment/card]"
            else:
                continue

        prefix = f"[{time_str}] {display}" if time_str else display
        lines.append(f"{prefix}: {text}")

    return "\n".join(lines)


# -- Hermes webhook ------------------------------------------------------------

def send_to_hermes(transcript: str, source_name: str, date_str: str) -> bool:
    """POST the transcript to the Hermes webhook with HMAC-SHA256 signature."""
    payload = json.dumps({
        "event": "call-transcription",
        "text": transcript,
        "source": source_name,
        "date": date_str,
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
        log.info("Hermes accepted Chat transcript for %s (HTTP %s)", date_str, resp.status_code)
        return True
    except requests.RequestException as e:
        log.error("Failed to send to Hermes: %s", e)
        return False


# -- Main entry point ----------------------------------------------------------

@functions_framework.http
def chat_processor(request):
    """HTTP Cloud Function entry point. Called by Cloud Scheduler daily."""

    # Validate required config
    missing = [k for k, v in {
        "CHAT_SPACE_ID": CHAT_SPACE_ID,
        "IMPERSONATE_USER": IMPERSONATE_USER,
        "SERVICE_ACCOUNT_JSON": SA_JSON_STR,
        "HERMES_WEBHOOK_URL": HERMES_WEBHOOK_URL,
        "HERMES_WEBHOOK_SECRET": HERMES_WEBHOOK_SECRET,
        "GCS_STATE_BUCKET": GCS_STATE_BUCKET,
    }.items() if not v]
    if missing:
        msg = f"Missing required env vars: {', '.join(missing)}"
        log.error(msg)
        return (msg, 500)

    # Special: list_spaces helper (GET ?action=list_spaces)
    if request.method == "GET" and request.args.get("action") == "list_spaces":
        return list_spaces_helper()

    processed_dates = load_processed_dates(GCS_STATE_BUCKET)

    # Determine which dates to process (yesterday + any missed days within LOOKBACK_DAYS)
    today = datetime.now(timezone.utc).date()
    dates_to_process = []
    for days_ago in range(1, LOOKBACK_DAYS + 1):
        d = (today - timedelta(days=days_ago)).isoformat()
        if d not in processed_dates:
            dates_to_process.append(d)

    if not dates_to_process:
        log.info("No new dates to process.")
        return ("No new dates to process.", 200)

    log.info("Dates to process: %s", dates_to_process)

    try:
        service = get_chat_service()
        members = get_space_members(service, CHAT_SPACE_ID)
        log.info("Space members resolved: %s", members)
    except Exception as e:
        log.error("Failed to initialise Chat API: %s", e)
        return (f"Chat API init failed: {e}", 500)

    processed_count = 0
    for date_str in sorted(dates_to_process):
        log.info("Processing date: %s", date_str)
        try:
            messages = list_messages_for_day(service, CHAT_SPACE_ID, date_str)
        except Exception as e:
            log.error("Error fetching messages for %s: %s", date_str, e)
            continue

        if len(messages) < MIN_MESSAGES:
            log.info("Skipping %s -- only %d message(s) (below MIN_MESSAGES=%d)",
                     date_str, len(messages), MIN_MESSAGES)
            processed_dates.add(date_str)
            continue

        transcript = format_transcript(messages, members, date_str)
        source_name = f"google-chat-{date_str}.txt"

        if send_to_hermes(transcript, source_name, date_str):
            processed_dates.add(date_str)
            processed_count += 1
            log.info("Processed %s (%d messages)", date_str, len(messages))
        else:
            log.warning("Hermes rejected transcript for %s, will retry next run", date_str)

    save_processed_dates(GCS_STATE_BUCKET, processed_dates)
    return (f"Done. Processed {processed_count} day(s).", 200)


def list_spaces_helper():
    """Helper: list all DM spaces with a sample message to help identify them.
    Useful for finding CHAT_SPACE_ID. Call with GET ?action=list_spaces."""
    try:
        service = get_chat_service()
        resp = service.spaces().list(pageSize=100).execute()
        spaces = []
        for s in resp.get("spaces", []):
            space_name = s.get("name")
            sample = ""
            try:
                msg_resp = service.spaces().messages().list(
                    parent=space_name, pageSize=3, orderBy="createTime desc"
                ).execute()
                for msg in msg_resp.get("messages", []):
                    sender = msg.get("sender", {}).get("displayName", "?")
                    text = msg.get("text", "")[:60]
                    ts = msg.get("createTime", "")[:10]
                    sample += f"[{ts}] {sender}: {text}\n"
            except Exception as e:
                sample = f"(error: {e})"
            spaces.append({
                "name": space_name,
                "type": s.get("spaceType"),
                "recent_messages": sample.strip() or "(no messages)",
            })
        return (json.dumps(spaces, indent=2), 200, {"Content-Type": "application/json"})
    except Exception as e:
        return (str(e), 500)
