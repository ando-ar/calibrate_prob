"""Configurable introduced population; no outcome labels used to anchor rates."""
import math
import random
from dataclasses import dataclass


@dataclass(frozen=True)
class Population:
    job_count: int = 1000
    seeker_skill_alpha: float = 1.0
    seeker_skill_beta: float = 1.0
    required_skill_alpha: float = 1.0
    required_skill_beta: float = 1.0
    salary_floor: float = 200.0
    desired_salary_base: float = 300.0
    desired_salary_skill_slope: float = 800.0
    desired_salary_std: float = 100.0
    offered_salary_base: float = 300.0
    offered_salary_skill_slope: float = 800.0
    offered_salary_std: float = 150.0
    remote_preference_rate: float = 0.5
    remote_availability_rate: float = 0.5
    other_applications_mean: float = 2.0
    competing_candidates_mean: float = 5.0

    def __post_init__(self):
        if type(self.job_count) is not int or self.job_count < 1:
            raise ValueError('job_count must be a positive integer')
        for name, value in vars(self).items():
            if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
                raise ValueError(f'{name} must be finite and nonnegative')
            if (name.endswith(('_alpha', '_beta')) or name == 'salary_floor') and value == 0:
                raise ValueError(f'{name} must be positive')
            if name.endswith('_rate') and value > 1:
                raise ValueError(f'{name} must be in [0, 1]')


def make_jobs(config):
    p = config.population
    rng = random.Random(f'{config.seed}:jobs')
    jobs = []
    for _ in range(p.job_count):
        required = rng.betavariate(p.required_skill_alpha, p.required_skill_beta)
        salary = max(p.salary_floor, p.offered_salary_base + p.offered_salary_skill_slope * required
                     + rng.gauss(0, p.offered_salary_std))
        jobs.append((required, salary, int(rng.random() < p.remote_availability_rate)))
    return jobs


def draw_pair(config, rng, job):
    from .mechanism import pair_features
    p = config.population
    skill = rng.betavariate(p.seeker_skill_alpha, p.seeker_skill_beta)
    desired = max(p.salary_floor, p.desired_salary_base + p.desired_salary_skill_slope * skill
                  + rng.gauss(0, p.desired_salary_std))
    required, offered, remote = job
    features = pair_features(skill, required, desired, offered,
                             int(rng.random() < p.remote_preference_rate), remote)
    features['other_applications'] = poisson(rng, p.other_applications_mean)
    features['competing_candidates'] = poisson(rng, p.competing_candidates_mean)
    return features, rng.gauss(0, config.mechanism.hidden_std), rng.gauss(0, config.mechanism.hidden_std)


def poisson(rng, mean):
    # Independent chunks avoid exp(-mean) underflow for large means.
    result = 0
    while mean > 0:
        part = min(mean, 20.)
        threshold, product, count = math.exp(-part), 1., -1
        while product > threshold:
            product *= rng.random()
            count += 1
        result += count
        mean -= part
    return result
