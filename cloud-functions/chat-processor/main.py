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
  IMPERSONATE_USER       -- Email of the user whose DMs to read (e.g. "you@company.com")
  SERVICE_ACCOUNT_JSON   -- Full service account JSON key as a string
  HERMES_WEBHOOK_URL     -- e.g. http://srv1694637.hstgr.cloud:8644/webhooks/call-transcription
  HERMES_WEBHOOK_SECRET  -- HMAC secret from: hermes webhook subscribe
  GCS_STATE_BUCKET       -- GCS bucket name for state storage (same as transcript-processor)

Optional environment variables:
  LOOKBACK_DAYS          -- Days to look back on first run (default: 7)
  MIN_MESSAGES           -- Skip days with fewer messages than this (default: 2)
  CHAT_SPACE_ID          -- If set, only process this one space; otherwise all active DM spaces
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
    "https://www.googleapis.com/auth/chat.memberships.readonly",
]

# State key maps space_id -> set of processed date strings
PROCESSED_DATES_KEY = "processed_chat_dates.json"

# -- Config from environment ---------------------------------------------------

CHAT_SPACE_ID       = os.environ.get("CHAT_SPACE_ID", "")  # optional; if blank, all DM spaces are processed
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

def load_processed_dates(bucket_name: str) -> dict:
    """Return {space_id: set(date_strings)} for all tracked spaces."""
    client = gcs.Client()
    bucket = client.bucket(bucket_name)
    blob = bucket.blob(PROCESSED_DATES_KEY)
    if not blob.exists():
        return {}
    data = json.loads(blob.download_as_text())
    # Support both old format (top-level "dates" list) and new per-space format
    if "spaces" in data:
        return {space_id: set(dates) for space_id, dates in data["spaces"].items()}
    return {}


def save_processed_dates(bucket_name: str, state: dict) -> None:
    """Persist {space_id: set(date_strings)} to GCS."""
    client = gcs.Client()
    bucket = client.bucket(bucket_name)
    blob = bucket.blob(PROCESSED_DATES_KEY)
    blob.upload_from_string(
        json.dumps({
            "spaces": {space_id: sorted(dates) for space_id, dates in state.items()},
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }),
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

    # Chat API filter uses RFC3339. Only '>' (not '>=') is supported by the API.
    # Subtract 1s from start so the boundary message at exactly 00:00:00 is included.
    start_adj = day_start - timedelta(seconds=1)
    start_ts = start_adj.strftime("%Y-%m-%dT%H:%M:%SZ")
    end_ts = day_end.strftime("%Y-%m-%dT%H:%M:%SZ")
    msg_filter = f'createTime > "{start_ts}" AND createTime < "{end_ts}"'

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

def list_dm_spaces(service) -> list:
    """Return all DIRECT_MESSAGE spaces accessible to the impersonated user."""
    spaces = []
    page_token = None
    while True:
        params = {"pageSize": 100}
        if page_token:
            params["pageToken"] = page_token
        try:
            resp = service.spaces().list(**params).execute()
        except HttpError as e:
            log.warning("Could not list spaces: %s", e)
            break
        # Filter client-side -- the API filter parameter is unreliable for spaceType
        for s in resp.get("spaces", []):
            if s.get("spaceType") == "DIRECT_MESSAGE":
                spaces.append(s)
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return spaces


@functions_framework.http
def chat_processor(request):
    """HTTP Cloud Function entry point. Called by Cloud Scheduler daily."""

    # Validate required config (CHAT_SPACE_ID is now optional)
    missing = [k for k, v in {
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

    try:
        service = get_chat_service()
    except Exception as e:
        log.error("Failed to initialise Chat API: %s", e)
        return (f"Chat API init failed: {e}", 500)

    # Resolve which spaces to process
    if CHAT_SPACE_ID:
        spaces_to_process = [{"name": CHAT_SPACE_ID, "displayName": CHAT_SPACE_ID}]
    else:
        all_dm_spaces = list_dm_spaces(service)
        log.info("list_dm_spaces returned %d space(s): %s", len(all_dm_spaces), [s.get("name") for s in all_dm_spaces])
        # Filter out the Google Chat "tips" bot (no real human messages)
        spaces_to_process = [
            s for s in all_dm_spaces
            if s.get("name") != "spaces/rkhHu8AAAAE"
            and not s.get("singleUserBotDm", False)
        ]
        log.info("Discovered %d DM space(s) to process (after filter): %s", len(spaces_to_process), [s.get("name") for s in spaces_to_process])

    state = load_processed_dates(GCS_STATE_BUCKET)  # {space_id: set(dates)}

    # Determine which dates to check (yesterday + any missed within LOOKBACK_DAYS)
    today = datetime.now(timezone.utc).date()
    candidate_dates = [
        (today - timedelta(days=d)).isoformat()
        for d in range(1, LOOKBACK_DAYS + 1)
    ]

    processed_count = 0
    for space in spaces_to_process:
        space_id = space["name"]  # e.g. "spaces/yWe0K8AAAAE"
        space_key = space_id.replace("/", "_")  # safe dict key
        processed_for_space = state.get(space_key, set())

        dates_to_process = [d for d in candidate_dates if d not in processed_for_space]
        if not dates_to_process:
            log.info("%s: all dates already processed, skipping", space_id)
            continue

        members = get_space_members(service, space_id)
        log.info("%s: members=%s", space_id, members)

        for date_str in sorted(dates_to_process):
            log.info("%s: processing %s", space_id, date_str)
            try:
                messages = list_messages_for_day(service, space_id, date_str)
            except Exception as e:
                log.error("%s: error fetching messages for %s: %s", space_id, date_str, e)
                continue

            if len(messages) < MIN_MESSAGES:
                log.info("%s: skipping %s -- only %d message(s)", space_id, date_str, len(messages))
                processed_for_space.add(date_str)
                continue

            transcript = format_transcript(messages, members, date_str)
            # Name includes a short space suffix so Hermes can tell spaces apart
            space_suffix = space_id.split("/")[-1][:8]
            source_name = f"google-chat-{space_suffix}-{date_str}.txt"

            if send_to_hermes(transcript, source_name, date_str):
                processed_for_space.add(date_str)
                processed_count += 1
                log.info("%s: sent transcript for %s (%d messages)", space_id, date_str, len(messages))
            else:
                log.warning("%s: Hermes rejected %s, will retry next run", space_id, date_str)

        state[space_key] = processed_for_space

    save_processed_dates(GCS_STATE_BUCKET, state)
    return (f"Done. Processed {processed_count} chat transcript(s).", 200)


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
