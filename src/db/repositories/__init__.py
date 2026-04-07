"""
Database repositories for the investing automation platform.

Repositories provide a clean data access layer between business logic and the database.
"""

from src.db.repositories.approval_repository import ApprovalRepository
from src.db.repositories.execution_repository import ExecutionRepository
from src.db.repositories.idempotency_repository import IdempotencyRepository
from src.db.repositories.reconciliation_repository import ReconciliationRepository
from src.db.repositories.signal_repository import SignalRepository
from src.db.repositories.sleeve_repository import SleeveRepository
from src.db.repositories.state_repository import StateRepository, state_repository

__all__ = [
    "ApprovalRepository",
    "ExecutionRepository",
    "IdempotencyRepository",
    "ReconciliationRepository",
    "SignalRepository",
    "SleeveRepository",
    "StateRepository",
    "state_repository",
]
