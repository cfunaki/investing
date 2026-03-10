import { Page } from 'playwright';
import { readFileSync } from 'fs';

// Load selectors from config
const selectors = JSON.parse(
  readFileSync(new URL('./selectors.json', import.meta.url), 'utf-8')
);

const BRAVOS_IDEAS_URL = 'https://bravosresearch.com/ideas/';
const MAX_RETRIES = 3;
const RETRY_DELAY_MS = 2000;
const SCROLL_DELAY_MS = 1000;
const MAX_SCROLL_ATTEMPTS = 50;

export interface NavigationResult {
  success: boolean;
  currentUrl: string;
  error?: string;
}

/**
 * Navigate to the Bravos ideas page with retry logic
 */
export async function navigateToIdeas(page: Page): Promise<NavigationResult> {
  let lastError: string | undefined;

  for (let attempt = 1; attempt <= MAX_RETRIES; attempt++) {
    try {
      console.log(`Navigating to ideas page (attempt ${attempt}/${MAX_RETRIES})...`);

      await page.goto(BRAVOS_IDEAS_URL, {
        waitUntil: 'networkidle',
        timeout: 30000,
      });

      // Wait for content to load
      await page.waitForSelector(
        selectors.ideasPage.listContainer,
        { timeout: 10000 }
      ).catch(() => {
        // If main selector fails, try alternatives
        console.log('Primary list container not found, checking alternatives...');
      });

      return {
        success: true,
        currentUrl: page.url(),
      };

    } catch (e) {
      lastError = e instanceof Error ? e.message : String(e);
      console.error(`Navigation attempt ${attempt} failed:`, lastError);

      if (attempt < MAX_RETRIES) {
        console.log(`Waiting ${RETRY_DELAY_MS}ms before retry...`);
        await page.waitForTimeout(RETRY_DELAY_MS);
      }
    }
  }

  return {
    success: false,
    currentUrl: page.url(),
    error: lastError,
  };
}

/**
 * Check if there's a next page and navigate to it
 * Returns true if successfully navigated to next page, false if no more pages
 */
export async function goToNextPage(page: Page): Promise<boolean> {
  const pagination = selectors.ideasPage.pagination;

  // Try clicking next button
  const nextButton = await page.$(pagination.nextButton);
  if (nextButton) {
    const isDisabled = await nextButton.evaluate((el: Element) =>
      el.hasAttribute('disabled') ||
      el.classList.contains('disabled') ||
      el.getAttribute('aria-disabled') === 'true'
    );

    if (!isDisabled) {
      console.log('Clicking next page button...');
      await nextButton.click();
      await page.waitForLoadState('networkidle');
      return true;
    }
  }

  // Try load more button
  const loadMoreButton = await page.$(pagination.loadMoreButton);
  if (loadMoreButton) {
    const isVisible = await loadMoreButton.isVisible();
    if (isVisible) {
      console.log('Clicking load more button...');
      await loadMoreButton.click();
      await page.waitForLoadState('networkidle');
      return true;
    }
  }

  return false;
}

/**
 * Handle infinite scroll - scroll down and wait for new content
 * Returns the number of new items loaded, or 0 if no more content
 */
export async function handleInfiniteScroll(page: Page): Promise<number> {
  // Count current items
  const initialCount = await page.$$(selectors.ideasPage.ideaRow).then(rows => rows.length);

  let scrollAttempts = 0;
  let currentCount = initialCount;

  while (scrollAttempts < MAX_SCROLL_ATTEMPTS) {
    // Scroll to bottom
    await page.evaluate(() => {
      window.scrollTo(0, document.body.scrollHeight);
    });

    // Wait for potential new content
    await page.waitForTimeout(SCROLL_DELAY_MS);

    // Check for loading indicator
    const loadingIndicator = await page.$(selectors.ideasPage.infiniteScroll.sentinel);
    if (loadingIndicator) {
      const isVisible = await loadingIndicator.isVisible();
      if (isVisible) {
        // Wait for loading to complete
        await page.waitForFunction(
          (selector: string) => {
            const el = document.querySelector(selector);
            return !el || !el.checkVisibility();
          },
          selectors.ideasPage.infiniteScroll.sentinel,
          { timeout: 10000 }
        ).catch(() => {
          // Loading indicator might not disappear, continue anyway
        });
      }
    }

    // Count items after scroll
    const newCount = await page.$$(selectors.ideasPage.ideaRow).then(rows => rows.length);

    if (newCount > currentCount) {
      console.log(`Loaded ${newCount - currentCount} more items (total: ${newCount})`);
      currentCount = newCount;
      scrollAttempts = 0;  // Reset attempts on successful load
    } else {
      scrollAttempts++;
    }

    // Check if we've stopped getting new content
    if (scrollAttempts >= 3) {
      break;
    }
  }

  return currentCount - initialCount;
}

/**
 * Navigate to a specific idea's detail page
 */
export async function navigateToIdeaDetail(page: Page, url: string): Promise<NavigationResult> {
  let lastError: string | undefined;

  for (let attempt = 1; attempt <= MAX_RETRIES; attempt++) {
    try {
      console.log(`Navigating to idea detail: ${url} (attempt ${attempt}/${MAX_RETRIES})`);

      await page.goto(url, {
        waitUntil: 'networkidle',
        timeout: 30000,
      });

      // Wait for detail content to load
      await page.waitForSelector(
        selectors.detailPage.container,
        { timeout: 10000 }
      );

      return {
        success: true,
        currentUrl: page.url(),
      };

    } catch (e) {
      lastError = e instanceof Error ? e.message : String(e);
      console.error(`Detail navigation attempt ${attempt} failed:`, lastError);

      if (attempt < MAX_RETRIES) {
        await page.waitForTimeout(RETRY_DELAY_MS);
      }
    }
  }

  return {
    success: false,
    currentUrl: page.url(),
    error: lastError,
  };
}

/**
 * Go back to the ideas list from a detail page
 */
export async function returnToIdeasList(page: Page): Promise<NavigationResult> {
  return navigateToIdeas(page);
}
