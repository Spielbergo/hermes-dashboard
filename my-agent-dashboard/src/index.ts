import { config } from './config.js';
import './server.js'; // Import the server logic

async function main() {
  // Only start the bot if Telegram is configured
  if (config.telegram.token && config.telegram.allowedUserIds.length > 0) {
    console.log('Agent online via Telegram');
  } else {
    console.log('Agent online (Telegram bot not started - missing token or user IDs)');
  }
}

main().catch(console.error);
