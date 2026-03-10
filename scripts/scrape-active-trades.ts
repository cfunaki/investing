#!/usr/bin/env npx tsx
/**
 * Scrape Active Trade Ideas from Bravos Research
 *
 * This extracts the current portfolio positions with weights from /research/
 * Unlike /ideas/ which shows all historical trades, this shows only active positions.
 */

import { chromium } from 'playwright';
import { existsSync, readFileSync } from 'fs';
import { runActiveTradesExtraction, convertToAllocations } from '../src/scraping/active-trades-extractor.js';
import { saveStorageState } from '../src/auth/session-manager.js';

const SESSION_PATH = 'data/sessions/bravos.json';

async function main() {
  console.log('='.repeat(50));
  console.log('Bravos Research Active Trades Scraper');
  console.log('='.repeat(50));
  console.log('');

  // Load session
  if (!existsSync(SESSION_PATH)) {
    console.error('No session found. Run: npm run init-session');
    process.exit(1);
  }

  const storageState = JSON.parse(readFileSync(SESSION_PATH, 'utf-8'));

  const browser = await chromium.launch({ headless: true });
  const context = await browser.newContext({ storageState: storageState as any });
  const page = await context.newPage();

  try {
    // Extract active trades
    const result = await runActiveTradesExtraction(page);

    // Save session
    await saveStorageState(context);

    // Convert to allocations and display
    const allocations = convertToAllocations(result);

    console.log('\n' + '='.repeat(50));
    console.log('Target Allocations (for reconciliation)');
    console.log('='.repeat(50));

    for (const alloc of allocations) {
      const pctStr = `${(alloc.targetPct * 100).toFixed(1)}%`;
      console.log(`  ${alloc.symbol.padEnd(8)} ${pctStr.padStart(6)} ${alloc.side.toUpperCase()}`);
    }

    console.log('\n' + '='.repeat(50));
    console.log('Scraping Complete');
    console.log('='.repeat(50));
    console.log(`  Active positions: ${result.trades.length}`);
    console.log(`  Last updated: ${result.lastUpdated}`);
    console.log(`  Output: data/raw/active-trades-latest.json`);

  } catch (e) {
    console.error('Scraping failed:', e);
    process.exit(1);
  } finally {
    await browser.close();
  }
}

main();
