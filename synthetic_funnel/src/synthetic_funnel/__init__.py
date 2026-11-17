"""Public API for a standalone synthetic referral funnel."""
from .generator import VERSION as __version__
from .generator import Config, Delay, generate, write_dataset
from .legacy_scores import LegacyScores
from .market import MarketConfig
from .market_export import write_market_dataset
from .mechanism import Mechanism, pair_features, transition_probabilities
from .population import Population
from .rates import population_rates

__all__ = [
    "Config", "Delay", "LegacyScores", "MarketConfig", "Mechanism", "Population", "__version__", "generate",
    "pair_features", "population_rates", "transition_probabilities", "write_dataset", "write_market_dataset",
]
