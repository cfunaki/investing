#!/usr/bin/env npx tsx
/**
 * Scrape Entry and Target Prices from Bravos Ideas
 *
 * This clicks on each idea to expand it and extracts price data.
 * Focuses on current active trades to get entry/target prices for reconciliation.
 */

import { chromium, Page } from 'playwright';
import { existsSync, readFileSync, writeFileSync, mkdirSync } from 'fs';
import { saveStorageState } from '../src/auth/session-manager.js';

const SESSION_PATH = 'data/sessions/bravos.json';
const RAW_DATA_DIR = 'data/raw';
const OUTPUT_PATH = 'data/processed/idea_prices.json';

interface IdeaPrice {
  symbol: string;
  entryDate: string;
  entryPrice: number | null;
  targetPrice: number | null;
  stopLoss: number | null;
  postIds: string[];
  rawText: string;
  scrapedAt: string;
}

/**
 * Parse price from text using multiple patterns
 */
function parsePrice(text: string): number | null {
  // Remove commas and extra spaces
  const cleaned = text.replace(/,/g, '').trim();

  // Try to extract a number (with optional $ prefix)
  const match = cleaned.match(/\$?([\d.]+)/);
  if (match) {
    const value = parseFloat(match[1]);
    if (!isNaN(value) && value > 0) {
      return value;
    }
  }
  return null;
}

/**
 * Extract prices from expanded idea content
 */
function extractPricesFromText(text: string): {
  entryPrice: number | null;
  targetPrice: number | null;
  stopLoss: number | null;
} {
  const result = {
    entryPrice: null as number | null,
    targetPrice: null as number | null,
    stopLoss: null as number | null,
  };

  // Entry price patterns - ordered by specificity
  const entryPatterns = [
    // Bravos style: "This trade was entered on January 15, 2026, at $XXX"
    /trade\s+was\s+entered\s+on\s+\w+\s+\d+,?\s*\d*,?\s*at\s+\$?([\d,.]+)/i,
    // "entered on [date], at $XXX" - more flexible date matching
    /entered\s+on\s+[\w\s,]+at\s+\$?([\d,.]+)/i,
    // "entered at $XXX"
    /entered\s+at\s+\$?([\d,.]+)/i,
    // "entering at $XXX"
    /entering\s+(?:at\s+)?\$?([\d,.]+)/i,
    // "entry at $XXX" or "entry: $XXX"
    /entry\s*(?:price)?[:\s]+\$?([\d,.]+)/i,
    /entry\s+at\s+\$?([\d,.]+)/i,
    // "adding exposure at $XXX" or "increasing exposure at $XXX"
    /(?:adding|increasing)\s+(?:exposure\s+)?(?:at|to)\s+[^$]*\$?([\d,.]+)/i,
    // "buy at $XXX" or "buying at $XXX"
    /buy(?:ing)?\s+(?:at|@)\s*\$?([\d,.]+)/i,
  ];

  for (const pattern of entryPatterns) {
    const match = text.match(pattern);
    if (match) {
      const price = parsePrice(match[1]);
      // Sanity check: price should be reasonable (between $1 and $10000)
      if (price && price >= 1 && price <= 10000) {
        result.entryPrice = price;
        break;
      }
    }
  }

  // Target price patterns
  const targetPatterns = [
    // "price target of $XXX" or "price target: $XXX"
    /price\s+target\s*(?:of|:)?\s*\$?([\d,.]+)/i,
    // "target of $XXX" or "target: $XXX"
    /target\s*(?:of|:)?\s*\$?([\d,.]+)/i,
    // "targeting $XXX"
    /targeting\s+\$?([\d,.]+)/i,
    // "upside to $XXX"
    /upside\s+(?:to|of)\s+\$?([\d,.]+)/i,
    // "see $XXX" as target
    /see\s+(?:it\s+)?(?:reaching|going\s+to|at)?\s*\$?([\d,.]+)/i,
    // First target mentioned
    /first\s+target\s*(?:at|:)?\s*\$?([\d,.]+)/i,
  ];

  for (const pattern of targetPatterns) {
    const match = text.match(pattern);
    if (match) {
      const price = parsePrice(match[1]);
      if (price && price >= 1 && price <= 10000) {
        result.targetPrice = price;
        break;
      }
    }
  }

  // Stop loss patterns
  const stopPatterns = [
    // "stop at $XXX" or "stop: $XXX"
    /stop\s*(?:loss)?\s*(?:at|:)?\s*\$?([\d,.]+)/i,
    // "raising our stop from $XXX to $XXX" - get the second value
    /raising\s+(?:our\s+)?stop\s+(?:from\s+\$?[\d,.]+\s+)?to\s+\$?([\d,.]+)/i,
    // "stop from $XXX" - first value
    /stop\s+(?:from\s+)?\$?([\d,.]+)/i,
    // "exit at $XXX"
    /exit\s+(?:at|below)\s+\$?([\d,.]+)/i,
    // "cut at $XXX"
    /cut\s+(?:at|below)\s+\$?([\d,.]+)/i,
    // "below $XXX"
    /below\s+\$?([\d,.]+)/i,
  ];

  for (const pattern of stopPatterns) {
    const match = text.match(pattern);
    if (match) {
      const price = parsePrice(match[1]);
      if (price && price >= 1 && price <= 10000) {
        result.stopLoss = price;
        break;
      }
    }
  }

  return result;
}

/**
 * Click on an idea row to expand, then navigate to the detail page
 */
async function extractIdeaContent(
  page: Page,
  postIds: string,
  symbol: string,
  date: string
): Promise<IdeaPrice | null> {
  try {
    // Make sure we're on the ideas page
    if (!page.url().includes('/ideas/')) {
      await page.goto('https://bravosresearch.com/ideas/', { waitUntil: 'networkidle' });
      await page.waitForSelector('.idea_posts', { timeout: 10000 });
    }

    // Find the clickable element with matching data-posts (fresh lookup)
    const selector = `.idea-click[data-posts="${postIds}"]`;
    const element = await page.$(selector);

    if (!element) {
      console.log(`  [${symbol}] Could not find element with data-posts="${postIds}"`);
      return null;
    }

    // Click to expand
    await element.click();

    // Wait for content to load
    await page.waitForTimeout(1000);

    // Find the "Read more" link in the expanded content
    // Try multiple selectors for the active/expanded row
    const linkSelectors = [
      `.idea_posts.active a[href*="/news-feed/"]`,
      `.idea_posts.active a:has-text("Read more")`,
      `.idea_posts.active .idea-posts-list a`,
      `a[href*="/news-feed/"]`,
    ];

    let href: string | null = null;
    for (const linkSel of linkSelectors) {
      const readMoreLink = await page.$(linkSel);
      if (readMoreLink) {
        href = await readMoreLink.getAttribute('href');
        if (href && href.includes('/news-feed/')) break;
      }
    }

    if (!href) {
      console.log(`  [${symbol}] No detail page link found`);
      // Try clicking again to collapse
      try { await element.click(); } catch {}
      return null;
    }

    // Navigate to the detail page
    await page.goto(href, { waitUntil: 'domcontentloaded', timeout: 15000 });

    // Wait for content to load
    await page.waitForTimeout(800);

    // Extract text from the page content
    const contentSelectors = [
      '.entry-content',
      '.post-content',
      'article .content',
      'article',
      'main',
    ];

    let rawText = '';
    for (const sel of contentSelectors) {
      const contentEl = await page.$(sel);
      if (contentEl) {
        rawText = await contentEl.textContent() || '';
        if (rawText.length > 100) break;
      }
    }

    // If still no content, get body text
    if (rawText.length < 100) {
      rawText = await page.textContent('body') || '';
    }

    // Extract prices from the text
    const prices = extractPricesFromText(rawText);

    // Navigate back to ideas page for next iteration
    await page.goto('https://bravosresearch.com/ideas/', { waitUntil: 'networkidle' });
    await page.waitForSelector('.idea_posts', { timeout: 10000 });

    return {
      symbol,
      entryDate: date,
      entryPrice: prices.entryPrice,
      targetPrice: prices.targetPrice,
      stopLoss: prices.stopLoss,
      postIds: postIds.split(','),
      rawText: rawText.substring(0, 1500), // Store more text for debugging
      scrapedAt: new Date().toISOString(),
    };
  } catch (e) {
    console.error(`  [${symbol}] Error extracting content:`, e);
    // Try to navigate back to ideas page
    try {
      await page.goto('https://bravosresearch.com/ideas/', { waitUntil: 'networkidle' });
      await page.waitForSelector('.idea_posts', { timeout: 10000 });
    } catch {
      // Ignore navigation errors
    }
    return null;
  }
}

async function main() {
  console.log('='.repeat(50));
  console.log('Bravos Research Idea Prices Scraper');
  console.log('='.repeat(50));
  console.log('');

  // Load session
  if (!existsSync(SESSION_PATH)) {
    console.error('No session found. Run: npm run init-session');
    process.exit(1);
  }

  // Load active trades to know which symbols to scrape
  const activeTradesPath = 'data/raw/active-trades-latest.json';
  if (!existsSync(activeTradesPath)) {
    console.error('No active trades found. Run: npm run scrape-active first');
    process.exit(1);
  }

  const activeTrades = JSON.parse(readFileSync(activeTradesPath, 'utf-8'));
  const targetSymbols = new Set(activeTrades.trades.map((t: any) => t.symbol));
  console.log(`Targeting ${targetSymbols.size} active trade symbols: ${[...targetSymbols].join(', ')}\n`);

  const storageState = JSON.parse(readFileSync(SESSION_PATH, 'utf-8'));

  const browser = await chromium.launch({ headless: true });
  const context = await browser.newContext({ storageState: storageState as any });
  const page = await context.newPage();

  const results: IdeaPrice[] = [];

  try {
    // Navigate to ideas page
    console.log('Navigating to ideas page...');
    await page.goto('https://bravosresearch.com/ideas/', { waitUntil: 'networkidle' });

    // Wait for ideas to load
    await page.waitForSelector('.idea_posts', { timeout: 10000 });

    // First pass: collect all idea info without processing
    interface IdeaInfo {
      symbol: string;
      date: string;
      dataPosts: string;
      summaryText: string;
    }

    const ideaInfoList: IdeaInfo[] = [];

    const ideaRows = await page.$$('.idea_posts');
    console.log(`Found ${ideaRows.length} idea rows`);

    for (const row of ideaRows) {
      const clickable = await row.$('.idea-click');
      if (!clickable) continue;

      const summaryText = await clickable.textContent() || '';
      const dataPosts = await clickable.getAttribute('data-posts') || '';

      const symbolMatch = summaryText.match(/\(\$?([A-Z0-9]+)\)/);
      const dateMatch = summaryText.match(/\[(\d{2}\/\d{2}\/\d{4})\]/);

      if (!symbolMatch) continue;

      const symbol = symbolMatch[1];
      const date = dateMatch ? dateMatch[1] : '';

      if (!targetSymbols.has(symbol)) continue;

      if (summaryText.toLowerCase().includes('closing') ||
          summaryText.toLowerCase().includes('short')) {
        continue;
      }

      ideaInfoList.push({ symbol, date, dataPosts, summaryText });
    }

    console.log(`Found ${ideaInfoList.length} ideas for active trade symbols\n`);

    // Second pass: process each idea with fresh page state
    for (const info of ideaInfoList) {
      console.log(`Processing ${info.symbol} (${info.date})...`);

      const priceData = await extractIdeaContent(page, info.dataPosts, info.symbol, info.date);

      if (priceData) {
        results.push(priceData);
        console.log(`  Entry: ${priceData.entryPrice || 'N/A'}, Target: ${priceData.targetPrice || 'N/A'}, Stop: ${priceData.stopLoss || 'N/A'}`);
      }

      // Small delay between requests
      await page.waitForTimeout(300);
    }

    // Save session
    await saveStorageState(context);

    // Save results
    if (!existsSync('data/processed')) {
      mkdirSync('data/processed', { recursive: true });
    }

    writeFileSync(OUTPUT_PATH, JSON.stringify(results, null, 2));
    console.log(`\nSaved ${results.length} idea prices to ${OUTPUT_PATH}`);

    // Summary
    console.log('\n' + '='.repeat(50));
    console.log('Summary');
    console.log('='.repeat(50));

    const withEntry = results.filter(r => r.entryPrice);
    const withTarget = results.filter(r => r.targetPrice);

    console.log(`  Total processed: ${results.length}`);
    console.log(`  With entry price: ${withEntry.length}`);
    console.log(`  With target price: ${withTarget.length}`);

    console.log('\nResults:');
    console.log('-'.repeat(50));
    console.log(`${'Symbol'.padEnd(8)} ${'Date'.padEnd(12)} ${'Entry'.padStart(10)} ${'Target'.padStart(10)}`);
    console.log('-'.repeat(50));

    for (const r of results) {
      const entry = r.entryPrice ? `$${r.entryPrice.toFixed(2)}` : 'N/A';
      const target = r.targetPrice ? `$${r.targetPrice.toFixed(2)}` : 'N/A';
      console.log(`${r.symbol.padEnd(8)} ${r.entryDate.padEnd(12)} ${entry.padStart(10)} ${target.padStart(10)}`);
    }

  } catch (e) {
    console.error('Scraping failed:', e);

    // Save screenshot on error
    await page.screenshot({ path: `${RAW_DATA_DIR}/error-idea-prices.png`, fullPage: true });
    console.log('Error screenshot saved');

    process.exit(1);
  } finally {
    await browser.close();
  }
}

main();
