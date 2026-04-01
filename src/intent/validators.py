"""
Intent validators - check portfolio intents for issues.

Validators run after interpretation to catch:
- Data quality issues
- Suspicious patterns
- Missing required data
- Constraint violations

When issues are found, the intent is flagged for manual review.
"""

from dataclasses import dataclass
from typing import Protocol

import structlog

from src.signals.models import PortfolioIntent

logger = structlog.get_logger(__name__)


@dataclass
class ValidationResult:
    """Result from a single validation check."""

    valid: bool
    issue: str | None = None
    severity: str = "warning"  # 'warning' or 'error'


class IntentValidator(Protocol):
    """Protocol for intent validators."""

    def validate(self, intent: PortfolioIntent) -> ValidationResult:
        """
        Validate an intent.

        Returns:
            ValidationResult indicating if valid and any issues
        """
        ...


class WeightSumValidator:
    """
    Validates that target weights sum to approximately 1.0.

    Flags for review if weights are off by more than a threshold.
    """

    def __init__(self, tolerance: float = 0.05):
        self.tolerance = tolerance

    def validate(self, intent: PortfolioIntent) -> ValidationResult:
        total_weight = intent.total_weight

        if abs(total_weight - 1.0) > self.tolerance:
            return ValidationResult(
                valid=False,
                issue=f"Target weights sum to {total_weight:.2%}, expected ~100%",
                severity="error" if abs(total_weight - 1.0) > 0.2 else "warning",
            )

        return ValidationResult(valid=True)


class MinPositionsValidator:
    """
    Validates minimum number of positions.

    Empty or very sparse portfolios are suspicious.
    """

    def __init__(self, min_positions: int = 1):
        self.min_positions = min_positions

    def validate(self, intent: PortfolioIntent) -> ValidationResult:
        if intent.position_count < self.min_positions:
            return ValidationResult(
                valid=False,
                issue=f"Only {intent.position_count} positions, expected at least {self.min_positions}",
                severity="error" if intent.position_count == 0 else "warning",
            )

        return ValidationResult(valid=True)


class MaxPositionsValidator:
    """
    Validates maximum number of positions.

    Too many positions might indicate a parsing error.
    """

    def __init__(self, max_positions: int = 50):
        self.max_positions = max_positions

    def validate(self, intent: PortfolioIntent) -> ValidationResult:
        if intent.position_count > self.max_positions:
            return ValidationResult(
                valid=False,
                issue=f"Too many positions ({intent.position_count}), max expected is {self.max_positions}",
                severity="warning",
            )

        return ValidationResult(valid=True)


class SymbolValidator:
    """
    Validates that all symbols are valid ticker symbols.

    Basic format check - doesn't verify against a symbol database.
    """

    def validate(self, intent: PortfolioIntent) -> ValidationResult:
        invalid_symbols = []

        for alloc in intent.target_allocations:
            symbol = alloc.symbol

            # Basic validation: 1-5 uppercase letters/numbers
            if not symbol:
                invalid_symbols.append("(empty)")
            elif len(symbol) > 5:
                invalid_symbols.append(symbol)
            elif not symbol.isalnum():
                invalid_symbols.append(symbol)

        if invalid_symbols:
            return ValidationResult(
                valid=False,
                issue=f"Invalid symbols: {', '.join(invalid_symbols)}",
                severity="error",
            )

        return ValidationResult(valid=True)


class DuplicateSymbolValidator:
    """
    Validates that there are no duplicate symbols.
    """

    def validate(self, intent: PortfolioIntent) -> ValidationResult:
        symbols = [a.symbol for a in intent.target_allocations]
        unique_symbols = set(symbols)

        if len(symbols) != len(unique_symbols):
            duplicates = [s for s in unique_symbols if symbols.count(s) > 1]
            return ValidationResult(
                valid=False,
                issue=f"Duplicate symbols: {', '.join(duplicates)}",
                severity="error",
            )

        return ValidationResult(valid=True)


class MaxWeightValidator:
    """
    Validates that no single position exceeds a maximum weight.

    Very concentrated positions might indicate a parsing error.
    """

    def __init__(self, max_weight: float = 0.5):
        self.max_weight = max_weight

    def validate(self, intent: PortfolioIntent) -> ValidationResult:
        oversized = []

        for alloc in intent.target_allocations:
            if alloc.target_weight > self.max_weight:
                oversized.append(f"{alloc.symbol} ({alloc.target_weight:.1%})")

        if oversized:
            return ValidationResult(
                valid=False,
                issue=f"Oversized positions: {', '.join(oversized)}",
                severity="warning",
            )

        return ValidationResult(valid=True)


class ConfidenceValidator:
    """
    Validates that confidence is above a threshold.
    """

    def __init__(self, min_confidence: float = 0.5):
        self.min_confidence = min_confidence

    def validate(self, intent: PortfolioIntent) -> ValidationResult:
        if intent.confidence < self.min_confidence:
            return ValidationResult(
                valid=False,
                issue=f"Low confidence score: {intent.confidence:.1%}",
                severity="warning",
            )

        return ValidationResult(valid=True)


class IntentValidationPipeline:
    """
    Runs a pipeline of validators on an intent.

    Collects all validation issues and flags the intent for
    review if any errors are found.
    """

    def __init__(self, validators: list[IntentValidator] | None = None):
        """
        Initialize with validators.

        Args:
            validators: List of validators to run. If None, uses defaults.
        """
        if validators is None:
            validators = self._default_validators()

        self.validators = validators

    def _default_validators(self) -> list[IntentValidator]:
        """Return default set of validators."""
        return [
            WeightSumValidator(tolerance=0.05),
            MinPositionsValidator(min_positions=1),
            MaxPositionsValidator(max_positions=50),
            SymbolValidator(),
            DuplicateSymbolValidator(),
            MaxWeightValidator(max_weight=0.5),
            ConfidenceValidator(min_confidence=0.5),
        ]

    def validate(self, intent: PortfolioIntent) -> list[ValidationResult]:
        """
        Run all validators on the intent.

        Returns:
            List of all validation results (including passing ones)
        """
        log = logger.bind(intent_id=str(intent.id))

        results = []
        for validator in self.validators:
            result = validator.validate(intent)
            results.append(result)

            if not result.valid:
                log.warning(
                    "validation_failed",
                    validator=validator.__class__.__name__,
                    issue=result.issue,
                    severity=result.severity,
                )

        return results

    def validate_and_flag(self, intent: PortfolioIntent) -> tuple[bool, list[str]]:
        """
        Validate and automatically flag the intent if issues are found.

        Args:
            intent: The intent to validate

        Returns:
            Tuple of (is_valid, list_of_issues)
            If not valid, the intent is modified to require review.
        """
        results = self.validate(intent)

        issues = [r.issue for r in results if not r.valid and r.issue]
        errors = [r.issue for r in results if not r.valid and r.severity == "error" and r.issue]

        if errors:
            # Has errors - flag for review
            intent.flag_for_review("; ".join(errors))
            return False, issues

        if issues:
            # Only warnings - log but don't flag
            logger.info(
                "validation_warnings",
                intent_id=str(intent.id),
                warnings=issues,
            )

        return True, issues


# Singleton instance
_pipeline: IntentValidationPipeline | None = None


def get_validation_pipeline() -> IntentValidationPipeline:
    """Get the validation pipeline singleton."""
    global _pipeline
    if _pipeline is None:
        _pipeline = IntentValidationPipeline()
    return _pipeline


def validate_intent(intent: PortfolioIntent) -> tuple[bool, list[str]]:
    """
    Convenience function to validate an intent.

    Returns:
        Tuple of (is_valid, list_of_issues)
    """
    return get_validation_pipeline().validate_and_flag(intent)
