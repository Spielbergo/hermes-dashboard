import dotenv from 'dotenv';
import path from 'path';

// Load environment variables from .env file
dotenv.config({ path: path.resolve(process.cwd(), '.env') });

export interface AgentConfig {
  id: string;
  name: string;
  dbPath: string;
}

/**
 * Parse AGENTS env var. Format: semicolon-separated entries of "id:Display Name:/path/to/state.db"
 * Example: AGENTS=cli:Hermes CLI:/data/cli/state.db;webui:Hermes WebUI:/data/webui/state.db
 * Falls back to DB_PATH for single-agent setups.
 */
function parseAgents(): AgentConfig[] {
  const raw = process.env.AGENTS || '';
  if (raw) {
    const agents = raw.split(';').flatMap(entry => {
      const parts = entry.trim().split(':');
      if (parts.length < 3) return [];
      const id = parts[0].trim();
      const name = parts[1].trim();
      const dbPath = parts.slice(2).join(':').trim(); // rejoin — paths can contain colons
      return id && name && dbPath ? [{ id, name, dbPath }] : [];
    });
    if (agents.length) return agents;
  }
  // Backward compat: single DB_PATH
  const dbPath = process.env.DB_PATH || '/data/state.db';
  return [{ id: 'default', name: 'Hermes', dbPath }];
}

export const config = {
  agents: parseAgents(),
  dashboardToken: process.env.DASHBOARD_TOKEN!,
  port: parseInt(process.env.PORT || '5173', 10),
  corsOrigin: process.env.CORS_ORIGIN || '',
  transcriptProcessorUrl: process.env.TRANSCRIPT_PROCESSOR_URL || '',
  transcriptProcessorSecret: process.env.TRANSCRIPT_PROCESSOR_SECRET || '',
};
