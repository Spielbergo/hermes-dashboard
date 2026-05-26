import Database from 'better-sqlite3';
import fs from 'fs';
import path from 'path';
import { fileURLToPath } from 'url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const DB_PATH = process.env.TASKS_DB_PATH ?? path.resolve(__dirname, '..', 'data', 'tasks.db');

// Ensure the directory exists (works in both Docker /data and local ./data)
fs.mkdirSync(path.dirname(DB_PATH), { recursive: true });

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

      CREATE TABLE IF NOT EXISTS task_items (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id  TEXT NOT NULL,
        item_index  INTEGER NOT NULL,
        date        TEXT NOT NULL,
        source      TEXT,
        priority    TEXT,
        title       TEXT NOT NULL,
        context     TEXT,
        status      TEXT DEFAULT 'pending',
        sort_order  INTEGER DEFAULT 0,
        created_at  INTEGER DEFAULT (strftime('%s', 'now')),
        UNIQUE(session_id, item_index)
      );
      CREATE INDEX IF NOT EXISTS idx_task_items_date   ON task_items(date DESC);
      CREATE INDEX IF NOT EXISTS idx_task_items_status ON task_items(status);
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

// ── Task Items ────────────────────────────────────────────────────────────

export interface TaskItemInput {
  index: number;
  priority?: string;
  title: string;
  context?: string | null;
}

export interface TaskItem {
  id: number;
  session_id: string;
  item_index: number;
  date: string;
  source: string | null;
  priority: string | null;
  title: string;
  context: string | null;
  status: string;
  sort_order: number;
  created_at: number;
}

export function syncTaskItems(
  sessionId: string,
  date: string,
  source: string | null,
  items: TaskItemInput[]
): void {
  const db = getDb();
  const upsert = db.prepare(`
    INSERT INTO task_items (session_id, item_index, date, source, priority, title, context, sort_order)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    ON CONFLICT(session_id, item_index) DO NOTHING
  `);
  const syncMany = db.transaction((items: TaskItemInput[]) => {
    items.forEach((item, i) => {
      upsert.run(sessionId, item.index, date, source ?? null, item.priority ?? null, item.title, item.context ?? null, i);
    });
  });
  syncMany(items);
}

export function getTaskItems(): TaskItem[] {
  return getDb()
    .prepare(`SELECT * FROM task_items WHERE status != 'deleted' ORDER BY date DESC, sort_order ASC, id ASC`)
    .all() as TaskItem[];
}

export function updateTaskItem(id: number, patch: { status?: string; sort_order?: number }): void {
  const db = getDb();
  const fields: string[] = [];
  const values: any[] = [];
  if (patch.status !== undefined)     { fields.push('status = ?');     values.push(patch.status); }
  if (patch.sort_order !== undefined) { fields.push('sort_order = ?'); values.push(patch.sort_order); }
  if (!fields.length) return;
  values.push(id);
  db.prepare(`UPDATE task_items SET ${fields.join(', ')} WHERE id = ?`).run(...values);
}

export function bulkUpdateStatus(ids: number[], status: string): void {
  const db = getDb();
  const stmt = db.prepare('UPDATE task_items SET status = ? WHERE id = ?');
  const update = db.transaction((ids: number[]) => { ids.forEach(id => stmt.run(status, id)); });
  update(ids);
}

export function reorderTaskItems(items: { id: number; sort_order: number }[]): void {
  const db = getDb();
  const stmt = db.prepare('UPDATE task_items SET sort_order = ? WHERE id = ?');
  const update = db.transaction((items: { id: number; sort_order: number }[]) => {
    items.forEach(({ id, sort_order }) => stmt.run(sort_order, id));
  });
  update(items);
}

export function closeTasksDb() {
  if (db) { db.close(); db = null; }
}
