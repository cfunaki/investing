import { Page } from 'playwright';
import { pageHasAuthCookie } from './auth-cookie.js';

const BRAVOS_IDEAS_URL = 'https://bravosresearch.com/ideas/';
const LOGIN_PAGE_PATTERNS = ['/login', '/signin', '/auth'];
const MAX_WAIT_MS = 5 * 60 * 1000;  // 5 minutes max wait for login
const CHECK_INTERVAL_MS = 1000;      // Check every second

/**
 * Check whether we are logged in.
 *
 * This is decided solely by the presence of the WordPress auth cookie. The
 * previous implementation guessed from DOM selectors ('article', '.post',
 * 'a[href*="/account"]'), all of which are present on the logged-OUT page —
 * so it reported success for a failed login and poisoned the saved session.
 */
export async function isLoggedIn(page: Page): Promise<boolean> {
  return pageHasAuthCookie(page);
}

/**
 * Wait for the user to complete login.
 * Polls the page until logged-in state is detected or timeout.
 */
export async function waitForLogin(page: Page): Promise<void> {
  const startTime = Date.now();

  while (Date.now() - startTime < MAX_WAIT_MS) {
    // Check current page
    if (await isLoggedIn(page)) {
      return;
    }

    // Also check if we've been redirected to ideas page
    const url = page.url();
    if (url.includes('/ideas') && !url.includes('/login')) {
      // Double-check with a page reload
      await page.reload({ waitUntil: 'networkidle' });
      if (await isLoggedIn(page)) {
        return;
      }
    }

    // Wait before checking again
    await page.waitForTimeout(CHECK_INTERVAL_MS);
  }

  throw new Error(`Login timeout after ${MAX_WAIT_MS / 1000} seconds`);
}

/**
 * Navigate to a protected page and verify we're still logged in.
 * Useful for checking session validity before scraping.
 */
export async function verifySession(page: Page): Promise<boolean> {
  try {
    await page.goto(BRAVOS_IDEAS_URL, {
      waitUntil: 'networkidle',
      timeout: 30000
    });
    return await isLoggedIn(page);
  } catch (e) {
    console.error('Session verification failed:', e);
    return false;
  }
}
