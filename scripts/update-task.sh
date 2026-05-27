#!/bin/bash
# Update a task status in the agent dashboard
# Usage: update-task.sh <id> <status>
# Status values: pending, in-progress, done, deleted
# Load env vars (DASHBOARD_TOKEN, etc.) from Hermes .env
HERMES_ENV="${HERMES_HOME:-/home/hermes/.hermes}/.env"
if [ -f "$HERMES_ENV" ]; then
  set -a; source "$HERMES_ENV"; set +a
fi
DASHBOARD_URL="${DASHBOARD_URL:-https://my-agent-dashboard.srv1694637.hstgr.cloud}"

ID="$1"
STATUS="$2"

if [ -z "$ID" ] || [ -z "$STATUS" ]; then
  echo "Usage: $0 <task_id> <status>"
  echo "Status values: pending, in-progress, done, deleted"
  exit 1
fi

response=$(curl -sf -X PATCH \
  -H "Authorization: Bearer ${DASHBOARD_TOKEN}" \
  -H "Content-Type: application/json" \
  -d "{\"status\": \"$STATUS\"}" \
  "$DASHBOARD_URL/api/tasks/items/$ID")

if [ $? -ne 0 ]; then
  echo "Error: Could not update task $ID"
  exit 1
fi

echo "Task $ID updated to status: $STATUS"
