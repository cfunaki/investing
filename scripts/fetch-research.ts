import { chromium } from 'playwright';
import { readFileSync, writeFileSync } from 'fs';

const SESSION_PATH = 'data/sessions/bravos.json';

async function main() {
  const storageState = JSON.parse(readFileSync(SESSION_PATH, 'utf-8'));
  
  const browser = await chromium.launch({ headless: true });
  const context = await browser.newContext({ storageState: storageState as any });
  const page = await context.newPage();
  
  // Fetch the research/dashboard page (where we saw Active Trade Ideas)
  const url = 'https://bravosresearch.com/research/';
  console.log('Fetching:', url);
  
  await page.goto(url, { waitUntil: 'networkidle' });
  
  // Save HTML
  const html = await page.content();
  writeFileSync('data/raw/research-page.html', html);
  console.log('Saved HTML to data/raw/research-page.html');
  
  // Take screenshot
  await page.screenshot({ path: 'data/raw/research-page.png', fullPage: true });
  console.log('Saved screenshot to data/raw/research-page.png');
  
  await browser.close();
}

main().catch(console.error);
