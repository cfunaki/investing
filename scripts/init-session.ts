/**
 * Initialize Bravos Research session.
 * Opens browser for manual login and saves session state.
 *
 * Usage: npm run init-session
 */

import { initSession, closeSession } from '../src/auth/session-manager.js';

async function main() {
  console.log('='.repeat(50));
  console.log('Bravos Research Session Initialization');
  console.log('='.repeat(50));
  console.log('');

  try {
    const session = await initSession();

    console.log('');
    console.log('Session initialized successfully!');
    console.log('You can now run: npm run scrape');
    console.log('');

    // Keep browser open briefly so user can verify
    console.log('Closing browser in 3 seconds...');
    await session.page.waitForTimeout(3000);

    await closeSession(session);

  } catch (error) {
    console.error('Failed to initialize session:', error);
    process.exit(1);
  }
}

main();
