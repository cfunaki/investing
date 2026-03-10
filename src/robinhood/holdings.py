"""
Fetch current holdings from Robinhood.
"""

import json
from datetime import datetime
from pathlib import Path
from typing import TypedDict, Optional

import robin_stocks.robinhood as rh

from .auth import ensure_logged_in


class Holding(TypedDict):
    symbol: str
    quantity: float
    avg_cost: float
    current_price: float
    market_value: float
    current_pct: float  # Percentage of total portfolio
    unrealized_pl: float
    unrealized_pl_pct: float


class AccountInfo(TypedDict):
    portfolio_value: float
    buying_power: float
    cash: float
    positions_count: int


PROCESSED_DATA_DIR = Path("data/processed")


def get_holdings() -> list[Holding]:
    """
    Fetch all current stock positions from Robinhood.
    Returns list of holdings with calculated percentages.
    """
    if not ensure_logged_in():
        print("ERROR: Not logged in to Robinhood")
        return []

    print("Fetching holdings from Robinhood...")

    try:
        # Get all positions (stocks only)
        positions = rh.account.get_open_stock_positions()

        if not positions:
            print("No open positions found")
            return []

        holdings: list[Holding] = []
        total_value = 0.0

        for pos in positions:
            try:
                # Get stock info for symbol
                instrument_url = pos.get("instrument")
                instrument = rh.stocks.get_instrument_by_url(instrument_url)
                symbol = instrument.get("symbol", "UNKNOWN") if instrument else "UNKNOWN"

                # Parse position data
                quantity = float(pos.get("quantity", 0))
                avg_cost = float(pos.get("average_buy_price", 0))

                # Get current price
                quote = rh.stocks.get_latest_price(symbol)
                current_price = float(quote[0]) if quote and quote[0] else avg_cost

                # Calculate values
                market_value = quantity * current_price
                cost_basis = quantity * avg_cost
                unrealized_pl = market_value - cost_basis
                unrealized_pl_pct = (unrealized_pl / cost_basis * 100) if cost_basis > 0 else 0

                holdings.append({
                    "symbol": symbol,
                    "quantity": quantity,
                    "avg_cost": round(avg_cost, 4),
                    "current_price": round(current_price, 4),
                    "market_value": round(market_value, 2),
                    "current_pct": 0,  # Will calculate after getting total
                    "unrealized_pl": round(unrealized_pl, 2),
                    "unrealized_pl_pct": round(unrealized_pl_pct, 2),
                })

                total_value += market_value

            except Exception as e:
                print(f"Error processing position: {e}")
                continue

        # Calculate percentage of portfolio for each holding
        if total_value > 0:
            for holding in holdings:
                holding["current_pct"] = round(
                    holding["market_value"] / total_value, 4
                )

        # Sort by market value descending
        holdings.sort(key=lambda h: h["market_value"], reverse=True)

        print(f"Found {len(holdings)} positions worth ${total_value:,.2f}")

        return holdings

    except Exception as e:
        print(f"Error fetching holdings: {e}")
        return []


def get_account_info() -> Optional[AccountInfo]:
    """Get account summary information"""
    if not ensure_logged_in():
        return None

    try:
        # Get portfolio profile
        portfolio = rh.profiles.load_portfolio_profile()

        if not portfolio:
            return None

        # Get account for cash info
        account = rh.profiles.load_account_profile()
        buying_power = float(account.get("buying_power", 0)) if account else 0

        return {
            "portfolio_value": float(portfolio.get("equity", 0)),
            "buying_power": buying_power,
            "cash": float(portfolio.get("withdrawable_amount", 0)),
            "positions_count": int(portfolio.get("open_positions", 0)),
        }

    except Exception as e:
        print(f"Error fetching account info: {e}")
        return None


def save_holdings(holdings: list[Holding], account_info: Optional[AccountInfo] = None) -> str:
    """Save holdings to processed data directory"""
    PROCESSED_DATA_DIR.mkdir(parents=True, exist_ok=True)

    output_path = PROCESSED_DATA_DIR / "robinhood_holdings.json"

    data = {
        "holdings": holdings,
        "account": account_info,
        "total_value": sum(h["market_value"] for h in holdings),
        "fetched_at": datetime.now().isoformat(),
    }

    with open(output_path, "w") as f:
        json.dump(data, f, indent=2)

    print(f"Saved holdings to {output_path}")
    return str(output_path)


def load_holdings() -> tuple[list[Holding], Optional[AccountInfo]]:
    """Load previously saved holdings"""
    holdings_path = PROCESSED_DATA_DIR / "robinhood_holdings.json"

    if not holdings_path.exists():
        return [], None

    with open(holdings_path) as f:
        data = json.load(f)

    return data.get("holdings", []), data.get("account")


def fetch_and_save_holdings() -> list[Holding]:
    """Main entry point: fetch holdings and save to disk"""
    holdings = get_holdings()
    account_info = get_account_info()

    if holdings:
        save_holdings(holdings, account_info)

        # Print summary
        print("\nRobinhood Holdings:")
        print("-" * 60)
        for h in holdings:
            pct_str = f"{h['current_pct']:.1%}".rjust(6)
            value_str = f"${h['market_value']:,.2f}".rjust(12)
            pl_str = f"{h['unrealized_pl_pct']:+.1f}%".rjust(8)
            print(f"  {h['symbol'].ljust(6)} {pct_str} {value_str} {pl_str}")
        print("-" * 60)

        if account_info:
            print(f"  Portfolio Value: ${account_info['portfolio_value']:,.2f}")
            print(f"  Buying Power:    ${account_info['buying_power']:,.2f}")

    return holdings


if __name__ == "__main__":
    fetch_and_save_holdings()
