import { chromium } from 'playwright';
import { readFileSync, writeFileSync } from 'fs';

const SESSION_PATH = 'data/sessions/bravos.json';

async function main() {
  const storageState = JSON.parse(readFileSync(SESSION_PATH, 'utf-8'));
  
  const browser = await chromium.launch({ headless: true });
  const context = await browser.newContext({ storageState: storageState as any });
  const page = await context.newPage();
  
  // Fetch a detail page
  const detailUrl = 'https://bravosresearch.com/news-feed/initiating-long-on-aluminum-alum-breakout/';
  console.log('Fetching:', detailUrl);
  
  await page.goto(detailUrl, { waitUntil: 'networkidle' });
  
  // Save HTML
  const html = await page.content();
  writeFileSync('data/raw/detail-page.html', html);
  console.log('Saved HTML to data/raw/detail-page.html');
  
  // Take screenshot
  await page.screenshot({ path: 'data/raw/detail-page.png', fullPage: true });
  console.log('Saved screenshot to data/raw/detail-page.png');
  
  await browser.close();
}

main().catch(console.error);
