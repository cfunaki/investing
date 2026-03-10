import { Page } from 'playwright';

const BRAVOS_IDEAS_URL = 'https://bravosresearch.com/ideas/';
const LOGIN_PAGE_PATTERNS = ['/login', '/signin', '/auth'];
const MAX_WAIT_MS = 5 * 60 * 1000;  // 5 minutes max wait for login
const CHECK_INTERVAL_MS = 1000;      // Check every second

/**
 * Check if the current page indicates a logged-in state.
 *
 * Detection strategies (in order of reliability):
 * 1. URL is not a login page
 * 2. Presence of authenticated UI elements (user menu, avatar, etc.)
 * 3. Absence of login form
 */
export async function isLoggedIn(page: Page): Promise<boolean> {
  const url = page.url();

  // Check if we're on a login page
  const onLoginPage = LOGIN_PAGE_PATTERNS.some(pattern =>
    url.toLowerCase().includes(pattern)
  );

  if (onLoginPage) {
    return false;
  }

  // First check for NOT logged in indicators (more reliable)
  const notLoggedInIndicators = [
    'a:has-text("Log In")',
    'a:has-text("Sign In")',
    'button:has-text("Log In")',
    'a[href*="/login"]',
    '.access-restricted',  // Bravos uses this class for restricted content
  ];

  for (const selector of notLoggedInIndicators) {
    try {
      const element = await page.$(selector);
      if (element && await element.isVisible()) {
        return false;  // Found "Log In" link = not logged in
      }
    } catch {
      // Selector didn't match, continue
    }
  }

  // Check for authenticated UI elements
  const authIndicators = [
    'a[href*="logout"]',
    'a[href*="signout"]',
    'a[href*="/account"]',
    'a[href*="/my-account"]',
    'button:has-text("Sign Out")',
    'button:has-text("Log Out")',
    '.user-profile',
    '.user-menu',
    '.user-avatar',
    // Bravos-specific - actual ideas content
    '.idea-card',
    '.trade-idea',
    '.ideas-content',
  ];

  for (const selector of authIndicators) {
    try {
      const element = await page.$(selector);
      if (element) {
        return true;
      }
    } catch {
      // Selector didn't match, continue checking
    }
  }

  return false;
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
