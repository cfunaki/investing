import { BrowserContext, Page } from 'playwright';

/**
 * Ground truth for "are we logged in to Bravos".
 *
 * Bravos runs WordPress/WooCommerce, which sets a `wordpress_logged_in_<hash>`
 * cookie on successful auth and clears it on logout. That cookie is the only
 * unambiguous signal.
 *
 * DOM heuristics ("is there an article element?", "is there a logout link?")
 * were previously used instead, and they matched the logged-OUT page — so a
 * failed login reported success, a logged-out session was saved over the good
 * one in GCS, and the scraper silently returned zero positions for a month.
 */
const AUTH_COOKIE_PREFIXES = ['wordpress_logged_in_', 'wordpress_sec_'];

export async function hasAuthCookie(context: BrowserContext): Promise<boolean> {
  const cookies = await context.cookies();
  return cookies.some((c) =>
    AUTH_COOKIE_PREFIXES.some((prefix) => c.name.startsWith(prefix)) && !!c.value
  );
}

/**
 * Same check, addressed from a page.
 */
export async function pageHasAuthCookie(page: Page): Promise<boolean> {
  return hasAuthCookie(page.context());
}

/**
 * Names of the auth cookies currently held — for diagnostics.
 */
export async function authCookieNames(context: BrowserContext): Promise<string[]> {
  const cookies = await context.cookies();
  return cookies
    .filter((c) => AUTH_COOKIE_PREFIXES.some((p) => c.name.startsWith(p)))
    .map((c) => c.name);
}
