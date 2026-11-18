"""Public API for the standalone continuous-market synthetic referral funnel."""
from .market import MarketConfig, OCCUPATIONS, REGIONS
from .market_export import write_market_dataset
from .mechanism import Mechanism, pair_features, transition_probabilities
from .population import Population
from .rates import RateAnchor, population_rates
from .shared import VERSION as __version__

__all__ = [
    "MarketConfig", "Mechanism", "OCCUPATIONS", "Population", "RateAnchor", "REGIONS", "__version__",
    "pair_features", "population_rates", "transition_probabilities", "write_market_dataset",
]
