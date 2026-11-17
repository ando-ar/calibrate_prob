"""Population-sizing pilot, not the production market/oracle generator.

Only application-positive attempts need scheduled events: nonapplications cannot
extend waiting after the application deadline. Full referral counts are retained.
"""
import argparse
import heapq
import json
import random
from dataclasses import dataclass,asdict,replace
from pathlib import Path

import numpy as np


@dataclass(frozen=True)
class Settings:
    seeker_arrivals: int = 50
    job_arrivals: int = 50
    application_days: int = 18
    posting_days: int = 40
    referrals_per_day: int = 5
    warmup_days: int = 180
    measure_days: int = 365
    seed: int = 41


def simulate(settings):
    rng=random.Random(settings.seed)
    seekers={}
    jobs={}
    cases={}
    queue=[]
    next_s=next_j=next_e=0
    lifetime_s=[]
    lifetime_j=[]
    stats=dict(referrals=0,applications=0,screenings=0,hires=0,natural_exits=0,expired_jobs=0,cancellations=0,zero_application_exits=0)
    daily=[]
    def delay(mu,sd):
        value=rng.gauss(mu,sd)
        while value<0:
            value=rng.gauss(mu,sd)
        return value
    def count(key,when):
        if when>=settings.warmup_days:
            stats[key]+=1
    def maybe_exit(sid,now):
        if sid in seekers and seekers[sid]['until']<=now and not seekers[sid]['pending']:
            s=seekers.pop(sid)
            if now>=settings.warmup_days:
                lifetime_s.append(now-s['birth'])
            count('natural_exits',now)
            if not s['applied']:
                count('zero_application_exits',now)
    def cancel(eid,now):
        c=cases.pop(eid,None)
        if c is None:
            return
        sid,jid=c['sid'],c['jid']
        if sid in seekers:
            seekers[sid]['pending'].discard(eid)
        if jid in jobs:
            jobs[jid]['pending'].discard(eid)
        count('cancellations',now)
        maybe_exit(sid,now)
    def close(eid,now):
        c=cases.pop(eid)
        sid,jid=c['sid'],c['jid']
        if sid in seekers:
            seekers[sid]['pending'].discard(eid)
        if jid in jobs:
            jobs[jid]['pending'].discard(eid)
        maybe_exit(sid,now)
    def process(until):
        while queue and queue[0][0]<until:
            now,_,eid,stage=heapq.heappop(queue)
            if eid not in cases:
                continue
            c=cases[eid]
            sid,jid=c['sid'],c['jid']
            if sid not in seekers or jid not in jobs or jobs[jid]['until']<=now:
                cancel(eid,now)
                continue
            if stage==0:
                if seekers[sid]['until']<=now:
                    cancel(eid,now)
                    continue
                seekers[sid]['applied']=True
                c['stage']=1
                count('applications',now)
                heapq.heappush(queue,(now+delay(5,4),rng.random(),eid,1))
            elif stage==1:
                if rng.random()>=c['ps']:
                    close(eid,now)
                else:
                    c['stage']=2
                    count('screenings',now)
                    heapq.heappush(queue,(now+delay(30,20),rng.random(),eid,2))
            elif rng.random()>=c['pd']:
                close(eid,now)
            else:
                count('hires',now)
                s=seekers.pop(sid)
                j=jobs.pop(jid)
                if now>=settings.warmup_days:
                    lifetime_s.append(now-s['birth'])
                    lifetime_j.append(now-j['birth'])
                for other in set(s['pending'])|set(j['pending']):
                    cancel(other,now)
    for day in range(settings.warmup_days+settings.measure_days):
        for jid in [j for j,v in jobs.items() if v['until']<=day]:
            j=jobs.pop(jid)
            count('expired_jobs',day)
            if day>=settings.warmup_days:
                lifetime_j.append(day-j['birth'])
            for eid in list(j['pending']):
                cancel(eid,day)
        for sid in list(seekers):
            if sid not in seekers:
                continue
            if seekers[sid]['until']<=day:
                for eid in list(seekers[sid]['pending']):
                    if eid in cases and cases[eid]['stage']==0:
                        cancel(eid,day)
                maybe_exit(sid,day)
        for _ in range(settings.seeker_arrivals):
            seekers[next_s]=dict(birth=day,until=day+settings.application_days,pending=set(),seen=set(),applied=False)
            next_s+=1
        for _ in range(settings.job_arrivals):
            jobs[next_j]=dict(birth=day,until=day+settings.posting_days,pending=set())
            next_j+=1
        process(day+.001)
        pool=list(jobs)
        eligible=[sid for sid,s in seekers.items() if s['until']>day]
        for sid in eligible:
            s=seekers[sid]
            selected=[]
            attempts=0
            while len(selected)<min(settings.referrals_per_day,len(pool)) and attempts<100:
                attempts+=1
                jid=rng.choice(pool)
                if jid in s['seen']:
                    continue
                s['seen'].add(jid)
                selected.append(jid)
                count('referrals',day)
                # Fixed pair propensity, independent of query/output order.
                p=random.Random(f'{settings.seed}:{sid}:{jid}:pair')
                pa,ps,pd=.005+.01*p.random(),.05+.1*p.random(),.035+.07*p.random()
                if rng.random()>=pa:
                    continue
                eid=next_e
                next_e+=1
                cases[eid]=dict(sid=sid,jid=jid,ps=ps,pd=pd,stage=0)
                s['pending'].add(eid)
                jobs[jid]['pending'].add(eid)
                heapq.heappush(queue,(day+.01+delay(3,2),rng.random(),eid,0))
        process(day+1)
        if day>=settings.warmup_days:
            daily.append(dict(day=day,seekers=len(seekers),jobs=len(jobs),eligible=len(eligible),waiting=sum(s['until']<=day+1 for s in seekers.values())))
    def summary(key):
        x=np.array([d[key] for d in daily])
        return dict(mean=float(x.mean()),p05=float(np.quantile(x,.05)),p95=float(np.quantile(x,.95)),min=int(x.min()),max=int(x.max()),
                    first_half=float(x[:len(x)//2].mean()),second_half=float(x[len(x)//2:].mean()))
    return dict(settings=asdict(settings),population={k:summary(k) for k in ('seekers','jobs','eligible','waiting')},
                events=stats,exited_mean_days=dict(seekers=float(np.mean(lifetime_s)) if lifetime_s else None,jobs=float(np.mean(lifetime_j)) if lifetime_j else None),daily=daily)


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    args.output.mkdir(parents=True,exist_ok=False)
    rows=[]
    for w in (15,18,21):
        for l in (39,40,41):
            r=simulate(Settings(application_days=w,posting_days=l))
            r['objective']=abs(r['population']['seekers']['mean']/1000-1)+abs(r['population']['jobs']['mean']/2000-1)
            rows.append(r)
            print(w,l,r['population']['seekers']['mean'],r['population']['jobs']['mean'],flush=True)
    best=min(rows,key=lambda r:r['objective'])
    center=best['settings']['application_days']
    for w in range(max(1,center-2),center+3):
        if w in (15,18,21):
            continue
        r=simulate(Settings(application_days=w,posting_days=best['settings']['posting_days']))
        r['objective']=abs(r['population']['seekers']['mean']/1000-1)+abs(r['population']['jobs']['mean']/2000-1)
        rows.append(r)
        print('refine',w,r['population']['seekers']['mean'],flush=True)
    best=min(rows,key=lambda r:r['objective'])
    validations=[]
    for seed in (111,222,333):
        r=simulate(Settings(**{**best['settings'],'seed':seed}))
        validations.append(r)
        print('validate',seed,r['population']['seekers']['mean'],r['population']['jobs']['mean'],flush=True)
    result=dict(scale=.01,full_scale_arrivals=dict(seekers=5000,jobs=5000),target=dict(seekers=1000,jobs=2000),
                search=rows,best=best,validation=validations,
                limitations=['Sizing pilot only: uniform fixed pair propensities within configured bounds; no trained scores, full profile mechanism or oracle.',
                             'Downscaled competition and repeat-pair effects may differ at full population size. Full-scale confirmation required.'])
    (args.output/'results.json').write_text(json.dumps(result,indent=2),encoding='utf-8')


if __name__=='__main__':
    main()
