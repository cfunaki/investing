/**
 * Scrape trade ideas from Bravos Research.
 * Requires a valid session (run init-session first).
 *
 * Usage: npm run scrape
 */

import { getSession, closeSession, saveStorageState } from '../src/auth/session-manager.js';
import { navigateToIdeas, handleInfiniteScroll, goToNextPage } from '../src/scraping/bravos-navigator.js';
import { runFullExtraction, saveRawData } from '../src/scraping/ideas-extractor.js';
import { verifySession } from '../src/auth/login-detector.js';

async function main() {
  console.log('='.repeat(50));
  console.log('Bravos Research Ideas Scraper');
  console.log('='.repeat(50));
  console.log('');

  let session;

  try {
    // Get valid session
    session = await getSession();

    // Verify we're logged in
    console.log('Verifying session...');
    const isValid = await verifySession(session.page);

    if (!isValid) {
      console.error('Session is not valid. Please run: npm run init-session');
      await closeSession(session);
      process.exit(1);
    }

    // Navigate to ideas page
    console.log('Navigating to ideas page...');
    const navResult = await navigateToIdeas(session.page);

    if (!navResult.success) {
      console.error('Failed to navigate to ideas page:', navResult.error);
      await closeSession(session);
      process.exit(1);
    }

    // Capture screenshot and HTML for debugging
    await session.page.screenshot({ path: 'data/raw/debug-ideas-page.png', fullPage: true });
    const html = await session.page.content();
    const fs = await import('fs');
    fs.writeFileSync('data/raw/debug-ideas-page.html', html);
    console.log('Debug files saved to data/raw/debug-ideas-page.{png,html}');

    // Handle pagination/infinite scroll
    console.log('Loading all ideas...');

    // Try infinite scroll first
    const scrolledItems = await handleInfiniteScroll(session.page);
    if (scrolledItems > 0) {
      console.log(`Loaded ${scrolledItems} additional items via scroll`);
    }

    // Try pagination
    let pageCount = 1;
    while (await goToNextPage(session.page)) {
      pageCount++;
      console.log(`Loaded page ${pageCount}`);

      // Also scroll on new pages
      await handleInfiniteScroll(session.page);
    }

    // Run extraction
    console.log('');
    console.log('Starting extraction...');
    const result = await runFullExtraction(session.page);

    // Save raw data
    const filepath = saveRawData(result);

    // Update session (in case cookies changed)
    await saveStorageState(session.context);

    // Summary
    console.log('');
    console.log('='.repeat(50));
    console.log('Scraping Complete');
    console.log('='.repeat(50));
    console.log(`  Ideas extracted: ${result.ideas.length}`);
    console.log(`  Errors: ${result.errors.length}`);
    console.log(`  Output: ${filepath}`);
    console.log('');
    console.log('Next steps:');
    console.log('  1. make normalize  - Normalize the scraped data');
    console.log('  2. make reconcile  - Run reconciliation');
    console.log('');

  } catch (error) {
    console.error('Scraping failed:', error);

    if (session) {
      // Try to capture screenshot on error
      try {
        await session.page.screenshot({
          path: 'data/raw/error-scrape-failed.png',
          fullPage: true,
        });
        console.log('Error screenshot saved to data/raw/error-scrape-failed.png');
      } catch {
        // Ignore screenshot errors
      }
    }

    process.exit(1);

  } finally {
    if (session) {
      await closeSession(session);
    }
  }
}

main();
