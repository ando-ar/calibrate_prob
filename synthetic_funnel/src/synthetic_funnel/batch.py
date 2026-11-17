"""Bounded-memory vectorized probability evaluation for million-row datasets."""
import random
from datetime import timedelta

import numpy as np

from .mechanism import probability_arrays
from .population import draw_pair, make_jobs
from .rates import population_rates


def pair_batches(config, batch_size=8192):
    anchor = population_rates(config).intercepts
    rng = random.Random(f'{config.seed}:pairs')
    jobs = make_jobs(config)
    for start in range(0, config.rows, batch_size):
        count = min(batch_size, config.rows-start)
        rows, a, b = [], [], []
        for i in range(start, start+count):
            f, candidate, employer = draw_pair(config, rng, jobs[i % len(jobs)])
            rows.append(f)
            a.append(candidate)
            b.append(employer)
        arrays = {key: np.array([row[key] for row in rows]) for key in rows[0]}
        gates = probability_arrays(config, arrays, np.array(a), np.array(b), anchor)
        yield start, rows, a, b, gates


def generate_batch(config):
    from .generator import DATE_COLUMNS, STAGES, parse_datetime
    date_rng = random.Random(f'{config.seed}:dates')
    outcome_rng = random.Random(f'{config.seed}:outcomes')
    start_date, end_date, cutoff = map(parse_datetime, (config.referral_start,config.referral_end,config.as_of))
    delays = config.application_delay,config.screening_delay,config.decision_delay
    for start, rows, affinities_a, affinities_b, gates in pair_batches(config):
        for offset, f in enumerate(rows):
            i = start+offset
            referral = start_date+(end_date-start_date)*date_rng.random()
            pair = gates[offset]
            row = dict(event_id=f'synthetic-{i:09d}',user_id=f'user-{i:09d}',
                       item_id=f'item-{i % config.population.job_count:04d}',**f,shoukai_dt=referral.isoformat())
            pa, ps = float(pair[0].prod()), float(pair[1].prod())
            truth = {'event_id': row['event_id'],'seeker_affinity': affinities_a[offset],
                         'employer_affinity': affinities_b[offset],'failure_stage': '','failure_reason': '',
                         'application_screening_gap': pa-ps,
                         'relative_application_screening_gap': pa/config.base_probabilities[0]-ps/config.base_probabilities[1]}
            current = referral
            for stage, column, gate, delay in zip(STAGES,DATE_COLUMNS,pair,delays):
                c,e = map(float,gate)
                truth[f'p_{stage}_eventual'] = c*e
                truth[f'p_candidate_{stage}'] = c
                truth[f'p_employer_{stage}'] = e
                if current is not None:
                    employer_ok = outcome_rng.random() < e
                    candidate_ok = outcome_rng.random() < c
                    if employer_ok and candidate_ok:
                        current += timedelta(days=delay.sample(outcome_rng))
                    else:
                        truth['failure_stage'] = stage
                        truth['failure_reason'] = ('employer_rejection' if not employer_ok else
                                                   'not_applied' if stage=='application' else 'candidate_withdrawal')
                        current = None
                truth[f'latent_{column}'] = current.isoformat() if current is not None else ''
                row[column] = current.isoformat() if current is not None and current<=cutoff else ''
            yield row,truth
