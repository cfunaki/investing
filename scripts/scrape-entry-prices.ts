import { chromium } from 'playwright';
import { readFileSync } from 'fs';

const SESSION_PATH = 'data/sessions/bravos.json';

// Trade entries with dates from user
const trades = [
  { symbol: 'ALUM', date: '12/19/2025' },
  { symbol: 'EME', date: '01/15/2026' },
  { symbol: 'D', date: '02/05/2026' },
  { symbol: 'FHI', date: '02/13/2026' },
  { symbol: 'GE', date: '02/17/2026' },
  { symbol: 'DBC', date: '02/23/2026' },
  { symbol: 'HSY', date: '02/23/2026' },
  { symbol: 'NTR', date: '02/23/2026' },
  { symbol: 'AA', date: '02/24/2026' },
  { symbol: 'EXC', date: '02/27/2026' },
  { symbol: 'ANDE', date: '03/09/2026' },
];

async function scrapeEntryPrices() {
  const storageState = JSON.parse(readFileSync(SESSION_PATH, 'utf-8'));
  const browser = await chromium.launch({ headless: true });
  const context = await browser.newContext({ storageState: storageState as any });
  const page = await context.newPage();

  console.log('Scraping entry prices from Bravos /ideas/ page...\n');

  // Go to ideas page and search for each trade
  await page.goto('https://bravosresearch.com/ideas/', { waitUntil: 'networkidle' });

  const results: Array<{ symbol: string; date: string; entryPrice: string | null; link: string | null }> = [];

  // Get all idea links
  const ideaLinks = await page.$$eval('a[href*="/news-feed/"]', (links) =>
    links.map((a) => ({
      href: a.getAttribute('href') || '',
      text: a.textContent || '',
    }))
  );

  console.log(`Found ${ideaLinks.length} idea links\n`);

  for (const trade of trades) {
    // Find link matching this symbol
    const matchingLink = ideaLinks.find(
      (link) =>
        link.text.toLowerCase().includes(trade.symbol.toLowerCase()) &&
        link.text.toLowerCase().includes('long') &&
        !link.text.toLowerCase().includes('closing')
    );

    if (matchingLink) {
      try {
        await page.goto(matchingLink.href, { waitUntil: 'domcontentloaded', timeout: 15000 });

        // Get page content
        const content = await page.textContent('.entry-content, .post-content, article, main');

        // Look for entry price patterns
        let entryPrice: string | null = null;

        if (content) {
          // Try various patterns
          const patterns = [
            /entry(?:\s+price)?[:\s]+\$?([\d.]+)/i,
            /buy(?:ing)?\s+(?:at|@|price)[:\s]+\$?([\d.]+)/i,
            /\$?([\d.]+)\s+entry/i,
            /price[:\s]+\$?([\d.]+)/i,
            /at\s+\$?([\d.]+)\s+per\s+share/i,
            /current(?:ly)?\s+(?:trading|at)[:\s]+\$?([\d.]+)/i,
          ];

          for (const pattern of patterns) {
            const match = content.match(pattern);
            if (match) {
              entryPrice = match[1];
              break;
            }
          }
        }

        results.push({
          symbol: trade.symbol,
          date: trade.date,
          entryPrice,
          link: matchingLink.href,
        });

        console.log(
          `${trade.symbol.padEnd(6)} Entry: ${entryPrice ? '$' + entryPrice.padStart(8) : 'Not found'.padStart(9)}  (${trade.date})`
        );
      } catch (e) {
        console.log(`${trade.symbol.padEnd(6)} Error: Could not load page`);
        results.push({ symbol: trade.symbol, date: trade.date, entryPrice: null, link: matchingLink.href });
      }
    } else {
      console.log(`${trade.symbol.padEnd(6)} No matching link found`);
      results.push({ symbol: trade.symbol, date: trade.date, entryPrice: null, link: null });
    }
  }

  await browser.close();

  // Output JSON
  console.log('\n--- JSON Output ---');
  console.log(JSON.stringify(results, null, 2));
}

scrapeEntryPrices();
