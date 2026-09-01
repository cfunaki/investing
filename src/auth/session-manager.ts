import { chromium, Browser, BrowserContext, Page } from 'playwright';
import { existsSync, readFileSync, writeFileSync, mkdirSync } from 'fs';
import { dirname } from 'path';
import { config } from 'dotenv';
import { isLoggedIn, waitForLogin } from './login-detector.js';
import { hasAuthCookie, authCookieNames } from './auth-cookie.js';

// Load environment variables
config();

const SESSION_PATH = 'data/sessions/bravos.json';
const BRAVOS_BASE_URL = process.env.BRAVOS_BASE_URL || 'https://bravosresearch.com';
const BRAVOS_LOGIN_URL = `${BRAVOS_BASE_URL}/login`;
const BRAVOS_IDEAS_URL = `${BRAVOS_BASE_URL}/ideas/`;
const BRAVOS_USERNAME = process.env.BRAVOS_USERNAME;
const BRAVOS_PASSWORD = process.env.BRAVOS_PASSWORD;

export interface SessionManager {
  browser: Browser;
  context: BrowserContext;
  page: Page;
}

/**
 * Load saved session state if it exists
 */
export function loadStorageState(): object | undefined {
  if (existsSync(SESSION_PATH)) {
    try {
      const data = readFileSync(SESSION_PATH, 'utf-8');
      return JSON.parse(data);
    } catch (e) {
      console.error('Failed to load session state:', e);
      return undefined;
    }
  }
  return undefined;
}

/**
 * Save current session state to disk
 */
export async function saveStorageState(context: BrowserContext): Promise<void> {
  // Refuse to persist a logged-out session. The browser worker uploads this
  // file to GCS after every scrape, so saving a logged-out state overwrites a
  // good session and makes the failure self-perpetuating.
  if (!(await hasAuthCookie(context))) {
    throw new Error(
      'Refusing to save session: no WordPress auth cookie present. ' +
        'The browser is not logged in to Bravos.'
    );
  }

  const dir = dirname(SESSION_PATH);
  if (!existsSync(dir)) {
    mkdirSync(dir, { recursive: true });
  }

  const state = await context.storageState();
  writeFileSync(SESSION_PATH, JSON.stringify(state, null, 2));
  console.log(`Session saved to ${SESSION_PATH}`);
}


/**
 * Perform automated login using credentials from .env
 */
async function performAutomatedLogin(page: Page): Promise<boolean> {
  if (!BRAVOS_USERNAME || !BRAVOS_PASSWORD) {
    console.log('No Bravos credentials found in .env');
    return false;
  }

  console.log(`Attempting automated login as ${BRAVOS_USERNAME}...`);

  try {
    // Scope everything to the real login form. The previous implementation
    // used document.querySelector across the whole page with a comma-separated
    // selector list, which returns the first match in DOCUMENT order — on this
    // page that can be a newsletter/subscribe email input rather than the
    // login field, so the credentials were typed into the wrong box.
    let form = page.locator('form.woocommerce-form-login').first();
    if ((await form.count()) === 0) {
      // Fall back to whichever form actually contains a password input.
      form = page.locator('form:has(input[type="password"])').first();
    }
    if ((await form.count()) === 0) {
      console.log('No login form found on page');
      return false;
    }

    const userField = form.locator(
      'input[name="username"], input[name="log"], input[name="email"]'
    ).first();
    const passField = form.locator('input[type="password"]').first();

    if ((await userField.count()) === 0 || (await passField.count()) === 0) {
      console.log('Login form found but username/password inputs are missing');
      return false;
    }

    // Native fill dispatches real key/input events and respects the field's
    // own handlers, unlike assigning .value from injected JS.
    await userField.fill(BRAVOS_USERNAME);
    await passField.fill(BRAVOS_PASSWORD);

    // Tick "Remember me" so WordPress issues a ~14-day cookie instead of ~2-day.
    // Set it at the DOM level: this page's layout makes Playwright report every
    // control as "outside of the viewport", so .check() — even with force —
    // throws. Never let this step block the login.
    try {
      const remember = form.locator('input[type="checkbox"][name*="remember" i]').first();
      if ((await remember.count()) > 0) {
        await remember.evaluate((el) => {
          const box = el as HTMLInputElement;
          if (!box.checked) {
            box.checked = true;
            box.dispatchEvent(new Event('change', { bubbles: true }));
          }
        });
        console.log('Checked "Remember me" (extends the session to ~14 days)');
      }
    } catch (e) {
      console.log(`Could not tick "Remember me" (continuing): ${(e as Error).message.split('\n')[0]}`);
    }

    console.log(`Filled login form. username field value: "${await userField.inputValue()}"`);

    // Submit at the DOM level via requestSubmit(). Every control on this page
    // reports as "outside of the viewport" to Playwright — .click() (even with
    // force) and Enter both fail — but requestSubmit() involves no geometry and,
    // unlike form.submit(), passes the submit button as the submitter so
    // WooCommerce still receives the $_POST['login'] field process_login()
    // requires.
    const how = await form.evaluate((f) => {
      const el = f as HTMLFormElement;
      const btn = el.querySelector<HTMLElement>(
        'button[name="login"], button[type="submit"], input[type="submit"]'
      );
      if (typeof el.requestSubmit === 'function') {
        el.requestSubmit((btn as HTMLElement | null) ?? undefined);
        return 'requestSubmit';
      }
      if (btn) {
        btn.click();
        return 'buttonClick';
      }
      el.submit();
      return 'formSubmit';
    });
    console.log(`Submitted login form via ${how}`);

    await page.waitForLoadState('networkidle').catch(() => {});
    await page.waitForTimeout(2500);

    // Surface whatever WooCommerce said, rather than guessing from the DOM.
    const errorEl = page.locator('.woocommerce-error, .woocommerce-error li').first();
    if ((await errorEl.count()) > 0) {
      const text = (await errorEl.innerText().catch(() => '')).trim();
      if (text) {
        console.log(`Site reported: ${text.replace(/\s+/g, ' ').slice(0, 200)}`);
      }
    }

    await page.goto('https://bravosresearch.com/ideas/', { waitUntil: 'domcontentloaded' });
    await page.waitForLoadState('networkidle');
    await page.waitForTimeout(2000);

    console.log(`Ideas page URL: ${page.url()}`);
    await page.screenshot({ path: 'data/raw/debug-ideas-logged-in.png', fullPage: true });

    // Authoritative check: the WordPress auth cookie. DOM content is not
    // evidence of login — the public page renders articles and account links
    // too, which is what made this report success while logged out.
    const loggedIn = await hasAuthCookie(page.context());
    if (loggedIn) {
      console.log(
        `Login verified via auth cookie: ${(await authCookieNames(page.context())).join(', ')}`
      );
      return true;
    }

    console.log('No WordPress auth cookie present - automated login did NOT succeed.');
    return false;

  } catch (e) {
    console.error('Automated login failed:', e);
    return false;
  }
}

/**
 * Initialize a new session - tries automated login first, falls back to manual
 */
export async function initSession(): Promise<SessionManager> {
  const hasCredentials = !!(BRAVOS_USERNAME && BRAVOS_PASSWORD);

  console.log('Launching browser...');

  const browser = await chromium.launch({
    headless: false,  // Always visible for now to debug
    slowMo: 100,
  });

  const context = await browser.newContext({
    viewport: { width: 1920, height: 1080 },  // Larger viewport
  });

  const page = await context.newPage();

  // Navigate to login page
  console.log(`Navigating to ${BRAVOS_LOGIN_URL}`);
  await page.goto(BRAVOS_LOGIN_URL, { waitUntil: 'networkidle' });

  // Try automated login if credentials are available
  if (hasCredentials) {
    const success = await performAutomatedLogin(page);
    if (success) {
      console.log('Saving session...');
      await saveStorageState(context);
      return { browser, context, page };
    }

    // Automated login failed - relaunch in visible mode for manual login
    console.log('Automated login failed, switching to manual mode...');
    await browser.close();

    const manualBrowser = await chromium.launch({
      headless: false,
      slowMo: 100,
    });

    const manualContext = await manualBrowser.newContext({
      viewport: { width: 1280, height: 800 },
    });

    const manualPage = await manualContext.newPage();
    await manualPage.goto(BRAVOS_LOGIN_URL, { waitUntil: 'networkidle' });

    console.log('\n===========================================');
    console.log('Automated login failed. Please log in manually.');
    console.log('The script will continue once login is detected.');
    console.log('===========================================\n');

    await waitForLogin(manualPage);

    console.log('Login successful! Saving session...');
    await saveStorageState(manualContext);

    return { browser: manualBrowser, context: manualContext, page: manualPage };
  }

  // No credentials - manual login
  console.log('\n===========================================');
  console.log('Please log in to Bravos Research manually.');
  console.log('The script will continue once login is detected.');
  console.log('===========================================\n');

  await waitForLogin(page);

  console.log('Login successful! Saving session...');
  await saveStorageState(context);

  return { browser, context, page };
}

/**
 * Resume an existing session from saved state
 * Returns null if session is invalid/expired
 */
export async function resumeSession(): Promise<SessionManager | null> {
  const storageState = loadStorageState();

  if (!storageState) {
    console.log('No saved session found.');
    return null;
  }

  console.log('Found saved session, attempting to resume...');

  const browser = await chromium.launch({
    headless: true,  // Can run headless for scraping
  });

  const context = await browser.newContext({
    storageState: storageState as any,
    viewport: { width: 1280, height: 800 },
  });

  const page = await context.newPage();

  // Test if session is still valid by navigating to protected page
  console.log('Validating session...');
  await page.goto(BRAVOS_IDEAS_URL, { waitUntil: 'networkidle' });

  if (await isLoggedIn(page)) {
    console.log('Session is valid!');
    return { browser, context, page };
  }

  console.log('Session expired or invalid.');
  await browser.close();
  return null;
}

/**
 * Get a valid session - resume existing or prompt for new login
 */
export async function getSession(): Promise<SessionManager> {
  // Try to resume existing session first
  const existing = await resumeSession();
  if (existing) {
    return existing;
  }

  // Fall back to manual login
  console.log('Starting manual login flow...');
  return initSession();
}

/**
 * Clean up browser resources
 */
export async function closeSession(session: SessionManager): Promise<void> {
  await session.browser.close();
}
