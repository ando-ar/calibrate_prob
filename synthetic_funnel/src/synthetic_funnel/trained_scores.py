"""Past-only balanced classifiers, trained on observed mature outcome labels."""
import calendar
import hashlib
from dataclasses import asdict, replace
from datetime import timedelta
from functools import lru_cache

import numpy as np
from sklearn.linear_model import LogisticRegression
from sklearn.preprocessing import StandardScaler


def deadline(t):
    month = t.month+3
    year = t.year+(month-1)//12
    month = (month-1)%12+1
    return t.replace(year=year,month=month,day=min(t.day,calendar.monthrange(year,month)[1]))


@lru_cache(maxsize=16)
def fit_models(config):
    if config.legacy_scores.training_window_days is not None:
        from .window_training import fit_window_models
        return fit_window_models(config)
    from .generator import FEATURE_FIELDS, _generate_unscored, parse_datetime
    cutoff = parse_datetime(config.referral_start)
    # Five months ensure every referral's common three-month label is mature.
    end = cutoff-timedelta(days=100)
    history = replace(config,rows=config.legacy_scores.training_rows,
                      seed=config.seed+config.legacy_scores.seed+1000003,
                      referral_start=(end-timedelta(days=730)).isoformat(),
                      referral_end=end.isoformat(),as_of=cutoff.isoformat(),
                      legacy_scores=replace(config.legacy_scores,enabled=False))
    x = np.empty((history.rows, len(FEATURE_FIELDS)))
    y = np.zeros((history.rows,3),dtype=np.int8)
    for i,(row,_) in enumerate(_generate_unscored(history)):
        start = parse_datetime(row['shoukai_dt'])
        horizon = deadline(start)
        if horizon>cutoff:
            raise ValueError('Immature historical label')
        x[i] = [row[k] for k in FEATURE_FIELDS]
        y[i] = [int(bool(row[k]) and parse_datetime(row[k])<=horizon) for k in ('oubo_dt','shorui_tsuuka_dt','kettei_dt')]
    rng = np.random.default_rng(config.legacy_scores.seed)
    models=[]
    for stage in range(3):
        risk = np.ones(history.rows,dtype=bool) if stage==0 else y[:,stage-1]==1
        positive = np.flatnonzero(risk & (y[:,stage]==1))
        negative = np.flatnonzero(risk & (y[:,stage]==0))
        if len(positive)<2 or len(negative)<len(positive):
            raise ValueError('Insufficient historical classes for 1:1 negative downsampling')
        selected = np.sort(np.r_[positive,rng.choice(negative,len(positive),replace=False)])
        scaler=StandardScaler().fit(x[selected])
        model=LogisticRegression(C=1.,max_iter=2000).fit(scaler.transform(x[selected]),y[selected,stage])
        if model.n_iter_.max()>=2000:
            raise ValueError('Legacy model did not converge')
        models.append({
            'features': list(FEATURE_FIELDS), 'mean': scaler.mean_.tolist(), 'scale': scaler.scale_.tolist(),
            'coefficients': model.coef_[0].tolist(), 'intercept': float(model.intercept_[0]),
            'population_rows': int(risk.sum()), 'population_positives': len(positive),
            'selected_positives': len(positive), 'selected_negatives': len(positive),
            'negative_sampling_probability': float(len(positive) / len(negative)),
            'training_row_indices': selected.tolist(),
            'rows_sha256': hashlib.sha256(selected.astype('<i8').tobytes()).hexdigest(),
        })
    return {
        'method': 'balanced_logistic_regression', 'training_config': asdict(history),
        'label': 'common 3 calendar months from referral; screening risk set is within-horizon applicants',
        'training_cutoff': cutoff.isoformat(), 'class_weights': 'none', 'sampling_correction': False,
        'models': models,
    }


def predict_scores(rows,bundle):
    result=[]
    for model in bundle['models']:
        x=np.array([[row[k] for k in model['features']] for row in rows])
        z=((x-model['mean'])/model['scale'])@np.array(model['coefficients'])+model['intercept']
        result.append(np.exp(-np.logaddexp(0,-z)))
    return np.column_stack(result)


def generate_scored(config,raw_factory):
    bundle=fit_models(config)
    batch=[]
    def scored(items):
        scores=predict_scores([row for row,_ in items],bundle)
        for (row,truth),score in zip(items,scores):
            row['legacy_apply_score'],row['legacy_screen_score'],row['legacy_decision_score']=map(float,score)
            yield row,truth
    for item in raw_factory(config):
        batch.append(item)
        if len(batch)==8192:
            yield from scored(batch)
            batch=[]
    if batch:
        yield from scored(batch)
