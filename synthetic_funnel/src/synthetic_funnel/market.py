"""Continuous market with immutable profiles, rule-filtered introductions and exits."""
import copy
import heapq
import random
from dataclasses import dataclass, replace

import numpy as np

from .generator import Config
from .mechanism import probability_arrays
from .rates import population_rates

LATENT_CANDIDATE_FIELDS=('p_candidate_application','p_candidate_screening','p_candidate_decision')
LATENT_EMPLOYER_FIELDS=('p_employer_application','p_employer_screening','p_employer_decision')
LATENT_DATE_FIELDS=('latent_oubo_dt','latent_shorui_tsuuka_dt','latent_kettei_dt')
# Counterfactual failures only: the market's cancellation reasons never enter these columns.
FAILURE_REASONS={1:'not_applied',2:'candidate_withdrawal',3:'employer_rejection'}
# Stage-gate delays of the ordinary funnel; the counterfactual resamples them
# ignoring exits, posting expiry and cancellations.
LATENT_STAGE_DELAYS=((3.,2.),(5.,4.),(30.,20.))
LATENT_STREAM=0x6C6174656E74  # b'latent'; seeds the counterfactual stream apart from the market stream
DTYPE=np.dtype([(k,'i4') for k in ('seeker','job','day')]+[(k,'f8') for k in ('application','screening','decision','pa','ps','pd')]+[(k,'i2') for k in ('other_applications','competing_candidates')]
               +[(k,'f8') for k in (*LATENT_CANDIDATE_FIELDS,*LATENT_EMPLOYER_FIELDS,*LATENT_DATE_FIELDS)]
               +[('failure_stage','i2'),('failure_reason','i2')])
INTRODUCTION_OFFSET_DAYS = 0.01


@dataclass(frozen=True)
class MarketConfig:
    seed:int=71
    seeker_arrivals:int=50
    job_arrivals:int=50
    days:int=1825
    application_days:int=20
    posting_days:int=40
    referrals:int=5

    def __post_init__(self):
        for key in ('seeker_arrivals','job_arrivals'):
            if type(getattr(self,key)) is not int or getattr(self,key)<0:raise ValueError('Arrivals must be nonnegative integers')
        for key in ('days','application_days','posting_days'):
            if type(getattr(self,key)) is not int or getattr(self,key)<1:raise ValueError('Durations must be positive integers')
        if type(self.referrals) is not int or not 0<=self.referrals<=5:raise ValueError('Daily referrals must be between 0 and 5')


def features(rows,seekers,jobs):
    s=seekers[rows['seeker']]
    j=jobs[rows['job']]
    f={'seeker_skill': s[:,1],'required_skill': j[:,1],'desired_salary': s[:,2],'offered_salary': j[:,2],
           'wants_remote': s[:,5],'allows_remote': j[:,5],'skill_match': 1-abs(s[:,1]-j[:,1]),
           'skill_shortfall': np.maximum(j[:,1]-s[:,1],0),'overqualification': np.maximum(s[:,1]-j[:,1],0),
           'salary_shortfall': np.maximum((s[:,2]-j[:,2])/s[:,2],0),'remote_mismatch': s[:,5]*(1-j[:,5]),
           'other_applications': rows['other_applications'],'competing_candidates': rows['competing_candidates']}
    f['seeker_days_left']=s[:,0]+20-rows['day']
    f['job_days_left']=j[:,0]+40-rows['day']
    return f


def counterfactual(rows,day,rng):
    """Fill counterfactual latent arrivals, failure stage and reason per referral.

    Records where the funnel would reach if the market kept mediating after the
    referral: exits, posting expiry and cancellations never interrupt it. Every
    draw comes only from the dedicated stream, so the market stream, replay and
    observed dates are untouched.
    """
    current=np.full(len(rows),day+INTRODUCTION_OFFSET_DAYS)
    stage=np.zeros(len(rows),dtype=np.int64)
    reason=np.zeros(len(rows),dtype=np.int64)
    for i in range(3):
        pending=np.flatnonzero(stage==0)
        if not len(pending):break
        employer=rng.random(len(pending))<rows[LATENT_EMPLOYER_FIELDS[i]][pending]
        candidate=rng.random(len(pending))<rows[LATENT_CANDIDATE_FIELDS[i]][pending]
        stop=~(employer&candidate)
        stopped=pending[stop]
        stage[stopped]=i+1
        reason[stopped]=np.where(employer[stop],1 if i==0 else 2,3)
        moved=pending[~stop]
        if len(moved):
            mean,std=LATENT_STAGE_DELAYS[i]
            days=rng.normal(mean,std,len(moved))
            retry=days<0
            while retry.any():
                days[retry]=rng.normal(mean,std,int(retry.sum()))
                retry=days<0
            current[moved]+=days
            rows[LATENT_DATE_FIELDS[i]][moved]=current[moved]
    rows['failure_stage']=stage
    rows['failure_reason']=reason


def simulate(config, *, progress=None, snapshot_days=None, resume=None, focal_index=None,
             branch_seed=None, retain='all', daily_stats=None):
    """Simulate the same market; optionally retain only observed applications.

    Retention changes storage, never introductions, random draws or outcomes.
    Fully expired daily chunks are compacted only after all their cases close.

    If provided, daily_stats receives (day, live users, live items, introductions, new users,
    new items, applications, screenings, decisions) for each simulated day.
    """
    if retain not in ('all','applications'):
        raise ValueError('retain must be all or applications')
    if resume is not None and retain!='all':
        raise ValueError('Future branches require retain=all')
    rng=random.Random(config.seed if branch_seed is None else branch_seed)
    # Counterfactual draws come only from a dedicated stream, never from the market stream.
    latent_rng=np.random.default_rng(np.random.SeedSequence(
        [config.seed if branch_seed is None else branch_seed,LATENT_STREAM]))
    # Profiles are mutable 8-column rows: arrival, skill, salary, occupation, region, remote,
    # hidden affinity and departure (NaN while the actor is still live).
    seekers=[] if resume is None else [list(profile) for profile in resume['seekers']]
    jobs=[] if resume is None else [list(profile) for profile in resume['jobs']]
    # Profile tables avoid converting the entire growing history daily.
    seeker_table=np.empty((config.days*config.seeker_arrivals,8)) if resume is None else None
    job_table=np.empty((config.days*config.job_arrivals,8)) if resume is None else None
    live_s={} if resume is None else copy.deepcopy(resume['live_s'])
    live_j={} if resume is None else copy.deepcopy(resume['live_j'])
    chunks=[] if resume is None else [resume['rows'].copy()]
    queue=[]
    cases={}
    next_e=0
    daily_counts={}
    def count_transition(stage,t):
        daily_counts.setdefault(int(t),[0,0,0])[stage]+=1
    base=Config(seed=config.seed, legacy_scores=replace(Config().legacy_scores, enabled=False))
    anchors=population_rates(base).intercepts if resume is None else resume['anchors']
    def delay(mu,sd):
        while True:
            v=rng.gauss(mu,sd)
            if v>=0:
                return v
    def natural(sid,t):
        if sid in live_s and live_s[sid]['until']<=t and not live_s[sid]['pending']:
            seekers[sid][7]=t
            del live_s[sid]
    def finish(eid,t):
        c=cases.pop(eid,None)
        if c is None:return
        sid,jid,_,_,_=c
        if sid in live_s:live_s[sid]['pending'].discard(eid)
        if jid in live_j:live_j[jid]['pending'].discard(eid)
        natural(sid,t)
    def process(until):
        while queue and queue[0][0]<until:
            t,_,eid,stage=heapq.heappop(queue)
            if eid not in cases:continue
            sid,jid,chunk,offset,_=cases[eid]
            if sid not in live_s or jid not in live_j:
                finish(eid,t);continue
            row=chunks[chunk][offset]
            if stage==0:
                if live_s[sid]['until']<=t:finish(eid,t);continue
                row['application']=t
                count_transition(0,t)
                cases[eid]=(sid,jid,chunk,offset,1)
                heapq.heappush(queue,(t+delay(5,4),rng.random(),eid,1))
            elif stage==1:
                if eid!=focal_eid and rng.random()>=row['ps']:finish(eid,t);continue
                row['screening']=t
                count_transition(1,t)
                cases[eid]=(sid,jid,chunk,offset,2)
                heapq.heappush(queue,(t+delay(30,20),rng.random(),eid,2))
            else:
                if eid!=focal_eid and rng.random()>=row['pd']:finish(eid,t);continue
                row['decision']=t
                count_transition(2,t)
                seekers[sid][7]=t
                jobs[jid][7]=t
                s=live_s.pop(sid)
                j=live_j.pop(jid)
                for other in s['pending']|j['pending']:finish(other,t)
    focal_eid=-1
    if resume is not None:
        from scipy.special import ndtr, ndtri
        now=resume['day']+INTRODUCTION_OFFSET_DAYS
        rr=chunks[0]
        stages=np.where(np.isfinite(rr['screening']),2,np.where(np.isfinite(rr['application']),1,0))
        origins=np.where(stages==2,rr['screening'],np.where(stages==1,rr['application'],rr['day']+INTRODUCTION_OFFSET_DAYS))
        mus=np.array([3.,5.,30.])[stages];sigmas=np.array([2.,4.,20.])[stages]
        elapsed_all=np.maximum(0.,now-origins)
        tail=ndtr((mus-elapsed_all)/sigmas)
        survival_all=tail/ndtr(mus/sigmas)
        posterior_all=rr['pa']*survival_all/(1-rr['pa']+rr['pa']*survival_all)
        numpy_rng=np.random.default_rng(rng.getrandbits(128))
        keep=(stages>0)|(numpy_rng.random(len(rr))<posterior_all)
        keep[focal_index]=True
        offsets=np.flatnonzero(keep)
        delays_all=mus[offsets]-sigmas[offsets]*ndtri(np.maximum(np.finfo(float).tiny,numpy_rng.random(len(offsets))*tail[offsets]))
        for offset,value in zip(offsets,delays_all):
            row=rr[offset]
            sid,jid=int(row['seeker']),int(row['job'])
            stage=int(stages[offset]);origin=float(origins[offset])
            eid=next_e;next_e+=1
            if offset==focal_index:focal_eid=eid
            cases[eid]=(sid,jid,0,offset,stage)
            live_s[sid]['pending'].add(eid);live_j[jid]['pending'].add(eid)
            heapq.heappush(queue,(origin+value,rng.random(),eid,stage))
        if focal_eid<0:raise ValueError('Focal introduction not found')
        process(resume['day']+1)
        if focal_eid not in cases:return chunks[0][focal_index].copy()
    first_day=0 if resume is None else resume['day']+1
    for day in range(first_day,config.days):
        for jid in [i for i,v in live_j.items() if v['until']<=day]:
            j=live_j.pop(jid)
            jobs[jid][7]=day
            for eid in list(j['pending']):finish(eid,day)
        for sid in list(live_s):
            if sid not in live_s:continue
            if live_s[sid]['until']<=day:
                for eid in list(live_s[sid]['pending']):
                    if eid in cases and cases[eid][-1]==0:finish(eid,day)
                natural(sid,day)
        for _ in range(config.seeker_arrivals):
            sid=len(seekers);skill=rng.random()
            # arrival, skill, desired salary, occupation, region, remote preference, hidden
            # affinity, departure (NaN while live)
            seekers.append([day,skill,max(200,300+800*skill+rng.gauss(0,100)),rng.randrange(3),rng.randrange(4),int(rng.random()<.5),rng.gauss(0,.5),np.nan])
            if seeker_table is not None:seeker_table[sid]=seekers[-1]
            live_s[sid]={'until': day+config.application_days,'pending': set(),'seen': set()}
        for _ in range(config.job_arrivals):
            jid=len(jobs);skill=rng.random();salary=max(200,300+800*skill+rng.gauss(0,150))
            # salary minimum is offered minus 100, clipped to 100.
            jobs.append([day,skill,salary,rng.randrange(3),rng.randrange(4),int(rng.random()<.5),rng.gauss(0,.5),np.nan])
            if job_table is not None:job_table[jid]=jobs[-1]
            live_j[jid]={'until': day+config.posting_days,'pending': set()}
        process(day+INTRODUCTION_OFFSET_DAYS)
        pools={}
        for role in range(3):
            for region in range(4):
                ids=[jid for jid in live_j if jobs[jid][3]==role and (jobs[jid][4]==region or jobs[jid][5])]
                ids.sort(key=lambda jid:max(100,jobs[jid][2]-100))
                pools[role,region]=(ids,np.array([max(100,jobs[jid][2]-100) for jid in ids]))
        selected=[]
        for sid,s in live_s.items():
            if s['until']<=day:continue
            profile=seekers[sid]
            pool,lower=pools[profile[3],profile[4]]
            n=int(np.searchsorted(lower,profile[2],side='left'))
            if not n:continue
            chosen=[]
            for _ in range(200):
                if len(chosen)>=min(config.referrals,n):break
                jid=pool[rng.randrange(n)]
                if jid in s['seen']:continue
                s['seen'].add(jid);chosen.append(jid)
            for jid in chosen:
                other=sum(cases[e][-1]>=1 for e in s['pending'] if e in cases)
                rivals=sum(cases[e][-1]>=1 for e in live_j[jid]['pending'] if e in cases)
                selected.append((sid,jid,day,np.nan,np.nan,np.nan,0.,0.,0.,other,rivals,
                                 0.,0.,0.,0.,0.,0.,np.nan,np.nan,np.nan,0,0))
        rows=np.array(selected,dtype=DTYPE)
        chunks.append(rows)
        if len(rows):
            sa=seeker_table[:len(seekers)] if seeker_table is not None else np.asarray(seekers)
            ja=job_table[:len(jobs)] if job_table is not None else np.asarray(jobs)
            f=features(rows,sa,ja)
            gates=probability_arrays(base,f,sa[rows['seeker'],6],ja[rows['job'],6],anchors)
            rows['pa'],rows['ps'],rows['pd']=gates.prod(axis=-1).T
            rows['p_candidate_application'],rows['p_candidate_screening'],rows['p_candidate_decision']=gates[:,:,0].T
            rows['p_employer_application'],rows['p_employer_screening'],rows['p_employer_decision']=gates[:,:,1].T
            counterfactual(rows,day,latent_rng)
            for offset,row in enumerate(rows):
                if rng.random()>=row['pa']:continue
                sid,jid=int(row['seeker']),int(row['job']);eid=next_e;next_e+=1
                cases[eid]=(sid,jid,len(chunks)-1,offset,0)
                live_s[sid]['pending'].add(eid);live_j[jid]['pending'].add(eid)
                heapq.heappush(queue,(day+INTRODUCTION_OFFSET_DAYS+delay(3,2),rng.random(),eid,0))
        if snapshot_days is not None and day in snapshot_days:
            snapshot_days[day]=make_snapshot(day,seekers,jobs,live_s,live_j,chunks,cases,anchors)
        process(day+1)
        if resume is not None and focal_eid not in cases:return chunks[0][focal_index].copy()
        if retain=='applications' and day>=config.posting_days+1:
            old=day-config.posting_days-1
            # Every job introduced in this chunk has expired; offsets are unused.
            if any(c[2]==old for c in cases.values()):
                raise RuntimeError('Cannot compact an active case')
            chunks[old]=chunks[old][np.isfinite(chunks[old]['application'])].copy()
        if daily_stats is not None:
            daily_stats.append((day, len(live_s), len(live_j), len(rows), config.seeker_arrivals,
                                config.job_arrivals, *daily_counts.pop(day, (0, 0, 0))))
        if progress and day%100==0:progress(day,len(rows))
    if resume is not None:return chunks[0][focal_index].copy()
    rows=np.concatenate(chunks)
    if retain=='applications':rows=rows[np.isfinite(rows['application'])]
    return rows, np.asarray(seekers).reshape(-1, 8), np.asarray(jobs).reshape(-1, 8)


def make_snapshot(day,seekers,jobs,live_s,live_j,chunks,cases,anchors):
    smap={sid:i for i,sid in enumerate(live_s)};jmap={jid:i for i,jid in enumerate(live_j)}
    rows=[];keys=[]
    # Unapplied attempts have no observed outcome until application/expiry.
    # Do not retain internally known future application flags.
    for ci in range(max(0,len(chunks)-21),len(chunks)):
        for ri,row in enumerate(chunks[ci]):
            if not np.isfinite(row['application']) and int(row['seeker']) in smap and int(row['job']) in jmap and live_s[int(row['seeker'])]['until']>day+INTRODUCTION_OFFSET_DAYS:
                rows.append(row.copy());keys.append((ci,ri))
    for sid,jid,ci,ri,stage in cases.values():
        if stage>=1 and sid in smap and jid in jmap:
            rows.append(chunks[ci][ri].copy());keys.append((ci,ri))
    array=np.array(rows,dtype=DTYPE)
    array['seeker']=[smap[int(v)] for v in array['seeker']]
    array['job']=[jmap[int(v)] for v in array['job']]
    ss={smap[sid]:{'until': s['until'],'pending': set(),'seen': {jmap[j] for j in s['seen'] if j in jmap}} for sid,s in live_s.items()}
    jj={jmap[jid]:{'until': j['until'],'pending': set()} for jid,j in live_j.items()}
    return {'day': day,'seekers': [list(seekers[i]) for i in smap],'jobs': [list(jobs[i]) for i in jmap],'live_s': ss,'live_j': jj,
                'rows': array,'keys': keys,'anchors': anchors}


def labels(rows,horizon):
    return np.column_stack([np.isfinite(rows[k])&(rows[k]<=horizon) for k in ('application','screening','decision')])


def matched_candidates(rows,y,stage,positives):
    from collections import defaultdict
    if stage>0:
        negatives=np.flatnonzero(y[:,stage-1]&~y[:,stage]).tolist()
        return {int(i):negatives for i in positives}
    by_s=defaultdict(list);by_j=defaultdict(list)
    risk=np.ones(len(rows),bool) if stage==0 else y[:,stage-1]
    for i in np.flatnonzero(risk & ~y[:,stage]):
        key=(int(rows['seeker'][i]),int(rows['day'][i])) if stage==0 else int(rows['seeker'][i])
        by_s[key].append(int(i));by_j[int(rows['job'][i])].append(int(i))
    result={}
    for i in positives:
        sid,jid=int(rows['seeker'][i]),int(rows['job'][i])
        if stage==0:values=by_s[sid,int(rows['day'][i])]
        elif stage==1:values=by_j[jid]
        else:values=set(by_s[sid])|set(by_j[jid])
        result[int(i)]=list(values)
    return result


class SamplingError(ValueError):
    pass


def extract(rows,horizon,stage,period,as_of,mode,seed=42,sample_size=None,positive_count=None,negative_ratio=None,positive_eligibility='all'):
    """Extract mature stage risk sets; indices refer to the immutable market."""
    if type(stage) is not int or stage not in (0,1,2) or period[0]>=period[1]:
        raise SamplingError('Invalid phase or period')
    y=labels(rows,horizon)
    origin=rows['day'] if stage==0 else rows[('application','screening')[stage-1]]
    risk=np.ones(len(rows),bool) if stage==0 else y[:,stage-1]
    eligible=risk&(origin>=period[0])&(origin<period[1])&(horizon<=as_of)
    indices=np.flatnonzero(eligible)
    rng=np.random.default_rng(seed)
    if mode=='uniform':
        if positive_count is not None or negative_ratio is not None or type(sample_size) is not int or sample_size<1 or positive_eligibility!='all':
            raise SamplingError('uniform accepts only sample_size')
        if sample_size>len(indices):raise SamplingError('INSUFFICIENT_POPULATION')
        return {'indices': np.sort(rng.choice(indices,sample_size,replace=False)),'population': len(indices)}
    if mode!='matched' or negative_ratio is None or positive_count is None or sample_size is not None:
        raise SamplingError('Invalid matched request')
    if type(negative_ratio) is not int or negative_ratio<1 or (positive_count!='all' and (type(positive_count) is not int or positive_count<1)):
        raise SamplingError('Positive count and negative ratio must be positive integers')
    positive=indices[y[indices,stage]]
    if positive_eligibility not in ('all','has_matched_negatives'):raise SamplingError('Invalid positive eligibility')
    excluded=0
    if positive_eligibility=='has_matched_negatives':
        c=matched_candidates(rows[indices],y[indices],stage,np.searchsorted(indices,positive))
        accepted=np.array([i for i in positive if len(c[int(np.searchsorted(indices,i))])>=negative_ratio],dtype=np.int64)
        excluded=len(positive)-len(accepted);positive=accepted
    if positive_count=='all':selected=positive
    else:
        if positive_count>len(positive):raise SamplingError('INSUFFICIENT_POSITIVES')
        selected=np.sort(rng.choice(positive,positive_count,replace=False))
    local=rows[indices];target=y[indices]
    local_positive=np.searchsorted(indices,selected)
    candidates=matched_candidates(local,target,stage,local_positive)
    failures=[int(indices[i]) for i,values in candidates.items() if len(values)<negative_ratio]
    if failures:
        raise SamplingError(f'INSUFFICIENT_MATCHED_NEGATIVES: {len(failures)}/{len(selected)} positives lack {negative_ratio} distinct candidates')
    matches=[]
    for i in local_positive:
        generator=np.random.default_rng(np.random.SeedSequence([seed,int(indices[i])]))
        negatives=generator.choice(candidates[int(i)],negative_ratio,replace=False)
        matches.extend((int(indices[i]),int(indices[n])) for n in negatives)
    mapping=np.array(matches,dtype=np.int64).reshape(-1,2)
    negative,counts=np.unique(mapping[:,1],return_counts=True)
    return {'positive_indices': selected,'matches': mapping,'negative_indices': negative,'negative_multiplicity': counts,'population': len(indices),'excluded_positives': excluded}
