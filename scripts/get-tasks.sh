#!/bin/bash
# Fetch tasks from the agent dashboard
# Load env vars (DASHBOARD_TOKEN, etc.) from Hermes .env
HERMES_ENV="${HERMES_HOME:-/home/hermes/.hermes}/.env"
if [ -f "$HERMES_ENV" ]; then
  set -a; source "$HERMES_ENV"; set +a
fi
DASHBOARD_URL="${DASHBOARD_URL:-https://my-agent-dashboard.srv1694637.hstgr.cloud}"

response=$(curl -sf -H "Authorization: Bearer ${DASHBOARD_TOKEN}" "$DASHBOARD_URL/api/tasks/items")
if [ $? -ne 0 ]; then
  echo 'Error: Could not reach dashboard'
  exit 1
fi

echo "$response" | python3 -c "
import json, sys
data = json.load(sys.stdin)
items = data.get('items', [])
if not items:
    print('No tasks found in the dashboard.')
    sys.exit(0)
print(f'Dashboard Tasks ({len(items)} total):')
print()
for item in items:
    status = item['status']
    priority = (item.get('priority') or 'normal').upper()
    title = item['title']
    context = item.get('context') or ''
    date = item.get('date', '')
    id_ = item['id']
    icons = {'pending': '[PENDING]', 'done': '[DONE]', 'in-progress': '[IN PROGRESS]', 'deleted': '[DELETED]'}
    icon = icons.get(status, f'[{status.upper()}]')
    print(f'  ID {id_} | {icon} | [{priority}] {title}')
    if context:
        print(f'         Context: {context}')
    if date:
        print(f'         Added: {date}')
    print()
"
