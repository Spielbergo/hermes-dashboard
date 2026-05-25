import express from 'express';
import cors from 'cors';
import path from 'path';
import { fileURLToPath } from 'url';
import { config } from './config.js';
import { getStateMeta, getRecentMessages, getSessions, closeDb } from './memory.js';

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

// API Endpoints
app.get('/api/memory', (_, res) => {
  try {
    res.json({ memory: getStateMeta() });
  } catch (error: any) {
    console.error('Error fetching state meta:', error);
    res.status(500).json({ error: `Failed to fetch state: ${error.message}` });
  }
});

app.get('/api/recent-messages/:sessionId', (req, res) => {
  try {
    const { sessionId } = req.params;
    const messages = getRecentMessages(sessionId, 100);
    res.json({ messages });
  } catch (error: any) {
    console.error('Error fetching recent messages:', error);
    res.status(500).json({ error: `Failed to fetch recent messages: ${error.message}` });
  }
});

app.get('/api/sessions', (_, res) => {
  try {
    const sessions = getSessions(50);
    res.json({ sessions });
  } catch (error: any) {
    console.error('Error fetching sessions:', error);
    res.status(500).json({ error: `Failed to fetch sessions: ${error.message}` });
  }
});

// Add more endpoints here for skills, todos, etc., as needed.

// Fallback: serve index.html for any non-API route (SPA catch-all)
app.get('/{*splat}', (_, res) => res.sendFile(path.join(frontendDir, 'index.html')));

const server = app.listen(config.port, '0.0.0.0', () => {
  console.log(`Dashboard server listening at http://0.0.0.0:${config.port}`);
});

// Handle shutdown gracefully
const shutdown = (signal: string) => {
  console.log(`${signal} signal received: closing HTTP server`);
  closeDb();
  server.close(() => {
    console.log('HTTP server closed');
    process.exit(0);
  });
};

process.on('SIGTERM', () => shutdown('SIGTERM'));
process.on('SIGINT', () => shutdown('SIGINT'));
