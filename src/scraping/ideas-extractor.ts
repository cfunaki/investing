import { Page } from 'playwright';
import { writeFileSync, mkdirSync, existsSync } from 'fs';
import { readFileSync } from 'fs';
import { navigateToIdeaDetail } from './bravos-navigator.js';

// Load selectors from config
const selectors = JSON.parse(
  readFileSync(new URL('./selectors.json', import.meta.url), 'utf-8')
);

const RAW_DATA_DIR = 'data/raw';
const ERROR_SCREENSHOT_DIR = 'data/raw';

export interface IdeaSummary {
  rowId: string;
  postIds: string[];
  summaryText: string;
  direction?: 'Long' | 'Short';
  companyName?: string;
  symbol?: string;
  date?: string;
}

export interface IdeaDetail {
  ideaId: string;
  sourceUrl: string;
  scrapedAt: string;
  title?: string;
  symbol?: string;
  side?: 'buy' | 'sell' | 'unknown';
  relativeWeight?: string;
  entryPrice?: string;
  targetPrice?: string;
  stopLoss?: string;
  status?: 'open' | 'closed' | 'unknown';
  date?: string;
  notes?: string;
  rawHtml?: string;
}

export interface ExtractionResult {
  ideas: IdeaDetail[];
  errors: Array<{ url: string; error: string }>;
  timestamp: string;
}

/**
 * Parse summary text to extract structured data
 * Format: "Long on Company Name ($TICKER) [MM/DD/YYYY]"
 * or: "Long on Company Name ($TICKER) - [MM/DD/YYYY]"
 */
function parseSummaryText(text: string): { direction?: 'Long' | 'Short'; companyName?: string; symbol?: string; date?: string } {
  const pattern = /^(Long|Short)\s+on\s+(.+?)\s+\(\$(\w+)\)\s*(?:-\s*)?\[(\d{2}\/\d{2}\/\d{4})\]$/;
  const match = text.trim().match(pattern);

  if (match) {
    return {
      direction: match[1] as 'Long' | 'Short',
      companyName: match[2].trim(),
      symbol: match[3].toUpperCase(),
      date: match[4],
    };
  }

  // Try to at least extract the ticker if full pattern doesn't match
  const tickerMatch = text.match(/\(\$(\w+)\)/);
  const dateMatch = text.match(/\[(\d{2}\/\d{2}\/\d{4})\]/);
  const directionMatch = text.match(/^(Long|Short)/i);

  return {
    direction: directionMatch ? (directionMatch[1].charAt(0).toUpperCase() + directionMatch[1].slice(1).toLowerCase()) as 'Long' | 'Short' : undefined,
    symbol: tickerMatch ? tickerMatch[1].toUpperCase() : undefined,
    date: dateMatch ? dateMatch[1] : undefined,
  };
}

/**
 * Extract summary information from idea rows on the list page
 * Phase A of two-phase extraction
 */
export async function extractIdeaSummaries(page: Page): Promise<IdeaSummary[]> {
  console.log('Extracting idea summaries from list page...');

  const summaries: IdeaSummary[] = [];

  // Find all idea rows using the updated selector
  const rows = await page.$$(selectors.ideasPage.ideaRow);
  console.log(`Found ${rows.length} idea rows`);

  for (let i = 0; i < rows.length; i++) {
    const row = rows[i];

    try {
      // Get the clickable element with data-posts attribute
      const clickableEl = await row.$(selectors.ideasPage.ideaClickable);
      if (!clickableEl) {
        console.log(`Row ${i}: No clickable element found, skipping`);
        continue;
      }

      // Get post IDs from data-posts attribute
      const dataPostsAttr = await clickableEl.getAttribute('data-posts');
      const postIds = dataPostsAttr ? dataPostsAttr.split(',').map(id => id.trim()) : [];

      if (postIds.length === 0) {
        console.log(`Row ${i}: No post IDs found, skipping`);
        continue;
      }

      // Extract summary text from span inside the clickable element
      const summaryEl = await clickableEl.$('span');
      const summaryText = summaryEl ? await summaryEl.textContent() || '' : await clickableEl.textContent() || '';

      // Parse the summary text to extract structured data
      const parsed = parseSummaryText(summaryText.trim());

      // Generate row ID from post IDs
      const rowId = `idea-${postIds[0]}`;

      summaries.push({
        rowId,
        postIds,
        summaryText: summaryText.trim(),
        direction: parsed.direction,
        companyName: parsed.companyName,
        symbol: parsed.symbol,
        date: parsed.date,
      });

      console.log(`Row ${i}: ${parsed.direction || '?'} ${parsed.symbol || '?'} [${parsed.date || '?'}]`);

    } catch (e) {
      console.error(`Error extracting row ${i}:`, e);
    }
  }

  console.log(`Extracted ${summaries.length} idea summaries`);
  return summaries;
}

/**
 * Extract full details from an idea's detail page
 * Phase B of two-phase extraction
 */
export async function extractIdeaDetail(page: Page, summary: IdeaSummary): Promise<IdeaDetail | null> {
  console.log(`Extracting details from: ${summary.detailUrl}`);

  const navResult = await navigateToIdeaDetail(page, summary.detailUrl);

  if (!navResult.success) {
    console.error(`Failed to navigate to detail page: ${navResult.error}`);
    await captureErrorScreenshot(page, summary.rowId);
    return null;
  }

  try {
    const detail: IdeaDetail = {
      ideaId: summary.rowId,
      sourceUrl: summary.detailUrl,
      scrapedAt: new Date().toISOString(),
    };

    // Extract title
    const titleEl = await page.$(selectors.detailPage.title);
    detail.title = titleEl ? (await titleEl.textContent())?.trim() : undefined;

    // Extract symbol
    const symbolEl = await page.$(selectors.detailPage.symbol);
    detail.symbol = symbolEl
      ? (await symbolEl.textContent())?.trim().toUpperCase()
      : summary.symbolText?.toUpperCase();

    // Determine side (buy/sell)
    const buyIndicator = await page.$(selectors.detailPage.side.buy);
    const sellIndicator = await page.$(selectors.detailPage.side.sell);
    if (buyIndicator) {
      detail.side = 'buy';
    } else if (sellIndicator) {
      detail.side = 'sell';
    } else {
      detail.side = 'unknown';
    }

    // Extract allocation/weight
    const weightEl = await page.$(selectors.detailPage.allocation.weight);
    const pctEl = await page.$(selectors.detailPage.allocation.percentage);
    detail.relativeWeight = weightEl
      ? (await weightEl.textContent())?.trim()
      : pctEl
        ? (await pctEl.textContent())?.trim()
        : undefined;

    // Extract prices
    const entryEl = await page.$(selectors.detailPage.prices.entry);
    detail.entryPrice = entryEl ? (await entryEl.textContent())?.trim() : undefined;

    const targetEl = await page.$(selectors.detailPage.prices.target);
    detail.targetPrice = targetEl ? (await targetEl.textContent())?.trim() : undefined;

    const stopEl = await page.$(selectors.detailPage.prices.stopLoss);
    detail.stopLoss = stopEl ? (await stopEl.textContent())?.trim() : undefined;

    // Determine status
    const openIndicator = await page.$(selectors.detailPage.status.open);
    const closedIndicator = await page.$(selectors.detailPage.status.closed);
    if (openIndicator) {
      detail.status = 'open';
    } else if (closedIndicator) {
      detail.status = 'closed';
    } else {
      detail.status = 'unknown';
    }

    // Extract date
    const dateEl = await page.$(selectors.detailPage.date);
    detail.date = dateEl
      ? (await dateEl.getAttribute('datetime')) || (await dateEl.textContent())?.trim()
      : summary.dateText;

    // Extract notes/content
    const notesEls = await page.$$(selectors.detailPage.notes);
    if (notesEls.length > 0) {
      const notesTexts = await Promise.all(
        notesEls.map(el => el.textContent())
      );
      detail.notes = notesTexts.filter(Boolean).join('\n\n').trim();
    }

    // Optionally capture raw HTML for debugging
    const container = await page.$(selectors.detailPage.container);
    if (container) {
      detail.rawHtml = await container.innerHTML();
    }

    return detail;

  } catch (e) {
    console.error(`Error extracting detail from ${summary.detailUrl}:`, e);
    await captureErrorScreenshot(page, summary.rowId);
    return null;
  }
}

/**
 * Convert summary to IdeaDetail (MVP: extract directly from summary text)
 */
function summaryToDetail(summary: IdeaSummary): IdeaDetail {
  return {
    ideaId: summary.rowId,
    sourceUrl: `https://bravosresearch.com/ideas/#${summary.postIds[0]}`,
    scrapedAt: new Date().toISOString(),
    title: summary.summaryText,
    symbol: summary.symbol,
    side: summary.direction === 'Long' ? 'buy' : summary.direction === 'Short' ? 'sell' : 'unknown',
    date: summary.date,
    status: 'open', // Assume all visible ideas are open
    notes: summary.companyName,
    // These fields would require detail page navigation
    relativeWeight: undefined,
    entryPrice: undefined,
    targetPrice: undefined,
    stopLoss: undefined,
  };
}

/**
 * Run full extraction: list page summaries + optional detail pages
 */
export async function runFullExtraction(page: Page, fetchDetails: boolean = false): Promise<ExtractionResult> {
  const timestamp = new Date().toISOString().replace(/[:.]/g, '-');
  const result: ExtractionResult = {
    ideas: [],
    errors: [],
    timestamp,
  };

  // Phase A: Extract summaries from list page
  const summaries = await extractIdeaSummaries(page);

  if (!fetchDetails) {
    // MVP mode: convert summaries directly to details
    console.log(`\nConverting ${summaries.length} summaries to idea details...`);
    for (const summary of summaries) {
      if (summary.symbol) {
        result.ideas.push(summaryToDetail(summary));
      } else {
        result.errors.push({
          url: summary.rowId,
          error: 'Could not parse symbol from summary',
        });
      }
    }
  } else {
    // Full mode: navigate to detail pages
    console.log(`\nStarting Phase B: Extracting ${summaries.length} detail pages...\n`);

    for (let i = 0; i < summaries.length; i++) {
      const summary = summaries[i];
      console.log(`[${i + 1}/${summaries.length}] Processing idea ${summary.rowId}`);

      try {
        // For now, use summary-based extraction
        // TODO: Implement AJAX-based detail extraction by clicking on elements
        const detail = summaryToDetail(summary);
        result.ideas.push(detail);

      } catch (e) {
        const errorMsg = e instanceof Error ? e.message : String(e);
        result.errors.push({
          url: summary.rowId,
          error: errorMsg,
        });
      }

      // Small delay to be polite
      await page.waitForTimeout(200);
    }
  }

  console.log(`\nExtraction complete: ${result.ideas.length} ideas, ${result.errors.length} errors`);

  return result;
}

/**
 * Save extraction results to disk
 */
export function saveRawData(result: ExtractionResult): string {
  if (!existsSync(RAW_DATA_DIR)) {
    mkdirSync(RAW_DATA_DIR, { recursive: true });
  }

  const filename = `ideas-${result.timestamp}.json`;
  const filepath = `${RAW_DATA_DIR}/${filename}`;

  writeFileSync(filepath, JSON.stringify(result, null, 2));
  console.log(`Saved raw data to ${filepath}`);

  return filepath;
}

/**
 * Capture screenshot on error for debugging
 */
async function captureErrorScreenshot(page: Page, id: string): Promise<void> {
  if (!existsSync(ERROR_SCREENSHOT_DIR)) {
    mkdirSync(ERROR_SCREENSHOT_DIR, { recursive: true });
  }

  const timestamp = new Date().toISOString().replace(/[:.]/g, '-');
  const filename = `error-${id}-${timestamp}.png`;
  const filepath = `${ERROR_SCREENSHOT_DIR}/${filename}`;

  try {
    await page.screenshot({ path: filepath, fullPage: true });
    console.log(`Error screenshot saved to ${filepath}`);
  } catch (e) {
    console.error('Failed to capture error screenshot:', e);
  }
}
