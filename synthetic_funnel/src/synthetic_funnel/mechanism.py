"""Synthetic two-sided acceptance model; coefficients are scenario assumptions."""
import math
from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True)
class Mechanism:
    salary_penalties: tuple = (3.0, 1.0, 4.0)
    skill_shortfall_penalties: tuple = (0.0, 4.0, 2.0)
    overqualification_penalties: tuple = (1.5, 0.5, 2.0)
    remote_penalties: tuple = (1.0, 0.5, 1.5)
    hidden_std: float = 0.5
    decision_ng_gap_strength: float = 6.0
    decision_withdrawal_gap_strength: float = 6.0
    decision_gap_tolerance: float = 0.05
    decision_gap_power: float = 1.0
    other_applications_penalties: tuple = (0.5, 0.3, 0.8)
    competing_candidates_penalties: tuple = (0.0, 0.5, 0.7)

    def __post_init__(self):
        for name in ('salary_penalties', 'skill_shortfall_penalties',
                     'overqualification_penalties', 'remote_penalties',
                     'other_applications_penalties', 'competing_candidates_penalties'):
            values = getattr(self, name)
            if len(values) != 3 or any(type(x) not in (int, float) or not math.isfinite(x) or x < 0 for x in values):
                raise ValueError(f'{name} must contain three finite nonnegative values')
            object.__setattr__(self, name, tuple(values))
        if type(self.hidden_std) not in (int, float) or not math.isfinite(self.hidden_std) or self.hidden_std < 0:
            raise ValueError('hidden_std must be finite and nonnegative')
        for name in ('decision_ng_gap_strength', 'decision_withdrawal_gap_strength',
                     'decision_gap_tolerance', 'decision_gap_power'):
            value = getattr(self, name)
            if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
                raise ValueError(f'{name} must be finite and nonnegative')
        if self.decision_gap_tolerance > 1 or self.decision_gap_power < 1:
            raise ValueError('gap tolerance must be <= 1; gap power must be >= 1')


def pair_features(seeker_skill, required_skill, desired_salary, offered_salary,
                  wants_remote, allows_remote):
    for value in (seeker_skill, required_skill):
        if not math.isfinite(value) or not 0 <= value <= 1:
            raise ValueError('Skill levels must be in [0, 1]')
    for value in (desired_salary, offered_salary):
        if not math.isfinite(value) or value <= 0:
            raise ValueError('Salaries must be finite and positive')
    if wants_remote not in (0, 1) or allows_remote not in (0, 1):
        raise ValueError('Remote flags must be 0 or 1')
    return {'seeker_skill': seeker_skill, 'required_skill': required_skill,
                'desired_salary': desired_salary, 'offered_salary': offered_salary,
                'wants_remote': int(wants_remote), 'allows_remote': int(allows_remote),
                'skill_match': 1 - abs(seeker_skill - required_skill),
                'skill_shortfall': max(required_skill - seeker_skill, 0),
                'overqualification': max(seeker_skill - required_skill, 0),
                'salary_shortfall': max((desired_salary - offered_salary) / desired_salary, 0),
                'remote_mismatch': int(wants_remote and not allows_remote)}


def gap_penalties(application, screening, model):
    """Return candidate-withdrawal and employer-NG logit penalties."""
    delta = np.asarray(application) - np.asarray(screening)
    ng = model.decision_ng_gap_strength * np.maximum(delta - model.decision_gap_tolerance, 0) ** model.decision_gap_power
    withdrawal = model.decision_withdrawal_gap_strength * np.maximum(-delta - model.decision_gap_tolerance, 0) ** model.decision_gap_power
    return withdrawal, ng


def configured_gap_penalties(application, screening, config):
    # Compare each probability with its own population target.
    application = np.asarray(application) / config.base_probabilities[0]
    screening = np.asarray(screening) / config.base_probabilities[1]
    return gap_penalties(application, screening, config.mechanism)


def bound_stage_probability(probability, config, stage):
    base = config.base_probabilities[stage]
    width = config.probability_half_widths[stage]
    return base - width + 2 * width * probability


def bounded_gates(candidate_logit, employer_logit, config, stage):
    """Bound the stage probability while retaining the two-sided failure split."""
    log_c = -np.logaddexp(0, -np.asarray(candidate_logit))
    log_e = np.zeros_like(log_c) if stage == 0 else -np.logaddexp(0, -np.asarray(employer_logit))
    raw = np.exp(log_c + log_e)
    p = bound_stage_probability(raw, config, stage)
    # Preserve each side's share of log failure risk, including extreme scores.
    denominator = log_c + log_e
    weight = np.divide(log_c, denominator, out=np.full_like(raw, .5), where=denominator != 0)
    if stage == 0:
        weight = np.ones_like(weight)
    candidate = np.exp(np.log(p) * weight)
    employer = np.exp(np.log(p) * (1 - weight))
    return candidate, employer


def score_components(config, features, seeker_motivation, employer_motivation):
    model = config.mechanism
    scores = []
    for i, effect in enumerate(config.skill_effects):
        match = effect * (np.asarray(features['skill_match']) - .5) / (1 if i == 0 else 2)
        c = (match + seeker_motivation - model.salary_penalties[i] * np.asarray(features['salary_shortfall'])
             - model.overqualification_penalties[i] * np.asarray(features['overqualification'])
             - model.remote_penalties[i] * np.asarray(features['remote_mismatch']))
        e = match + employer_motivation - model.skill_shortfall_penalties[i] * np.asarray(features['skill_shortfall'])
        c = c - model.other_applications_penalties[i] * np.log1p(features.get('other_applications', 0))
        e = e - model.competing_candidates_penalties[i] * np.log1p(features.get('competing_candidates', 0))
        scores.append(np.broadcast_arrays(c, e))
    return scores


def array_sigmoid(values):
    return np.exp(-np.logaddexp(0, -np.asarray(values)))


def probability_arrays(config, features, seeker_motivation=0.0, employer_motivation=0.0, intercepts=None):
    """Return vectorized probabilities with shape (..., stage, side)."""
    if intercepts is None:
        from .rates import population_rates
        intercepts = population_rates(config).intercepts
    scores = score_components(config, features, seeker_motivation, employer_motivation)
    result = []
    for i, (c, e) in enumerate(scores):
        if i == 2:
            a = result[0][0] * result[0][1]
            s = result[1][0] * result[1][1]
            withdrawal, ng = configured_gap_penalties(a, s, config)
            c, e = c - withdrawal, e - ng
        candidate, employer = bounded_gates(c + intercepts[i], e + intercepts[i], config, i)
        result.append((candidate, employer))
    return np.stack([np.stack(sides, axis=-1) for sides in result], axis=-2)


def transition_probabilities(config, features, seeker_motivation=0.0, employer_motivation=0.0):
    if not all(math.isfinite(x) for x in (seeker_motivation, employer_motivation)):
        raise ValueError('Hidden motivations must be finite')
    return tuple(tuple(float(v) for v in pair) for pair in
                 probability_arrays(config, features, seeker_motivation, employer_motivation))
