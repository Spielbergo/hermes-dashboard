import Database from 'better-sqlite3';
import path from 'path';
import { fileURLToPath } from 'url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const DB_PATH = path.resolve(__dirname, '..', 'data', 'tasks.db');

let db: Database.Database | null = null;

function getDb(): Database.Database {
  if (!db) {
    db = new Database(DB_PATH);
    db.pragma('journal_mode = WAL');
    db.exec(`
      CREATE TABLE IF NOT EXISTS task_reports (
        id        INTEGER PRIMARY KEY AUTOINCREMENT,
        date      TEXT NOT NULL,
        source    TEXT,
        raw_json  TEXT NOT NULL,
        created_at INTEGER DEFAULT (strftime('%s', 'now'))
      );
      CREATE INDEX IF NOT EXISTS idx_task_reports_date ON task_reports(date DESC);
    `);
  }
  return db;
}

export interface Task {
  priority: 'high' | 'medium' | 'low';
  title: string;
  context?: string;
  assigned_to?: string;
  due?: string;
}

export interface TaskReport {
  id: number;
  date: string;
  source: string | null;
  tasks: Task[];
  created_at: number;
}

export function saveTaskReport(date: string, source: string | null, tasks: Task[]): number {
  const result = getDb().prepare(
    'INSERT INTO task_reports (date, source, raw_json) VALUES (?, ?, ?)'
  ).run(date, source ?? null, JSON.stringify(tasks));
  return result.lastInsertRowid as number;
}

export function getTaskReports(limit = 30): TaskReport[] {
  const rows = getDb().prepare(
    'SELECT id, date, source, raw_json, created_at FROM task_reports ORDER BY date DESC, id DESC LIMIT ?'
  ).all(limit) as any[];
  return rows.map(r => ({
    id: r.id,
    date: r.date,
    source: r.source,
    tasks: JSON.parse(r.raw_json) as Task[],
    created_at: r.created_at,
  }));
}

export function closeTasksDb() {
  if (db) { db.close(); db = null; }
}
