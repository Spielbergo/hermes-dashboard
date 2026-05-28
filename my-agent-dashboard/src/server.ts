import express from 'express';
import cors from 'cors';
import path from 'path';
import { fileURLToPath } from 'url';
import { createHmac } from 'crypto';
import { config } from './config.js';
import { getStateMeta, getRecentMessages, getSessions, getWebhookSessions, deleteSession, closeAllDbs } from './memory.js';
import { saveTaskReport, getTaskReports, syncTaskItems, getTaskItems, updateTaskItem, bulkUpdateStatus, reorderTaskItems, closeTasksDb } from './tasks.js';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const frontendDir = path.resolve(__dirname, '..', 'frontend');

const app = express();

app.use(express.json({ limit: '1mb' }));

// Serve frontend static files publicly (no auth required to load the page)
app.use(express.static(frontendDir));

// CORS — only needed when running the frontend separately (e.g. local dev).
if (config.corsOrigin) {
  app.use('/api', cors({
    origin: config.corsOrigin,
    methods: ['GET', 'POST', 'OPTIONS'],
    allowedHeaders: ['Content-Type', 'Authorization'],
    credentials: true,
  }));
}

// Auth middleware scoped to /api/* only
app.use('/api', (req, res, next) => {
  if (!config.dashboardToken) {
    console.error('DASHBOARD_TOKEN is not set in .env');
    return res.status(500).send('Dashboard token not configured.');
  }
  const authHeader = req.headers.authorization;
  if (!authHeader || authHeader !== `Bearer ${config.dashboardToken}`) {
    console.warn(`Unauthorized access attempt from ${req.ip}`);
    return res.status(401).send('Unauthorized');
  }
  next();
});

// Helper — resolve agentId to its dbPath
function resolveAgent(agentId: string) {
  return config.agents.find(a => a.id === agentId) ?? null;
}

// ── API Endpoints ──

// List available agents (id + name only — no sensitive paths)
app.get('/api/agents', (_, res) => {
  res.json({ agents: config.agents.map(({ id, name }) => ({ id, name })) });
});

// State meta for a specific agent
app.get('/api/:agentId/memory', (req, res) => {
  const agent = resolveAgent(req.params.agentId);
  if (!agent) return res.status(404).json({ error: 'Agent not found' });
  try {
    res.json({ memory: getStateMeta(agent.dbPath) });
  } catch (error: any) {
    console.error('Error fetching state meta:', error);
    res.status(500).json({ error: `Failed to fetch state: ${error.message}` });
  }
});

// Sessions for a specific agent
app.get('/api/:agentId/sessions', (req, res) => {
  const agent = resolveAgent(req.params.agentId);
  if (!agent) return res.status(404).json({ error: 'Agent not found' });
  try {
    res.json({ sessions: getSessions(agent.dbPath, 50) });
  } catch (error: any) {
    console.error('Error fetching sessions:', error);
    res.status(500).json({ error: `Failed to fetch sessions: ${error.message}` });
  }
});

// Messages for a specific session within a specific agent
app.get('/api/:agentId/messages/:sessionId', (req, res) => {
  const agent = resolveAgent(req.params.agentId);
  if (!agent) return res.status(404).json({ error: 'Agent not found' });
  try {
    const messages = getRecentMessages(agent.dbPath, req.params.sessionId, 100);
    res.json({ messages });
  } catch (error: any) {
    console.error('Error fetching messages:', error);
    res.status(500).json({ error: `Failed to fetch messages: ${error.message}` });
  }
});

// Delete a session and its messages
app.delete('/api/:agentId/sessions/:sessionId', (req, res) => {
  const agent = resolveAgent(req.params.agentId);
  if (!agent) return res.status(404).json({ error: 'Agent not found' });
  try {
    deleteSession(agent.dbPath, req.params.sessionId);
    res.json({ ok: true });
  } catch (error: any) {
    console.error('Error deleting session:', error);
    res.status(500).json({ error: `Failed to delete session: ${error.message}` });
  }
});

// Webhook sessions for a specific agent (source LIKE '%webhook%')
app.get('/api/:agentId/webhook-sessions', (req, res) => {
  const agent = resolveAgent(req.params.agentId);
  if (!agent) return res.status(404).json({ error: 'Agent not found' });
  try {
    res.json({ sessions: getWebhookSessions(agent.dbPath, 30) });
  } catch (error: any) {
    console.error('Error fetching webhook sessions:', error);
    res.status(500).json({ error: `Failed to fetch webhook sessions: ${error.message}` });
  }
});

// Ingest tasks from GCP Cloud Function — uses same DASHBOARD_TOKEN auth
app.post('/api/ingest/tasks', (req, res) => {
  const body = req.body;
  if (!body || typeof body !== 'object') {
    return res.status(400).json({ error: 'Invalid payload' });
  }
  const date: string = body.date || new Date().toISOString().slice(0, 10);
  const source: string | null = body.source_file ?? body.source ?? null;
  const tasks = Array.isArray(body.tasks) ? body.tasks : [];
  if (!tasks.length) {
    return res.status(400).json({ error: 'No tasks in payload' });
  }
  try {
    const id = saveTaskReport(date, source, tasks);
    res.json({ ok: true, id });
  } catch (error: any) {
    console.error('Error saving task report:', error);
    res.status(500).json({ error: error.message });
  }
});

// Read task reports
app.get('/api/tasks', (_, res) => {
  try {
    res.json({ reports: getTaskReports(30) });
  } catch (error: any) {
    console.error('Error reading task reports:', error);
    res.status(500).json({ error: error.message });
  }
});

// ── Task Items (individual task management) ──────────────────────────────

app.post('/api/tasks/sync', (req, res) => {
  const { session_id, date, source, items } = req.body;
  if (!session_id || !Array.isArray(items) || !items.length) {
    return res.status(400).json({ error: 'session_id and items[] required' });
  }
  try {
    syncTaskItems(session_id, date || new Date().toISOString().slice(0, 10), source ?? null, items);
    res.json({ ok: true });
  } catch (error: any) {
    res.status(500).json({ error: error.message });
  }
});

app.get('/api/tasks/items', (_, res) => {
  try {
    res.json({ items: getTaskItems() });
  } catch (error: any) {
    res.status(500).json({ error: error.message });
  }
});

app.patch('/api/tasks/items/:id', (req, res) => {
  const id = parseInt(req.params.id);
  if (isNaN(id)) return res.status(400).json({ error: 'Invalid id' });
  try {
    updateTaskItem(id, req.body);
    res.json({ ok: true });
  } catch (error: any) {
    res.status(500).json({ error: error.message });
  }
});

app.delete('/api/tasks/items/:id', (req, res) => {
  const id = parseInt(req.params.id);
  if (isNaN(id)) return res.status(400).json({ error: 'Invalid id' });
  try {
    updateTaskItem(id, { status: 'deleted' });
    res.json({ ok: true });
  } catch (error: any) {
    res.status(500).json({ error: error.message });
  }
});

app.post('/api/tasks/items/bulk', (req, res) => {
  const { ids, status } = req.body;
  if (!Array.isArray(ids) || !status) return res.status(400).json({ error: 'ids[] and status required' });
  try {
    bulkUpdateStatus(ids.map(Number), status);
    res.json({ ok: true });
  } catch (error: any) {
    res.status(500).json({ error: error.message });
  }
});

app.post('/api/tasks/items/reorder', (req, res) => {
  const { items } = req.body;
  if (!Array.isArray(items)) return res.status(400).json({ error: 'items[] required' });
  try {
    reorderTaskItems(items);
    res.json({ ok: true });
  } catch (error: any) {
    res.status(500).json({ error: error.message });
  }
});

// ── Drive Polling Control ─────────────────────────────────────────────────

async function callProcessor(action: string): Promise<{ ok: boolean; data?: any; error?: string }> {
  const url = config.transcriptProcessorUrl;
  if (!url) return { ok: false, error: 'TRANSCRIPT_PROCESSOR_URL not configured' };
  const secret = config.transcriptProcessorSecret;
  const body = JSON.stringify({ action });
  const sig = 'sha256=' + createHmac('sha256', secret).update(body).digest('hex');
  const method = action === 'status' ? 'GET' : 'POST';
  const reqUrl = method === 'GET' ? `${url}?action=${action}` : url + `?action=${action}`;
  try {
    const res = await fetch(reqUrl, {
      method,
      headers: { 'Content-Type': 'application/json', 'X-Hub-Signature-256': sig },
      body: method === 'POST' ? body : undefined,
      signal: AbortSignal.timeout(30000),
    });
    const data = await res.json();
    return { ok: res.ok, data };
  } catch (e: any) {
    return { ok: false, error: e.message };
  }
}

// GET /api/drive/status — polling enabled flag + processed count
app.get('/api/drive/status', async (_, res) => {
  const result = await callProcessor('status');
  if (!result.ok) return res.status(502).json({ error: result.error ?? 'Upstream error' });
  res.json(result.data);
});

// POST /api/drive/enable — enable Drive polling
app.post('/api/drive/enable', async (_, res) => {
  const result = await callProcessor('enable');
  if (!result.ok) return res.status(502).json({ error: result.error ?? 'Upstream error' });
  res.json(result.data);
});

// POST /api/drive/disable — disable Drive polling
app.post('/api/drive/disable', async (_, res) => {
  const result = await callProcessor('disable');
  if (!result.ok) return res.status(502).json({ error: result.error ?? 'Upstream error' });
  res.json(result.data);
});

// POST /api/drive/run — trigger an immediate poll now
app.post('/api/drive/run', async (_, res) => {
  const result = await callProcessor('run');
  if (!result.ok) return res.status(502).json({ error: result.error ?? 'Upstream error' });
  res.json(result.data);
});

// ── MCP Integration Health Checks ────────────────────────────────────────

const MCP_INTEGRATIONS: { id: string; name: string; url: string }[] = [
  { id: 'gmail',    name: 'Gmail',            url: 'https://gmail.srv1694637.hstgr.cloud/sse' },
  { id: 'calendar', name: 'Google Calendar',  url: 'https://calendar.srv1694637.hstgr.cloud/mcp' },
  { id: 'sheets',   name: 'Google Sheets',    url: 'https://sheets.srv1694637.hstgr.cloud/sse' },
];

async function checkMcpHealth(url: string): Promise<boolean> {
  try {
    const res = await fetch(url, {
      headers: { Accept: 'text/event-stream' },
      signal: AbortSignal.timeout(5000),
    });
    // SSE endpoint returns 200 with event-stream content-type when healthy
    return res.ok || res.status === 200;
  } catch {
    return false;
  }
}

// GET /api/integrations — returns health status for all MCP integrations
app.get('/api/integrations', async (_, res) => {
  const results = await Promise.all(
    MCP_INTEGRATIONS.map(async (m) => ({
      id: m.id,
      name: m.name,
      online: await checkMcpHealth(m.url),
    }))
  );
  res.json({ integrations: results });
});

// Fallback: serve index.html for any non-API route (SPA catch-all)
app.get('/{*splat}', (_, res) => res.sendFile(path.join(frontendDir, 'index.html')));

const server = app.listen(config.port, '0.0.0.0', () => {
  console.log(`Dashboard server listening at http://0.0.0.0:${config.port}`);
  console.log(`Agents configured: ${config.agents.map(a => a.name).join(', ')}`);
});

// Handle shutdown gracefully
const shutdown = (signal: string) => {
  console.log(`${signal} signal received: closing HTTP server`);
  closeAllDbs();
  closeTasksDb();
  server.close(() => {
    console.log('HTTP server closed');
    process.exit(0);
  });
};

process.on('SIGTERM', () => shutdown('SIGTERM'));
process.on('SIGINT', () => shutdown('SIGINT'));
