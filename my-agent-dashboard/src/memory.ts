import Database from 'better-sqlite3';
import { config } from './config.js';

let db: Database.Database | null = null;

function getDb() {
  if (!db) {
    db = new Database(config.dbPath);
    // Enable WAL mode for better performance/concurrency
    db.pragma('journal_mode = WAL');
  }
  return db;
}

// Tier 1: Core Memory
export function getCoreMemory(): string {
  const dbInstance = getDb();
  const rows = dbInstance.prepare('SELECT key, value FROM core_memory ORDER BY key').all() as any[];
  if (!rows.length) return '(no facts stored yet)';
  return rows.map(r => `• ${r.key}: ${r.value}`).join('\n');
}

// Tier 2: Conversation Log
export function getRecentMessages(chatId: string, limit = 20) {
  const dbInstance = getDb();
  const rows = dbInstance.prepare(
    `SELECT role, content FROM messages
     WHERE chat_id = ? ORDER BY id DESC LIMIT ?`
  ).all(chatId, limit) as any[];
  return rows.reverse();
}

export function getSummary(chatId: string): string | null {
  const dbInstance = getDb();
  const row = dbInstance.prepare('SELECT summary FROM summaries WHERE chat_id = ?')
    .get(chatId) as any;
  return row?.summary ?? null;
}

// Add more functions here to fetch todos, skills, etc., if they are stored in the DB

export function closeDb() {
  if (db) {
    db.close();
    db = null;
  }
}
