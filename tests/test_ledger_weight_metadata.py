"""
Regression tests for the ledger weight-metadata bug.

Root cause: BravosSignalProcessor built ProposedTrade without target_weight /
delta_weight, so every executed buy wrote weight=NULL into sleeve_positions.
A NULL-weight position was then treated as "already at target" forever, making
sells and trims structurally impossible.
"""

from decimal import Decimal

import pytest

from src.reconciliation.delta_reconciler import DeltaReconciler, EmptyTargetsError
from src.reconciliation.trade_mapping import delta_trade_to_proposed


class FakePosition:
    """Stand-in for SleevePosition (avoids needing a DB session)."""

    def __init__(self, symbol, shares, weight=None, cost_basis=None):
        self.symbol = symbol
        self.shares = Decimal(str(shares))
        self.weight = None if weight is None else Decimal(str(weight))
        self.cost_basis = cost_basis


@pytest.fixture
def reconciler():
    return DeltaReconciler(unit_size=Decimal("207.00"))


# ---------------------------------------------------------------------------
# 1. The proposal must carry weight metadata through to the stored trade dict
# ---------------------------------------------------------------------------


def test_proposed_trade_carries_target_weight_for_buy(reconciler):
    """A buy proposal must preserve target_weight so the ledger write records it."""
    changes = reconciler._calculate_weight_changes({}, {"NVDA": Decimal(5)})
    trades = reconciler._generate_trades(changes, {})
    proposed = delta_trade_to_proposed(trades[0], {}, {})

    assert proposed.target_weight == 5.0
    assert proposed.delta_weight == 5.0
    assert proposed.to_dict()["target_weight"] == 5.0


def test_proposed_trade_carries_weights_for_partial_sell(reconciler):
    """A trim proposal must record the surviving target weight, not 0."""
    positions = {"NVDA": FakePosition("NVDA", 10, weight=8)}
    changes = reconciler._calculate_weight_changes(positions, {"NVDA": Decimal(3)})
    trades = reconciler._generate_trades(changes, positions)
    proposed = delta_trade_to_proposed(trades[0], {}, {})

    assert proposed.side == "sell"
    assert proposed.target_weight == 3.0
    assert proposed.current_weight == 8.0
    # A partial trim must NOT look like a full exit to the ledger updater
    assert proposed.to_dict()["target_weight"] != 0


# ---------------------------------------------------------------------------
# 2. A NULL-weight position must not be able to swallow an exit
# ---------------------------------------------------------------------------


def test_null_weight_position_dropped_by_provider_exits(reconciler):
    """
    A ledger position with weight=NULL that the provider no longer holds is an
    exit. Previously old_weight was set to new_weight (0), yielding "unchanged"
    and silently swallowing the sell.
    """
    positions = {"LSCC": FakePosition("LSCC", Decimal("5.635126"), weight=None)}
    changes = reconciler._calculate_weight_changes(positions, {"NVDA": Decimal(5)})

    by_symbol = {c.symbol: c for c in changes}
    assert "LSCC" in by_symbol, "exit for NULL-weight position was swallowed"
    assert by_symbol["LSCC"].action == "exit"

    trades = reconciler._generate_trades(changes, positions)
    sells = [t for t in trades if t.side == "sell"]
    assert len(sells) == 1
    assert sells[0].symbol == "LSCC"
    assert sells[0].quantity == Decimal("5.635126")


def test_null_weight_position_still_in_targets_is_left_alone(reconciler):
    """
    A NULL-weight position the provider still holds stays untouched — we must
    not propose re-entering something already owned.
    """
    positions = {"NVDA": FakePosition("NVDA", 10, weight=None)}
    changes = reconciler._calculate_weight_changes(positions, {"NVDA": Decimal(5)})
    assert [c for c in changes if c.symbol == "NVDA"] == []


# ---------------------------------------------------------------------------
# 3. Normal weight arithmetic still works
# ---------------------------------------------------------------------------


def test_weight_decrease_produces_proportional_sell(reconciler):
    positions = {"NVDA": FakePosition("NVDA", 10, weight=8)}
    changes = reconciler._calculate_weight_changes(positions, {"NVDA": Decimal(6)})
    trades = reconciler._generate_trades(changes, positions)

    assert len(trades) == 1
    assert trades[0].side == "sell"
    # 2 of 8 units removed -> 25% of shares
    assert trades[0].quantity == Decimal("2.500000")
    assert trades[0].target_weight == Decimal(6)


def test_weight_increase_produces_buy(reconciler):
    positions = {"NVDA": FakePosition("NVDA", 10, weight=3)}
    changes = reconciler._calculate_weight_changes(positions, {"NVDA": Decimal(5)})
    trades = reconciler._generate_trades(changes, positions)

    assert trades[0].side == "buy"
    assert trades[0].notional == Decimal("414.00")  # 2 units * 207
    assert trades[0].target_weight == Decimal(5)


# ---------------------------------------------------------------------------
# 4. An empty target set must never be reconciled against
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_empty_targets_aborts_instead_of_liquidating(reconciler, monkeypatch):
    """
    A scrape that returns zero positions must abort, not propose exiting every
    position in the sleeve.
    """
    from uuid import uuid4

    result = await reconciler.reconcile(sleeve_id=uuid4(), new_weights={})

    assert result.success is False
    assert result.empty_targets is True
    assert result.trades == []


def test_empty_targets_error_is_distinct_type():
    assert issubclass(EmptyTargetsError, Exception)
