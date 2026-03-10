"""
Normalize raw scraped ideas from Bravos into a clean schema.
"""

import json
import re
import hashlib
from datetime import datetime
from pathlib import Path
from typing import TypedDict, Optional
from glob import glob


class NormalizedIdea(TypedDict):
    idea_id: str
    date: Optional[str]
    symbol: Optional[str]
    side: str  # 'buy' | 'sell' | 'unknown'
    entry_price: Optional[float]
    target_price: Optional[float]
    stop_loss: Optional[float]
    relative_weight: Optional[float]  # e.g., 0.05 for 5%
    status: str  # 'open' | 'closed' | 'unknown'
    notes: Optional[str]
    source_url: str


RAW_DATA_DIR = Path("data/raw")
PROCESSED_DATA_DIR = Path("data/processed")


def parse_price(price_str: Optional[str]) -> Optional[float]:
    """Extract numeric price from string like '$123.45' or '123.45'"""
    if not price_str:
        return None

    # Remove currency symbols and whitespace
    cleaned = re.sub(r'[^\d.]', '', price_str)

    try:
        return float(cleaned) if cleaned else None
    except ValueError:
        return None


def parse_weight(weight_str: Optional[str]) -> Optional[float]:
    """
    Parse allocation weight from various formats:
    - '5%' -> 0.05
    - '5.5%' -> 0.055
    - '0.05' -> 0.05
    - '5' -> 0.05 (assumes percentage if > 1)
    """
    if not weight_str:
        return None

    # Remove whitespace
    cleaned = weight_str.strip()

    # Check for percentage sign
    is_percentage = '%' in cleaned
    cleaned = cleaned.replace('%', '').strip()

    try:
        value = float(cleaned)

        # If it had a % sign or value > 1, treat as percentage
        if is_percentage or value > 1:
            return value / 100

        return value
    except ValueError:
        return None


def parse_date(date_str: Optional[str]) -> Optional[str]:
    """Parse date string into ISO format"""
    if not date_str:
        return None

    # Common date formats to try
    formats = [
        "%Y-%m-%d",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%dT%H:%M:%SZ",
        "%Y-%m-%dT%H:%M:%S.%fZ",
        "%m/%d/%Y",
        "%m/%d/%y",
        "%B %d, %Y",
        "%b %d, %Y",
        "%d %B %Y",
        "%d %b %Y",
    ]

    cleaned = date_str.strip()

    for fmt in formats:
        try:
            dt = datetime.strptime(cleaned, fmt)
            return dt.strftime("%Y-%m-%d")
        except ValueError:
            continue

    # If none worked, return original
    return cleaned


def generate_idea_hash(idea: dict) -> str:
    """Generate a unique hash for deduplication"""
    key_parts = [
        idea.get("symbol", ""),
        idea.get("date", ""),
        idea.get("side", ""),
        idea.get("entry_price", ""),
    ]
    key_string = "|".join(str(p) for p in key_parts)
    return hashlib.md5(key_string.encode()).hexdigest()[:12]


def normalize_idea(raw_idea: dict) -> NormalizedIdea:
    """Convert raw scraped idea to normalized schema"""

    return {
        "idea_id": raw_idea.get("ideaId", generate_idea_hash(raw_idea)),
        "date": parse_date(raw_idea.get("date")),
        "symbol": raw_idea.get("symbol", "").upper() if raw_idea.get("symbol") else None,
        "side": raw_idea.get("side", "unknown"),
        "entry_price": parse_price(raw_idea.get("entryPrice")),
        "target_price": parse_price(raw_idea.get("targetPrice")),
        "stop_loss": parse_price(raw_idea.get("stopLoss")),
        "relative_weight": parse_weight(raw_idea.get("relativeWeight")),
        "status": raw_idea.get("status", "unknown"),
        "notes": raw_idea.get("notes"),
        "source_url": raw_idea.get("sourceUrl", ""),
    }


def load_latest_raw_data() -> list[dict]:
    """Load the most recent raw scraped data file"""
    raw_files = sorted(glob(str(RAW_DATA_DIR / "ideas-*.json")))

    if not raw_files:
        print("No raw data files found in data/raw/")
        return []

    latest_file = raw_files[-1]
    print(f"Loading raw data from: {latest_file}")

    with open(latest_file) as f:
        data = json.load(f)

    return data.get("ideas", [])


def dedupe_ideas(ideas: list[NormalizedIdea]) -> list[NormalizedIdea]:
    """Remove duplicate ideas based on hash"""
    seen = set()
    unique = []

    for idea in ideas:
        idea_hash = generate_idea_hash(idea)
        if idea_hash not in seen:
            seen.add(idea_hash)
            unique.append(idea)
        else:
            print(f"Skipping duplicate: {idea.get('symbol')} on {idea.get('date')}")

    return unique


def run_normalization() -> list[NormalizedIdea]:
    """Main entry point: load raw data, normalize, dedupe, save"""

    # Load raw data
    raw_ideas = load_latest_raw_data()
    print(f"Loaded {len(raw_ideas)} raw ideas")

    if not raw_ideas:
        return []

    # Normalize each idea
    normalized = [normalize_idea(raw) for raw in raw_ideas]

    # Filter out ideas without symbols (likely parsing errors)
    valid = [idea for idea in normalized if idea["symbol"]]
    print(f"Normalized {len(valid)} ideas with valid symbols")

    # Deduplicate
    unique = dedupe_ideas(valid)
    print(f"After deduplication: {len(unique)} unique ideas")

    # Save processed data
    PROCESSED_DATA_DIR.mkdir(parents=True, exist_ok=True)
    output_path = PROCESSED_DATA_DIR / "ideas.json"

    with open(output_path, "w") as f:
        json.dump({
            "ideas": unique,
            "count": len(unique),
            "processed_at": datetime.now().isoformat(),
        }, f, indent=2)

    print(f"Saved normalized ideas to {output_path}")

    return unique


if __name__ == "__main__":
    run_normalization()
