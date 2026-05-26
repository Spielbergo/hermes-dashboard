import Database from 'better-sqlite3';

// Per-path DB cache — opens each database once, read-only
const dbCache = new Map<string, Database.Database>();

function getDb(dbPath: string): Database.Database {
  if (!dbCache.has(dbPath)) {
    const db = new Database(dbPath, { readonly: true });
    db.pragma('journal_mode = WAL');
    dbCache.set(dbPath, db);
  }
  return dbCache.get(dbPath)!;
}

// State meta — key/value store
export function getStateMeta(dbPath: string): string {
  const db = getDb(dbPath);
  const rows = db.prepare('SELECT key, value FROM state_meta ORDER BY key').all() as any[];
  if (!rows.length) return '(no state data stored yet)';
  return rows.map(r => `• ${r.key}: ${r.value}`).join('\n');
}

// Recent messages for a session
export function getRecentMessages(dbPath: string, sessionId: string, limit = 100) {
  const db = getDb(dbPath);
  const rows = db.prepare(
    `SELECT role, content, tool_name, timestamp
     FROM messages
     WHERE session_id = ? ORDER BY timestamp DESC LIMIT ?`
  ).all(sessionId, limit) as any[];
  return rows.reverse();
}

// Sessions list (most recent first)
export function getSessions(dbPath: string, limit = 50) {
  const db = getDb(dbPath);
  const rows = db.prepare(
    `SELECT id, title, source, model, started_at, ended_at,
            message_count, input_tokens, output_tokens, estimated_cost_usd
     FROM sessions ORDER BY started_at DESC LIMIT ?`
  ).all(limit) as any[];
  return rows;
}

// Webhook sessions — sessions created by the gateway webhook platform
export function getWebhookSessions(dbPath: string, limit = 30) {
  const db = getDb(dbPath);
  try {
    const rows = db.prepare(
      `SELECT s.id, s.title, s.source, s.started_at,
              (SELECT content FROM messages
               WHERE session_id = s.id AND role = 'assistant'
               ORDER BY timestamp DESC LIMIT 1) as analysis
       FROM sessions s
       WHERE s.source LIKE '%webhook%'
       ORDER BY s.started_at DESC LIMIT ?`
    ).all(limit) as any[];
    return rows;
  } catch {
    return [];
  }
}

export function closeAllDbs() {
  for (const db of dbCache.values()) db.close();
  dbCache.clear();
}
