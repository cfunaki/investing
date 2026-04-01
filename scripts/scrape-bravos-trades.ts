#!/usr/bin/env npx tsx
/**
 * Comprehensive Bravos Trade Scraper
 *
 * Scrapes ALL posts for each trade to build complete trade records with:
 * - Entry price (from original post)
 * - Target price (from original post or first mention)
 * - Current stop (from most recent post)
 * - Weight history (track changes over time)
 * - All trade actions (entries, adds, trims, exits)
 */

import { chromium, Page } from 'playwright';
import { existsSync, readFileSync, writeFileSync, mkdirSync } from 'fs';
import { saveStorageState } from '../src/auth/session-manager.js';

const SESSION_PATH = 'data/sessions/bravos.json';
const OUTPUT_PATH = 'data/processed/bravos_trades.json';

// Types
interface TradePost {
  postId: string;
  date: string;
  action: 'enter' | 'add' | 'trim' | 'exit' | 'update' | 'unknown';
  price: number | null;
  weight: number | null;
  weightChange: { from: number; to: number } | null;
  stop: number | null;
  target: number | null;
  partialProfitPrice: number | null;
  rawText: string;
  url: string;
}

interface TradeRecord {
  symbol: string;
  companyName: string;
  status: 'active' | 'closed';
  side: 'long' | 'short';
  entryDate: string | null;
  entryPrice: number | null;
  targetPrice: number | null;
  currentStop: number | null;
  currentWeight: number | null;
  posts: TradePost[];
  scrapedAt: string;
}

interface BravosTradesData {
  trades: Record<string, TradeRecord>;
  lastUpdated: string;
  source: string;
}

/**
 * Parse a date string in various formats to YYYY-MM-DD
 */
function parseDate(dateStr: string): string | null {
  // Try MM/DD/YYYY
  const slashMatch = dateStr.match(/(\d{1,2})\/(\d{1,2})\/(\d{4})/);
  if (slashMatch) {
    const [, month, day, year] = slashMatch;
    return `${year}-${month.padStart(2, '0')}-${day.padStart(2, '0')}`;
  }

  // Try "Month DD, YYYY" or "Month DD YYYY"
  const monthNames = ['january', 'february', 'march', 'april', 'may', 'june',
    'july', 'august', 'september', 'october', 'november', 'december'];
  const wordMatch = dateStr.toLowerCase().match(/(\w+)\s+(\d{1,2}),?\s*(\d{4})/);
  if (wordMatch) {
    const [, monthName, day, year] = wordMatch;
    const monthIndex = monthNames.indexOf(monthName.toLowerCase());
    if (monthIndex !== -1) {
      return `${year}-${String(monthIndex + 1).padStart(2, '0')}-${day.padStart(2, '0')}`;
    }
  }

  return null;
}

/**
 * Parse price from text
 */
function parsePrice(text: string): number | null {
  const match = text.replace(/,/g, '').match(/\$?([\d.]+)/);
  if (match) {
    const value = parseFloat(match[1]);
    if (!isNaN(value) && value > 0 && value < 100000) {
      return value;
    }
  }
  return null;
}

/**
 * Parse structured data from a trade post
 */
function parseTradePost(text: string, url: string, postId: string): TradePost {
  const post: TradePost = {
    postId,
    date: '',
    action: 'unknown',
    price: null,
    weight: null,
    weightChange: null,
    stop: null,
    target: null,
    partialProfitPrice: null,
    rawText: text.substring(0, 2000),
    url,
  };

  // Extract date from header (usually at the top)
  const datePatterns = [
    /(\d{2}\/\d{2}\/\d{4})/,
    /(\w+\s+\d{1,2},?\s*\d{4})/,
  ];
  for (const pattern of datePatterns) {
    const match = text.match(pattern);
    if (match) {
      const parsed = parseDate(match[1]);
      if (parsed) {
        post.date = parsed;
        break;
      }
    }
  }

  // Determine action type
  const lowerText = text.toLowerCase();
  if (lowerText.includes('entering') || lowerText.includes('initiating') ||
      (lowerText.includes('adding') && lowerText.includes('new position'))) {
    post.action = 'enter';
  } else if (lowerText.includes('adding to') || lowerText.includes('increasing exposure') ||
             lowerText.includes('increasing our exposure')) {
    post.action = 'add';
  } else if (lowerText.includes('booking partial') || lowerText.includes('reducing') ||
             lowerText.includes('trimming') || lowerText.includes('taking profits')) {
    post.action = 'trim';
  } else if (lowerText.includes('closing') || lowerText.includes('exiting') ||
             lowerText.includes('stopped out')) {
    post.action = 'exit';
  } else if (lowerText.includes('update') || lowerText.includes('raising our stop') ||
             lowerText.includes('adjusting')) {
    post.action = 'update';
  }

  // Extract current action price (what they're buying/selling at now)
  const actionPricePatterns = [
    /(?:at|@)\s*\$?([\d,.]+)(?:\s+and|\s+,|\s*$)/i,
    /(?:adding|entering|buying|selling|trimming|reducing).*?(?:at|@)\s*\$?([\d,.]+)/i,
  ];
  for (const pattern of actionPricePatterns) {
    const match = text.match(pattern);
    if (match) {
      post.price = parsePrice(match[1]);
      if (post.price) break;
    }
  }

  // Extract weight change (e.g., "from 4 to 6" or "from 5 to 4")
  const weightChangeMatch = text.match(/(?:weight|allocation|exposure)\s*(?:from\s+)?(\d+)\s*to\s*(\d+)/i);
  if (weightChangeMatch) {
    post.weightChange = {
      from: parseInt(weightChangeMatch[1]),
      to: parseInt(weightChangeMatch[2]),
    };
    post.weight = post.weightChange.to;
  }

  // Extract standalone weight
  if (!post.weight) {
    const weightMatch = text.match(/weight\s*(?:of|:)?\s*(\d+)/i);
    if (weightMatch) {
      post.weight = parseInt(weightMatch[1]);
    }
  }

  // Extract stop loss
  const stopPatterns = [
    /(?:raising|setting|moving)?\s*(?:our\s+)?stop\s*(?:loss)?\s*(?:to|at|:)?\s*\$?([\d,.]+)/i,
    /stop\s*(?:loss)?\s*(?:is|at|:)\s*\$?([\d,.]+)/i,
    /stop\s+(?:from\s+\$?[\d,.]+\s+)?to\s+\$?([\d,.]+)/i,
  ];
  for (const pattern of stopPatterns) {
    const match = text.match(pattern);
    if (match) {
      post.stop = parsePrice(match[1]);
      if (post.stop) break;
    }
  }

  // Extract target price
  const targetPatterns = [
    /(?:price\s+)?target\s*(?:of|at|:)?\s*\$?([\d,.]+)/i,
    /target(?:ing)?\s+\$?([\d,.]+)/i,
    /upside\s+(?:to|of)\s+\$?([\d,.]+)/i,
    /first\s+target\s*(?:at|:)?\s*\$?([\d,.]+)/i,
  ];
  for (const pattern of targetPatterns) {
    const match = text.match(pattern);
    if (match) {
      post.target = parsePrice(match[1]);
      if (post.target) break;
    }
  }

  // Extract entry price from historical reference (e.g., "This trade was entered on Feb 05, 2026, at $62.54")
  // This is crucial for update posts that reference the original entry
  const entryRefPatterns = [
    /(?:trade|position)\s+was\s+entered\s+(?:on\s+)?[\w\s,]+at\s+\$?([\d,.]+)/i,
    /entered\s+(?:on\s+)?[\w\s,]+at\s+\$?([\d,.]+)/i,
    /entry\s*(?:price)?[:\s]+\$?([\d,.]+)/i,
  ];

  // Store referenced entry price separately (useful even for non-entry posts)
  let referencedEntryPrice: number | null = null;
  for (const pattern of entryRefPatterns) {
    const match = text.match(pattern);
    if (match) {
      referencedEntryPrice = parsePrice(match[1]);
      if (referencedEntryPrice) break;
    }
  }

  // If this is an entry action and we don't have a price, use referenced entry
  if (post.action === 'enter' && !post.price && referencedEntryPrice) {
    post.price = referencedEntryPrice;
  }

  // Store referenced entry price for aggregation use
  (post as any).referencedEntryPrice = referencedEntryPrice;

  // Extract partial profit price
  const partialProfitMatch = text.match(/(?:booked|booking)\s+(?:partial\s+)?profits?\s+(?:on\s+)?[\w\s,]*at\s+\$?([\d,.]+)/i);
  if (partialProfitMatch) {
    post.partialProfitPrice = parsePrice(partialProfitMatch[1]);
  }

  return post;
}

/**
 * Find the most recent trade cycle (from most recent 'enter' to present)
 * This avoids mixing old closed trades with current active ones
 */
function findCurrentTradeCycle(posts: TradePost[]): TradePost[] {
  // Sort posts by date (newest first)
  const sortedByDateDesc = [...posts].sort((a, b) => {
    if (!a.date) return 1;
    if (!b.date) return -1;
    return b.date.localeCompare(a.date);  // Descending
  });

  // Find the most recent 'enter' action
  let mostRecentEntryIndex = -1;
  for (let i = 0; i < sortedByDateDesc.length; i++) {
    if (sortedByDateDesc[i].action === 'enter') {
      mostRecentEntryIndex = i;
      break;
    }
  }

  if (mostRecentEntryIndex === -1) {
    // No entry found, return all posts
    return posts;
  }

  // Get the entry date
  const entryDate = sortedByDateDesc[mostRecentEntryIndex].date;

  // Return all posts from entry date onwards (including entry)
  // Re-sort ascending for proper processing
  const currentCyclePosts = sortedByDateDesc
    .slice(0, mostRecentEntryIndex + 1)
    .sort((a, b) => {
      if (!a.date) return 1;
      if (!b.date) return -1;
      return a.date.localeCompare(b.date);
    });

  return currentCyclePosts;
}

/**
 * Aggregate posts into a complete trade record
 */
function aggregateTradeRecord(
  symbol: string,
  companyName: string,
  side: 'long' | 'short',
  posts: TradePost[]
): TradeRecord {
  // Filter to only the current trade cycle
  const currentCyclePosts = findCurrentTradeCycle(posts);

  // Sort posts by date (oldest first)
  const sortedPosts = [...currentCyclePosts].sort((a, b) => {
    if (!a.date) return 1;
    if (!b.date) return -1;
    return a.date.localeCompare(b.date);
  });

  const record: TradeRecord = {
    symbol,
    companyName,
    status: 'active',
    side,
    entryDate: null,
    entryPrice: null,
    targetPrice: null,
    currentStop: null,
    currentWeight: null,
    posts: sortedPosts,
    scrapedAt: new Date().toISOString(),
  };

  // Entry date and price from earliest post in current cycle
  // Also check for referenced entry prices in update posts
  for (const post of sortedPosts) {
    // Check for referenced entry price (e.g., "This trade was entered... at $X")
    const referencedEntry = (post as any).referencedEntryPrice;
    if (referencedEntry && !record.entryPrice) {
      record.entryPrice = referencedEntry;
    }

    // Entry action with price
    if (post.action === 'enter' && post.price && !record.entryPrice) {
      record.entryDate = post.date;
      record.entryPrice = post.price;
    }

    // First target mentioned
    if (post.target && !record.targetPrice) {
      record.targetPrice = post.target;
    }
  }

  // If still no entry date, use earliest post date
  if (!record.entryDate && sortedPosts.length > 0) {
    record.entryDate = sortedPosts[0].date;
  }

  // Current stop and weight from most recent post
  for (const post of [...sortedPosts].reverse()) {
    if (post.stop && !record.currentStop) {
      record.currentStop = post.stop;
    }
    if (post.weight && !record.currentWeight) {
      record.currentWeight = post.weight;
    }
    if (record.currentStop && record.currentWeight) break;
  }

  // Check if trade is closed
  const lastPost = sortedPosts[sortedPosts.length - 1];
  if (lastPost?.action === 'exit') {
    record.status = 'closed';
  }

  return record;
}

/**
 * Extract all post URLs from an expanded idea row
 */
async function getPostUrls(page: Page): Promise<string[]> {
  const urls: string[] = [];

  // Find all "Read more" links in the active/expanded idea
  const links = await page.$$('.idea_posts.active a[href*="/news-feed/"], .idea_posts.active a:has-text("Read more")');

  for (const link of links) {
    const href = await link.getAttribute('href');
    if (href && href.includes('/news-feed/') && !urls.includes(href)) {
      urls.push(href);
    }
  }

  return urls;
}

/**
 * Scrape a single post page
 */
async function scrapePost(page: Page, url: string, postId: string): Promise<TradePost | null> {
  try {
    await page.goto(url, { waitUntil: 'domcontentloaded', timeout: 15000 });
    await page.waitForTimeout(800);

    // Extract content
    const contentSelectors = ['.entry-content', '.post-content', 'article', 'main'];
    let text = '';

    for (const sel of contentSelectors) {
      const el = await page.$(sel);
      if (el) {
        text = await el.textContent() || '';
        if (text.length > 100) break;
      }
    }

    if (text.length < 50) {
      text = await page.textContent('body') || '';
    }

    return parseTradePost(text, url, postId);
  } catch (e) {
    console.error(`    Error scraping ${url}:`, e);
    return null;
  }
}

async function main() {
  console.log('='.repeat(60));
  console.log('Comprehensive Bravos Trade Scraper');
  console.log('='.repeat(60));
  console.log('');

  if (!existsSync(SESSION_PATH)) {
    console.error('No session found. Run: npm run init-session');
    process.exit(1);
  }

  // Load active trades
  const activeTradesPath = 'data/raw/active-trades-latest.json';
  if (!existsSync(activeTradesPath)) {
    console.error('No active trades found. Run: npm run scrape-active first');
    process.exit(1);
  }

  const activeTrades = JSON.parse(readFileSync(activeTradesPath, 'utf-8'));
  const targetSymbols = new Map<string, { weight: number; side: 'long' | 'short'; name: string }>();

  for (const trade of activeTrades.trades) {
    targetSymbols.set(trade.symbol, {
      weight: trade.weight,
      side: trade.action.toLowerCase() as 'long' | 'short',
      name: trade.asset,
    });
  }

  console.log(`Targeting ${targetSymbols.size} active positions\n`);

  const storageState = JSON.parse(readFileSync(SESSION_PATH, 'utf-8'));
  const browser = await chromium.launch({ headless: true });
  const context = await browser.newContext({ storageState: storageState as any });
  const page = await context.newPage();

  const tradesData: BravosTradesData = {
    trades: {},
    lastUpdated: new Date().toISOString(),
    source: 'bravosresearch.com',
  };

  try {
    // Navigate to ideas page
    console.log('Navigating to ideas page...');
    await page.goto('https://bravosresearch.com/ideas/', { waitUntil: 'networkidle' });
    await page.waitForSelector('.idea_posts', { timeout: 10000 });

    // Collect idea info
    interface IdeaInfo {
      symbol: string;
      companyName: string;
      date: string;
      dataPosts: string;
      side: 'long' | 'short';
    }

    const ideaInfoList: IdeaInfo[] = [];
    const ideaRows = await page.$$('.idea_posts');

    for (const row of ideaRows) {
      const clickable = await row.$('.idea-click');
      if (!clickable) continue;

      const summaryText = await clickable.textContent() || '';
      const dataPosts = await clickable.getAttribute('data-posts') || '';

      const symbolMatch = summaryText.match(/\(\$?([A-Z0-9]+)\)/);
      const dateMatch = summaryText.match(/\[(\d{2}\/\d{2}\/\d{4})\]/);

      if (!symbolMatch) continue;

      const symbol = symbolMatch[1];
      if (!targetSymbols.has(symbol)) continue;

      // Skip closing/short entries for long positions
      const isClosing = summaryText.toLowerCase().includes('closing');
      const isShort = summaryText.toLowerCase().includes('short on');
      const targetInfo = targetSymbols.get(symbol)!;

      if (isClosing) continue;
      if (targetInfo.side === 'long' && isShort) continue;

      // Extract company name
      const nameMatch = summaryText.match(/on\s+(.+?)\s+\(\$/);
      const companyName = nameMatch ? nameMatch[1].trim() : targetInfo.name;

      ideaInfoList.push({
        symbol,
        companyName,
        date: dateMatch ? dateMatch[1] : '',
        dataPosts,
        side: targetInfo.side,
      });
    }

    console.log(`Found ${ideaInfoList.length} relevant ideas\n`);

    // Group by symbol (multiple ideas may exist for same symbol)
    const symbolIdeas = new Map<string, IdeaInfo[]>();
    for (const idea of ideaInfoList) {
      const existing = symbolIdeas.get(idea.symbol) || [];
      existing.push(idea);
      symbolIdeas.set(idea.symbol, existing);
    }

    // Process each symbol
    for (const [symbol, ideas] of symbolIdeas) {
      console.log(`\n${'─'.repeat(50)}`);
      console.log(`Processing ${symbol} (${ideas.length} idea groups)`);

      const allPosts: TradePost[] = [];

      for (const idea of ideas) {
        // Make sure we're on ideas page
        if (!page.url().includes('/ideas/')) {
          await page.goto('https://bravosresearch.com/ideas/', { waitUntil: 'networkidle' });
          await page.waitForSelector('.idea_posts', { timeout: 10000 });
        }

        // Find and click the idea
        const selector = `.idea-click[data-posts="${idea.dataPosts}"]`;
        const element = await page.$(selector);
        if (!element) continue;

        await element.click();
        await page.waitForTimeout(1000);

        // Get all post URLs
        const postUrls = await getPostUrls(page);
        console.log(`  Found ${postUrls.length} posts for ${idea.date}`);

        // Scrape each post
        for (const url of postUrls) {
          const postId = url.match(/\/news-feed\/(\d+)/)?.[1] || '';
          console.log(`    Scraping post ${postId}...`);

          const post = await scrapePost(page, url, postId);
          if (post) {
            allPosts.push(post);
            console.log(`      Action: ${post.action}, Price: ${post.price || 'N/A'}, Stop: ${post.stop || 'N/A'}, Target: ${post.target || 'N/A'}`);
          }

          await page.waitForTimeout(300);
        }
      }

      // Aggregate into trade record
      if (allPosts.length > 0) {
        const targetInfo = targetSymbols.get(symbol)!;
        const record = aggregateTradeRecord(
          symbol,
          ideas[0].companyName,
          targetInfo.side,
          allPosts
        );
        // Set current weight from active trades if not found in posts
        if (!record.currentWeight) {
          record.currentWeight = targetInfo.weight;
        }
        tradesData.trades[symbol] = record;

        console.log(`  → Entry: $${record.entryPrice || 'N/A'}, Target: $${record.targetPrice || 'N/A'}, Stop: $${record.currentStop || 'N/A'}, Weight: ${record.currentWeight}`);
      }
    }

    // Save results
    await saveStorageState(context);

    if (!existsSync('data/processed')) {
      mkdirSync('data/processed', { recursive: true });
    }

    writeFileSync(OUTPUT_PATH, JSON.stringify(tradesData, null, 2));
    console.log(`\n${'='.repeat(60)}`);
    console.log(`Saved ${Object.keys(tradesData.trades).length} trade records to ${OUTPUT_PATH}`);

    // Summary
    console.log('\n' + '='.repeat(60));
    console.log('SUMMARY');
    console.log('='.repeat(60));
    console.log('Symbol'.padEnd(8) + 'Entry'.padStart(10) + 'Target'.padStart(10) + 'Stop'.padStart(10) + 'Weight'.padStart(8) + 'Posts'.padStart(6));
    console.log('-'.repeat(60));

    for (const [symbol, record] of Object.entries(tradesData.trades)) {
      const entry = record.entryPrice ? `$${record.entryPrice.toFixed(2)}` : 'N/A';
      const target = record.targetPrice ? `$${record.targetPrice.toFixed(2)}` : 'N/A';
      const stop = record.currentStop ? `$${record.currentStop.toFixed(2)}` : 'N/A';
      const weight = record.currentWeight?.toString() || 'N/A';
      console.log(symbol.padEnd(8) + entry.padStart(10) + target.padStart(10) + stop.padStart(10) + weight.padStart(8) + record.posts.length.toString().padStart(6));
    }

  } catch (e) {
    console.error('Scraping failed:', e);
    await page.screenshot({ path: 'data/raw/error-bravos-trades.png', fullPage: true });
    process.exit(1);
  } finally {
    await browser.close();
  }
}

main();
