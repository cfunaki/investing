import { chromium, Browser, BrowserContext, Page } from 'playwright';
import { existsSync, readFileSync, writeFileSync, mkdirSync } from 'fs';
import { dirname } from 'path';
import { config } from 'dotenv';
import { isLoggedIn, waitForLogin } from './login-detector.js';

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
    // Fill form via JavaScript to bypass viewport issues
    console.log('Filling login form via JavaScript...');

    const formFilled = await page.evaluate(({ username, password }) => {
      // Find username field
      const usernameInput = document.querySelector<HTMLInputElement>(
        'input[name="email"], input[name="username"], input[type="email"], input#email, input#username'
      );
      if (!usernameInput) {
        console.error('Username field not found');
        return { success: false, error: 'Username field not found' };
      }

      // Find password field
      const passwordInput = document.querySelector<HTMLInputElement>(
        'input[name="password"], input[type="password"], input#password'
      );
      if (!passwordInput) {
        console.error('Password field not found');
        return { success: false, error: 'Password field not found' };
      }

      // Fill the fields
      usernameInput.value = username;
      usernameInput.dispatchEvent(new Event('input', { bubbles: true }));
      usernameInput.dispatchEvent(new Event('change', { bubbles: true }));

      passwordInput.value = password;
      passwordInput.dispatchEvent(new Event('input', { bubbles: true }));
      passwordInput.dispatchEvent(new Event('change', { bubbles: true }));

      // Check "Remember me" if present
      const rememberMe = document.querySelector<HTMLInputElement>(
        'input[type="checkbox"]#rememberme, input[name="rememberme"], input[type="checkbox"][name*="remember"]'
      );
      if (rememberMe && !rememberMe.checked) {
        rememberMe.checked = true;
        rememberMe.dispatchEvent(new Event('change', { bubbles: true }));
      }

      return { success: true, usernameValue: usernameInput.value, hasRememberMe: !!rememberMe };
    }, { username: BRAVOS_USERNAME, password: BRAVOS_PASSWORD });

    console.log('Form fill result:', formFilled);

    if (!formFilled.success) {
      console.log(`Form fill failed: ${formFilled.error}`);
      return false;
    }

    // Wait a moment for form to be ready
    await page.waitForTimeout(500);

    // Debug: Take screenshot before submission
    await page.screenshot({ path: 'data/raw/debug-before-submit.png', fullPage: true });
    console.log('Screenshot before submit saved');

    // Submit form via JavaScript
    console.log('Submitting login form via JavaScript...');
    await page.evaluate(() => {
      const form = document.querySelector('form.woocommerce-form-login, form.login, form#loginform, form');
      if (form) {
        // Try clicking the submit button first
        const submitBtn = form.querySelector<HTMLButtonElement>('button[type="submit"], input[type="submit"], button:not([type])');
        if (submitBtn) {
          submitBtn.click();
        } else {
          // Fallback to form submit
          (form as HTMLFormElement).submit();
        }
      }
    });

    // Wait for navigation to complete
    console.log('Waiting for navigation...');
    try {
      await Promise.race([
        page.waitForNavigation({ waitUntil: 'networkidle', timeout: 15000 }),
        page.waitForTimeout(15000)
      ]);
    } catch (e) {
      console.log('Navigation wait completed (or timed out)');
    }

    // Wait additional time for any AJAX/SPA updates
    await page.waitForTimeout(3000);
    console.log(`After wait, URL is: ${page.url()}`);

    // Check page content to debug
    const pageTitle = await page.title();
    console.log(`Page title: ${pageTitle}`);

    // Check for any error messages on the page
    const errorMessage = await page.$('.error, .alert-error, .login-error, .woocommerce-error');
    if (errorMessage) {
      const errorText = await errorMessage.textContent();
      console.log(`Error on page: ${errorText}`);
    }

    // Log visible elements to help debug
    const visibleLoginForm = await page.$('form.login, form#loginform, .woocommerce-form-login');
    if (visibleLoginForm) {
      console.log('Login form still visible - login may have failed');
    }

    const logoutLink = await page.$('a[href*="logout"]');
    if (logoutLink) {
      console.log('Found logout link - we ARE logged in!');
    }

    const currentUrl = page.url();
    console.log(`Current URL after login: ${currentUrl}`);

    // Take debug screenshot
    await page.screenshot({ path: 'data/raw/debug-after-login.png', fullPage: true });
    console.log('Debug screenshot saved to data/raw/debug-after-login.png');

    // Check if we're still on login page
    if (currentUrl.includes('/login')) {
      console.log('Still on login page - login may have failed');
      return false;
    }

    // We're redirected - navigate to ideas page to verify access
    console.log('Login appears successful, checking /ideas/ access...');
    await page.goto('https://bravosresearch.com/ideas/', { waitUntil: 'domcontentloaded' });
    await page.waitForLoadState('networkidle');
    await page.waitForTimeout(2000);

    const ideasUrl = page.url();
    console.log(`Ideas page URL: ${ideasUrl}`);

    // Take screenshot of ideas page
    await page.screenshot({ path: 'data/raw/debug-ideas-logged-in.png', fullPage: true });
    console.log('Ideas page screenshot saved to data/raw/debug-ideas-logged-in.png');

    // Check if we can see actual ideas content (vs paywall)
    const hasPaywall = await page.$('h1:has-text("Memberships")');
    if (hasPaywall) {
      console.log('WARNING: Ideas page shows membership paywall');
      return false;
    }

    // Check for actual ideas content
    const hasIdeasContent = await page.$('article, .idea, .trade, .post, table');
    if (hasIdeasContent) {
      console.log('Found ideas content - login successful!');
      return true;
    }

    // Check if logged in by looking for logout link
    const hasLogout = await page.$('a[href*="logout"], a:has-text("Log Out"), a:has-text("Sign Out")');
    if (hasLogout) {
      console.log('Found logout link - logged in but may need subscription');
      return true;
    }

    console.log('Could not verify login status');
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
