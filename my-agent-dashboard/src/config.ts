import dotenv from 'dotenv';
import path from 'path';

// Load environment variables from .env file
dotenv.config({ path: path.resolve(process.cwd(), '.env') });

// Helper to get absolute path relative to the project root
const resolvePath = (p: string) => path.resolve(process.cwd(), p);

export const config = {
  llm: {
    provider: process.env.LLM_PROVIDER || 'gemini',
    model: process.env.LLM_MODEL || 'gemini-2.5-flash-lite',
  },
  telegram: {
    token: process.env.TELEGRAM_BOT_TOKEN || '',
    allowedUserIds: (process.env.ALLOWED_USER_IDS || '')
      .split(',')
      .map(s => s.trim())
      .filter(Boolean)
      .map(Number),
  },
  user: {
    name: process.env.USER_NAME || 'friend',
    timezone: process.env.USER_TIMEZONE || 'UTC',
  },
  dbPath: process.env.DB_PATH || resolvePath('../hermes-agent/data/memory.db'),
  pineconeKey: process.env.PINECONE_API_KEY,
  pineconeIndex: process.env.PINECONE_INDEX ?? 'my-agent',
  supabaseUrl: process.env.SUPABASE_URL,
  supabaseServiceRoleKey: process.env.SUPABASE_SERVICE_ROLE_KEY,
  dashboardToken: process.env.DASHBOARD_TOKEN!,
  port: parseInt(process.env.PORT || '5173', 10),
  // Only set in dev or if frontend is on a different origin. Leave unset on VPS (same-origin via nginx).
  corsOrigin: process.env.CORS_ORIGIN || '',
};

// Ensure the DB path is correctly resolved if it's relative to the dashboard's root
if (!path.isAbsolute(config.dbPath)) {
  config.dbPath = path.resolve(process.cwd(), config.dbPath);
}
