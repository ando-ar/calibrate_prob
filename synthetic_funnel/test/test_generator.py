import csv
import json
import math
import random
import statistics
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime
from pathlib import Path

from synthetic_funnel import (
    Config,
    Delay,
    LegacyScores,
    Mechanism,
    generate,
    pair_features,
    transition_probabilities,
    write_dataset,
)
from synthetic_funnel.generator import file_hash


class GeneratorTest(unittest.TestCase):
    def test_reproducible_and_seed_changes_output(self):
        config = Config(rows=30, seed=3, legacy_scores=LegacyScores(enabled=False))
        a = list(generate(config))
        self.assertEqual(a, list(generate(config)))
        self.assertNotEqual(a, list(generate(replace(config, seed=4))))

    def test_truncated_normal_moments_and_constant(self):
        rng = random.Random(123)
        for mean, std in ((3, 2), (5, 4), (30, 20)):
            samples = [Delay(mean, std).sample(rng) for _ in range(20000)]
            alpha = -mean / std
            ratio = math.exp(-alpha * alpha / 2) / math.sqrt(2 * math.pi) / (
                0.5 * math.erfc(alpha / math.sqrt(2)))
            expected_mean = mean + std * ratio
            expected_var = std * std * (1 + alpha * ratio - ratio * ratio)
            self.assertGreaterEqual(min(samples), 0)
            self.assertAlmostEqual(statistics.mean(samples), expected_mean,
                                   delta=6 * math.sqrt(expected_var / len(samples)))
            self.assertAlmostEqual(statistics.pvariance(samples), expected_var,
                                   delta=expected_var * 0.06)
        self.assertEqual(Delay(0, 0).sample(rng), 0)
        self.assertEqual(Delay(5, 0).sample(rng), 5)

    def test_order_censoring_and_conditional_rates(self):
        config = Config(rows=20000, referral_start='2024-01-01T00:00:00+09:00',
                        referral_end='2024-01-01T00:00:00+09:00',
                        as_of='2024-01-02T00:00:00+09:00', skill_effects=(0, 0, 0),
                        legacy_scores=LegacyScores(enabled=False))
        counts = [0, 0, 0]
        expected = [0.0, 0.0, 0.0]
        variance = [0.0, 0.0, 0.0]
        cutoff = datetime.fromisoformat(config.as_of)
        censored = 0
        for row, truth in generate(config):
            previous = datetime.fromisoformat(row['shoukai_dt'])
            failed = False
            for i, col in enumerate(('oubo_dt', 'shorui_tsuuka_dt', 'kettei_dt')):
                if not failed:
                    p = truth[f'p_{("application", "screening", "decision")[i]}_eventual']
                    expected[i] += p
                    variance[i] += p * (1 - p)
                latent = truth['latent_' + col]
                self.assertFalse(failed and latent)
                if latent:
                    moment = datetime.fromisoformat(latent)
                    self.assertGreaterEqual(moment, previous)
                    previous = moment
                    counts[i] += 1
                    self.assertEqual(bool(row[col]), moment <= cutoff)
                    censored += not bool(row[col])
                else:
                    failed = True
                    self.assertEqual(row[col], '')
            self.assertNotIn('p_application_eventual', row)
        self.assertGreater(censored, 0)
        for hits, mean, var in zip(counts, expected, variance):
            self.assertAlmostEqual(hits, mean, delta=6 * math.sqrt(var))

    def test_matching_and_conditions(self):
        config = Config(mechanism=Mechanism(decision_ng_gap_strength=0, decision_withdrawal_gap_strength=0))
        good = pair_features(.7, .7, 800, 800, 1, 1)
        short_skill = pair_features(.2, .7, 800, 800, 1, 1)
        bad_salary = pair_features(.7, .7, 800, 400, 1, 1)
        bad_remote = pair_features(.7, .7, 800, 800, 1, 0)
        overqualified = pair_features(1, .7, 800, 800, 1, 1)
        reference = transition_probabilities(config, good)
        for bad in (short_skill, bad_salary, bad_remote, overqualified):
            changed = transition_probabilities(config, bad)
            for (c, e), (bc, be) in zip(reference, changed):
                self.assertGreater(c * e, bc * be)
        self.assertGreater(reference[1][1], transition_probabilities(config, short_skill)[1][1])
        for index in range(3):
            self.assertGreater(reference[index][0], transition_probabilities(config, bad_salary)[index][0])
        self.assertGreater(transition_probabilities(config, good, 1, 1)[2][0], reference[2][0])

    def test_truth_is_separate_and_job_attributes_stable(self):
        jobs = {}
        reasons = set()
        for row, truth in generate(Config(rows=3000, legacy_scores=LegacyScores(enabled=False))):
            self.assertEqual(set(row) & set(truth), {'event_id'})
            job = tuple(row[k] for k in ('required_skill', 'offered_salary', 'allows_remote'))
            self.assertEqual(jobs.setdefault(row['item_id'], job), job)
            reasons.add(truth['failure_reason'])
            for stage in ('application', 'screening', 'decision'):
                self.assertEqual(truth[f'p_{stage}_eventual'],
                                 truth[f'p_candidate_{stage}'] * truth[f'p_employer_{stage}'])
        self.assertTrue({'not_applied', 'employer_rejection', 'candidate_withdrawal'} <= reasons)

    def test_mechanism_configuration(self):
        config = Config.from_dict({'mechanism': {'hidden_std': 0, 'salary_penalties': [0, 0, 0]}})
        low = pair_features(.5, .5, 800, 400, 0, 0)
        high = pair_features(.5, .5, 800, 800, 0, 0)
        self.assertEqual(transition_probabilities(config, low), transition_probabilities(config, high))
        with self.assertRaises(ValueError):
            Mechanism(hidden_std=-1)
        with self.assertRaises(ValueError):
            Mechanism(salary_penalties=(1, 2))

    def test_invalid_config(self):
        for kwargs in ({'rows': 0}, {'seed': True}, {'base_probabilities': (0, .5, .5)},
                       {'skill_effects': (float('nan'), 0, 0)},
                       {'as_of': '2023-01-01T00:00:00+09:00'}, {'as_of': '2026-01-01'}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                Config.from_dict(kwargs)
        for args in ((-1, 2), (1, -2), (float('inf'), 1)):
            with self.assertRaises(ValueError):
                Delay(*args)

    def test_export_round_trip_hash_and_no_overwrite(self):
        scratch = Path(__file__).resolve().parents[1] / 'runs'
        scratch.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=scratch) as folder:
            output = Path(folder) / 'dataset'
            config = Config.from_dict({
                'rows': 20, 'application_delay': {'mean_days': 3, 'std_days': 0},
                'legacy_scores': {'enabled': False},
            })
            manifest = write_dataset(config, output, output_format="csv")
            self.assertEqual(Config.from_dict(manifest['config']), config)
            for name, expected in manifest['files'].items():
                self.assertEqual(file_hash(output / name), expected)
            with (output / 'events.csv').open(encoding='utf-8', newline='') as stream:
                self.assertEqual(len(list(csv.DictReader(stream))), 20)
            self.assertEqual(json.loads((output / 'manifest.json').read_text()), manifest)
            with self.assertRaises(FileExistsError):
                write_dataset(config, output)


if __name__ == '__main__':
    unittest.main()
