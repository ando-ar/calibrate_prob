"""Phase-specific past windows and bounded-memory uniform negative sampling."""
import hashlib
import heapq
import random
from dataclasses import asdict, replace
from datetime import timedelta

import numpy as np
from sklearn.linear_model import LogisticRegression
from sklearn.preprocessing import StandardScaler


def fit_window_models(config):
    from .generator import FEATURE_FIELDS, _generate_unscored, parse_datetime
    from .trained_scores import deadline
    cutoff=parse_datetime(config.referral_start)
    end=cutoff-timedelta(days=100)
    models=[]
    histories=[]
    for stage,days in enumerate(config.legacy_scores.training_window_days):
        window_start=end-timedelta(days=days)
        # Prior introductions can enter the stage during the requested window.
        # A common three-calendar-month horizon is at most 92 days here.
        lookback=days+(92 if stage else 0)
        n=lookback*config.legacy_scores.training_referrals_per_day
        history=replace(config,rows=n,seed=config.seed+config.legacy_scores.seed+1000003+stage,
                        referral_start=(end-timedelta(days=lookback)).isoformat(),referral_end=end.isoformat(),as_of=cutoff.isoformat(),
                        legacy_scores=replace(config.legacy_scores,enabled=False))
        rng=random.Random(config.legacy_scores.seed+stage)
        positives=[]
        negatives=[]
        population=0
        negative_count=0
        cap=config.legacy_scores.negative_reservoir_size
        for i,(row,_) in enumerate(_generate_unscored(history)):
            horizon=deadline(parse_datetime(row['shoukai_dt']))
            if horizon>cutoff:
                raise ValueError('Immature historical outcome')
            dates=[parse_datetime(row[k]) if row[k] else None for k in ('oubo_dt','shorui_tsuuka_dt','kettei_dt')]
            reached=[v is not None and v<=horizon for v in dates]
            if stage and not reached[stage-1]:
                continue
            entry=dates[stage-1] if stage else parse_datetime(row['shoukai_dt'])
            if not window_start<=entry<end:
                continue
            population+=1
            features=tuple(float(row[k]) for k in FEATURE_FIELDS)
            if reached[stage]:
                positives.append((i,features))
                if len(positives)>cap:
                    raise ValueError('Increase negative_reservoir_size to retain every positive')
            else:
                negative_count+=1
                # Retain the cap smallest independent random priorities.
                priority=rng.random()
                item=(-priority,i,features)
                if len(negatives)<cap:
                    heapq.heappush(negatives,item)
                elif item[0]>negatives[0][0]:
                    heapq.heapreplace(negatives,item)
        count=len(positives)
        if count<2 or negative_count<count:
            raise ValueError('Too few positive or negative outcomes; increase window or density')
        # Smallest count priorities are a uniform sample of all negatives.
        chosen=sorted(negatives,key=lambda v:-v[0])[:count]
        records=sorted([(i,f,1) for i,f in positives]+[(i,f,0) for _,i,f in chosen])
        x=np.array([f for _,f,_ in records])
        y=np.array([label for _,_,label in records])
        ids=np.array([i for i,_,_ in records],dtype='<i8')
        scaler=StandardScaler().fit(x)
        model=LogisticRegression(C=1.,max_iter=2000).fit(scaler.transform(x),y)
        if model.n_iter_.max()>=2000:
            raise ValueError('Legacy model did not converge')
        models.append({
            'features': list(FEATURE_FIELDS), 'mean': scaler.mean_.tolist(), 'scale': scaler.scale_.tolist(),
            'coefficients': model.coef_[0].tolist(), 'intercept': float(model.intercept_[0]),
            'population_rows': population, 'population_positives': count,
            'selected_positives': count, 'selected_negatives': count,
            'negative_sampling_probability': count / negative_count, 'training_row_indices': ids.tolist(),
            'rows_sha256': hashlib.sha256(ids.tobytes()).hexdigest(),
            'source_referrals': n, 'window_days': days,
            'risk_window_start': window_start.isoformat(), 'risk_window_end': end.isoformat(),
            'window_origin': ('referral', 'application', 'screening')[stage], 'training_config': asdict(history),
        })
        histories.append(asdict(history))
    return {
        'method': 'balanced_logistic_regression', 'training_configs_by_stage': histories,
        'training_cutoff': cutoff.isoformat(), 'sampling_correction': False, 'class_weights': 'none',
        'label': 'common 3 calendar months from referral; phase-specific reached risk sets', 'models': models,
    }
