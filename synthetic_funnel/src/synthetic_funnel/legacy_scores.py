"""Settings for models trained on balanced historical samples."""
from dataclasses import dataclass


@dataclass(frozen=True)
class LegacyScores:
    enabled: bool = True
    training_rows: int = 1000000
    training_window_days: tuple | None = None
    training_referrals_per_day: int = 10000
    negative_reservoir_size: int = 100000
    seed: int = 1931

    def __post_init__(self):
        if self.training_window_days is not None:
            if len(self.training_window_days)!=3 or any(type(v) is not int or v<1 for v in self.training_window_days):
                raise ValueError('training_window_days requires three positive integers')
            object.__setattr__(self,'training_window_days',tuple(self.training_window_days))
        if type(self.training_referrals_per_day) is not int or self.training_referrals_per_day<1 or type(self.negative_reservoir_size) is not int or self.negative_reservoir_size<1:
            raise ValueError('Training density and reservoir size must be positive integers')
        if type(self.training_rows) is not int or self.training_rows < 100:
            raise ValueError('training_rows must be an integer of at least 100')
        if type(self.enabled) is not bool or type(self.seed) is not int or self.seed < 0:
            raise ValueError('enabled must be bool and seed a nonnegative integer')
