import Database from 'better-sqlite3';
import { config } from './config.js';

let db: Database.Database | null = null;

function getDb() {
  if (!db) {
    db = new Database(config.dbPath, { readonly: true });
    db.pragma('journal_mode = WAL');
  }
  return db;
}

// State meta — key/value store
export function getStateMeta(): string {
  const dbInstance = getDb();
  const rows = dbInstance.prepare('SELECT key, value FROM state_meta ORDER BY key').all() as any[];
  if (!rows.length) return '(no state data stored yet)';
  return rows.map(r => `• ${r.key}: ${r.value}`).join('\n');
}

// Recent messages for a session
export function getRecentMessages(sessionId: string, limit = 100) {
  const dbInstance = getDb();
  const rows = dbInstance.prepare(
    `SELECT role, content, tool_name, timestamp
     FROM messages
     WHERE session_id = ? ORDER BY timestamp DESC LIMIT ?`
  ).all(sessionId, limit) as any[];
  return rows.reverse();
}

// Sessions list (most recent first)
export function getSessions(limit = 50) {
  const dbInstance = getDb();
  const rows = dbInstance.prepare(
    `SELECT id, title, source, model, started_at, ended_at,
            message_count, input_tokens, output_tokens, estimated_cost_usd
     FROM sessions ORDER BY started_at DESC LIMIT ?`
  ).all(limit) as any[];
  return rows;
}

export function closeDb() {
  if (db) {
    db.close();
    db = null;
  }
}
