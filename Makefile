.PHONY: install init scrape scrape-ideas scrape-active normalize reconcile report all clean

# Install all dependencies
install:
	npm install
	npx playwright install chromium
	python -m venv .venv
	.venv/bin/pip install -e .

# First-time Bravos login (opens browser for manual auth)
init:
	npm run init-session

# Scrape active trades from Bravos /research/ page (preferred - has weights)
scrape:
	npm run scrape-active

# Scrape historical ideas from Bravos /ideas/ page (fallback - no weights)
scrape-ideas:
	npm run scrape

# Scrape active trades only
scrape-active:
	npm run scrape-active

# Normalize raw scraped data
normalize:
	.venv/bin/python -m src.parsing.normalize_ideas
	.venv/bin/python -m src.parsing.derive_positions

# Fetch Robinhood holdings and run reconciliation
reconcile:
	.venv/bin/python scripts/run-reconcile.py

# Generate reports only (from existing processed data)
report:
	.venv/bin/python -m src.reporting.markdown_report

# Full pipeline: scrape -> normalize -> reconcile -> report
all: scrape normalize reconcile report

# Clean generated data
clean:
	rm -rf data/raw/*.json
	rm -rf data/processed/*.json
	rm -rf data/reports/*.md
	rm -rf data/reports/*.csv
