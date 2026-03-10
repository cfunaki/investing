import { Page } from 'playwright';
import { writeFileSync, mkdirSync, existsSync } from 'fs';

const RAW_DATA_DIR = 'data/raw';
const RESEARCH_URL = 'https://bravosresearch.com/research/';

export interface ActiveTrade {
  asset: string;
  symbol: string;
  action: 'Long' | 'Short';
  weight: number;
  category?: string;
}

export interface ActiveTradesResult {
  trades: ActiveTrade[];
  lastUpdated: string;
  scrapedAt: string;
  totalWeight: number;
}

/**
 * Extract ticker symbol from asset name
 * Examples:
 *   "EMCOR Group ($EME)" -> "EME"
 *   "Aluminum ($ALUM)" -> "ALUM"
 *   "Long on Nutrien (NTR)" -> "NTR"
 */
function extractSymbol(assetName: string): string | null {
  // Match ($SYMBOL) or (SYMBOL)
  const match = assetName.match(/\(\$?([A-Z0-9]+)\)/);
  return match ? match[1] : null;
}

/**
 * Extract active trades from the research page
 */
export async function extractActiveTrades(page: Page): Promise<ActiveTradesResult> {
  console.log('Navigating to research page...');
  await page.goto(RESEARCH_URL, { waitUntil: 'networkidle' });

  // Get last updated date
  const lastUpdatedEl = await page.$('.dash-newsletter .date');
  const lastUpdatedText = lastUpdatedEl
    ? await lastUpdatedEl.textContent()
    : '';
  const lastUpdated = lastUpdatedText?.replace('Last updated on:', '').trim() || '';

  console.log(`Active Trades last updated: ${lastUpdated}`);

  // Extract all trade rows
  const trades: ActiveTrade[] = [];
  let currentCategory = '';

  const rows = await page.$$('.asset-ratings .tbody > div');

  for (const row of rows) {
    const className = await row.getAttribute('class');

    // Check if this is a category header
    if (className?.includes('cat-name')) {
      const catEl = await row.$('span');
      currentCategory = catEl ? (await catEl.textContent())?.trim() || '' : '';
      continue;
    }

    // This is a trade row
    if (className?.includes('tr')) {
      const tds = await row.$$('.td');

      if (tds.length >= 3) {
        const assetText = (await tds[0].textContent())?.trim() || '';
        const actionText = (await tds[1].textContent())?.trim() || '';
        const weightText = (await tds[2].textContent())?.trim() || '';

        const symbol = extractSymbol(assetText);
        const weight = parseInt(weightText, 10);

        if (symbol && !isNaN(weight)) {
          trades.push({
            asset: assetText,
            symbol,
            action: actionText.toLowerCase().includes('short') ? 'Short' : 'Long',
            weight,
            category: currentCategory || undefined,
          });
        }
      }
    }
  }

  const totalWeight = trades.reduce((sum, t) => sum + t.weight, 0);

  console.log(`Extracted ${trades.length} active trades with total weight ${totalWeight}`);

  return {
    trades,
    lastUpdated,
    scrapedAt: new Date().toISOString(),
    totalWeight,
  };
}

/**
 * Convert weights (1-20 scale) to percentage allocations
 */
export function convertToAllocations(result: ActiveTradesResult): Array<{
  symbol: string;
  targetPct: number;
  weight: number;
  side: 'long' | 'short';
  asset: string;
}> {
  const { trades, totalWeight } = result;

  return trades.map(trade => ({
    symbol: trade.symbol,
    targetPct: totalWeight > 0 ? trade.weight / totalWeight : 0,
    weight: trade.weight,
    side: trade.action.toLowerCase() as 'long' | 'short',
    asset: trade.asset,
  }));
}

/**
 * Save active trades to disk
 */
export function saveActiveTrades(result: ActiveTradesResult): string {
  if (!existsSync(RAW_DATA_DIR)) {
    mkdirSync(RAW_DATA_DIR, { recursive: true });
  }

  const filename = `active-trades-${new Date().toISOString().replace(/[:.]/g, '-')}.json`;
  const filepath = `${RAW_DATA_DIR}/${filename}`;

  writeFileSync(filepath, JSON.stringify(result, null, 2));
  console.log(`Saved active trades to ${filepath}`);

  // Also save as latest
  const latestPath = `${RAW_DATA_DIR}/active-trades-latest.json`;
  writeFileSync(latestPath, JSON.stringify(result, null, 2));

  return filepath;
}

/**
 * Main extraction function
 */
export async function runActiveTradesExtraction(page: Page): Promise<ActiveTradesResult> {
  const result = await extractActiveTrades(page);
  saveActiveTrades(result);

  // Print summary
  console.log('\nActive Trades:');
  console.log('-'.repeat(60));
  console.log(`${'Symbol'.padEnd(8)} ${'Action'.padEnd(8)} ${'Weight'.padEnd(8)} ${'Alloc'.padEnd(8)} Category`);
  console.log('-'.repeat(60));

  const allocations = convertToAllocations(result);
  for (const alloc of allocations) {
    const pctStr = `${(alloc.targetPct * 100).toFixed(1)}%`;
    console.log(
      `${alloc.symbol.padEnd(8)} ${alloc.side.toUpperCase().padEnd(8)} ${String(alloc.weight).padEnd(8)} ${pctStr.padEnd(8)}`
    );
  }
  console.log('-'.repeat(60));
  console.log(`Total: ${result.trades.length} positions, weight sum: ${result.totalWeight}`);

  return result;
}
