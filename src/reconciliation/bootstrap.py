"""
Bootstrap the sleeve virtual ledger from actual Robinhood holdings.

Cross-references Bravos target symbols with RH positions to seed
sleeve_positions with actual share quantities.
"""

import json
import logging
from decimal import Decimal
from pathlib import Path

from src.brokers.robinhood import get_robinhood_adapter
from src.db.repositories.sleeve_position_repository import sleeve_position_repository
from src.db.repositories.sleeve_repository import sleeve_repository
from src.db.session import get_db_context
from src.reconciliation.delta_reconciler import parse_bravos_weights
from src.signals.bravos_processor import BRAVOS_TRADES_PATH

logger = logging.getLogger(__name__)


async def bootstrap_ledger(force: bool = False) -> dict:
    """
    Bootstrap sleeve_positions from RH holdings × Bravos targets.

    Only seeds NEW symbols — never overwrites existing ledger positions,
    since those are maintained by trade execution and may not reflect
    total RH holdings (e.g., user holds same stock outside Bravos).

    Args:
        force: If False, skips entirely when ledger has any positions.
               If True, runs but still only seeds new symbols.

    Returns:
        Dict with bootstrap results (seeded count, already tracked, errors).
    """
    # Get bravos sleeve
    async with get_db_context() as db:
        sleeve = await sleeve_repository.get_by_name(db, "bravos")
        if not sleeve:
            logger.warning("bootstrap_no_bravos_sleeve")
            return {"error": "No bravos sleeve found in database"}

        sleeve_id = sleeve.id

        # Check if ledger already has positions (skip unless forced)
        if not force:
            positions = await sleeve_position_repository.get_position_map(db, sleeve_id)
            if positions:
                logger.info("bootstrap_skipped_ledger_has_positions", extra={"count": len(positions)})
                return {"skipped": True, "existing_positions": len(positions)}

    # Get Bravos target symbols
    bravos_symbols = set()
    bravos_weights = {}
    if BRAVOS_TRADES_PATH.exists():
        with open(BRAVOS_TRADES_PATH) as f:
            bravos_data = json.load(f)
        bravos_weights = parse_bravos_weights(bravos_data)
        bravos_symbols = set(bravos_weights.keys())
    else:
        logger.warning("bootstrap_no_bravos_data", extra={"path": str(BRAVOS_TRADES_PATH)})
        return {"error": "No bravos_trades.json found. Run a scrape first."}

    if not bravos_symbols:
        return {"error": "No Bravos symbols found"}

    logger.info("bootstrap_bravos_symbols", extra={"symbols": sorted(bravos_symbols)})

    # Get RH holdings
    broker = get_robinhood_adapter()
    if not await broker.is_connected():
        connected = await broker.connect()
        if not connected:
            logger.warning("bootstrap_rh_not_connected")
            return {"error": "Cannot connect to Robinhood. Use /login first."}

    rh_positions = await broker.get_positions()
    rh_by_symbol = {p.symbol: p for p in rh_positions}

    logger.info("bootstrap_rh_positions", extra={"count": len(rh_positions)})

    # Get existing ledger positions (to avoid overwriting trade-tracked data)
    async with get_db_context() as db:
        existing_positions = await sleeve_position_repository.get_position_map(db, sleeve_id)

    # Cross-reference and seed — only NEW symbols, never overwrite existing
    seeded = []
    already_tracked = []
    not_in_rh = []

    async with get_db_context() as db:
        for symbol in bravos_symbols:
            # Skip symbols already in the ledger — their shares are
            # maintained by trade execution and may differ from total RH
            # holdings (e.g., if the user also holds the stock outside Bravos)
            if symbol in existing_positions:
                already_tracked.append(symbol)
                continue

            rh_pos = rh_by_symbol.get(symbol)
            if rh_pos and rh_pos.quantity > 0:
                weight = bravos_weights.get(symbol, Decimal(0))
                await sleeve_position_repository.upsert_position(
                    db=db,
                    sleeve_id=sleeve_id,
                    symbol=symbol,
                    shares=Decimal(str(rh_pos.quantity)),
                    weight=weight,
                    cost_basis=Decimal(str(rh_pos.quantity * rh_pos.average_cost)),
                )
                seeded.append(symbol)
                logger.info(
                    "bootstrap_seeded_position",
                    extra={
                        "symbol": symbol,
                        "shares": rh_pos.quantity,
                        "weight": float(weight),
                    },
                )
            else:
                not_in_rh.append(symbol)

    result = {
        "seeded": len(seeded),
        "seeded_symbols": sorted(seeded),
        "already_tracked": sorted(already_tracked),
        "not_in_rh": sorted(not_in_rh),
    }
    logger.info("bootstrap_complete", extra=result)
    return result
