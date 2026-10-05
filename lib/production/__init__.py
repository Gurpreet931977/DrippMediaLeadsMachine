"""
lib/production
==============
Phase 9.0: Production Scale Engine + Multi-Market Acquisition.
"""

from lib.production.market_config import MarketConfig, MarketRegistry
from lib.production.market_runner import (
    MarketRunner,
    RunState,
    BatchController,
    QuotaBudget,
    FailedCandidate,
    MarketScorecard,
)

__all__ = [
    "MarketConfig",
    "MarketRegistry",
    "MarketRunner",
    "RunState",
    "BatchController",
    "QuotaBudget",
    "FailedCandidate",
    "MarketScorecard",
]
