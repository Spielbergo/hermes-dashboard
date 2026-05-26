import express from 'express';
import cors from 'cors';
import path from 'path';
import { fileURLToPath } from 'url';
import { config } from './config.js';
import { getStateMeta, getRecentMessages, getSessions, closeAllDbs } from './memory.js';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const frontendDir = path.resolve(__dirname, '..', 'frontend');

const app = express();

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
  server.close(() => {
    console.log('HTTP server closed');
    process.exit(0);
  });
};

process.on('SIGTERM', () => shutdown('SIGTERM'));
process.on('SIGINT', () => shutdown('SIGINT'));
