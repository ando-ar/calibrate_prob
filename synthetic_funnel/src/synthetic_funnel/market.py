"""Continuous market with immutable profiles, rule-filtered introductions and exits."""
import copy
import heapq
import random
from bisect import bisect_left, insort
from dataclasses import dataclass, field

import numpy as np

from .mechanism import probability_arrays
from .rates import population_rates
from .shared import ProbabilityConfig

LATENT_CANDIDATE_FIELDS=('p_candidate_application','p_candidate_screening','p_candidate_decision')
LATENT_EMPLOYER_FIELDS=('p_employer_application','p_employer_screening','p_employer_decision')
LATENT_DATE_FIELDS=('latent_oubo_dt','latent_shorui_tsuuka_dt','latent_kettei_dt')
# Counterfactual failures only: the market's cancellation reasons never enter these columns.
FAILURE_REASONS={1:'not_applied',2:'candidate_withdrawal',3:'employer_rejection'}
# Stage-gate delays of the ordinary funnel; the counterfactual resamples them
# ignoring exits, posting expiry and cancellations.
LATENT_STAGE_DELAYS=((3.,2.),(5.,4.),(30.,20.))
LATENT_STREAM=0x6C6174656E74  # b'latent'; seeds the counterfactual stream apart from the market stream
# Observed terminal outcomes stored in events (issue #32). Code 0 means NULL:
# no terminal result was observed (hired, still in flight, or observation-end censored).
OUTCOME_STATUSES=('not_applied','ng','withdrawn','cancelled_posting_expiry',
                  'cancelled_seeker_exit','cancelled_market_end')
OUTCOME_STATUS_CODES={name:index+1 for index,name in enumerate(OUTCOME_STATUSES)}
# Item-side placement fee rate. Uniform today, but stored as a profile column so a
# future per-item/per-market rate needs no schema change (issue #34). No RNG draw:
# the fee is deterministic from already generated attributes.
FEE_RATE_DEFAULT=0.33
DTYPE=np.dtype([(k,'i4') for k in ('seeker','job','day')]+[(k,'f8') for k in ('application','screening','decision','pa','ps','pd')]+[(k,'i2') for k in ('other_applications','competing_candidates')]
               +[(k,'f8') for k in (*LATENT_CANDIDATE_FIELDS,*LATENT_EMPLOYER_FIELDS,*LATENT_DATE_FIELDS)]
               +[('failure_stage','i2'),('failure_reason','i2')]
               +[('status','i2'),('outcome_dt','f8'),('auto','i2')]
               +[('fee_yen','f8')])
INTRODUCTION_OFFSET_DAYS = 0.01


def observed_failure_status(u,p,p_employer):
    """Split a failed two-sided gate using the draw already consumed by the gate.

    Failure is u >= p with p = p_candidate * p_employer.  Conditional on failure,
    the employer side is the culprit for u < (1 - p_employer + p), which has the
    correct probability (1 - p_employer) / (1 - p).  This is the same priority as
    counterfactual() and, critically, consumes no extra random draw.
    """
    return 'ng' if u < 1.0-p_employer+p else 'withdrawn'

# Categories are drawn once per arriving participant and shared by the filtering rules.
OCCUPATIONS = ("エンジニア", "営業", "事務", "デザイン", "マーケティング", "介護", "販売", "製造")
REGIONS = ("東京", "大阪", "名古屋", "福岡", "札幌", "仙台", "広島", "沖縄")


# Training draws use their own stream so that neither the market stream
# (random.Random(seed)) nor the counterfactual stream (SeedSequence([seed, b'latent']))
# advances because of model fitting.
TRAINING_STREAM = 0x747261696E  # b'train'


@dataclass(frozen=True)
class MarketConfig:
    seed:int=71
    seeker_arrivals:int=50
    job_arrivals:int=50
    days:int=1825
    warmup:int=365
    application_days:int=20
    posting_days:int=40
    referrals:int=5
    training_window_days:tuple=(1,30,365)
    training_referrals_per_day:int=10000
    negative_reservoir_size:int=100000

    def __post_init__(self):
        for key in ('seeker_arrivals','job_arrivals'):
            if type(getattr(self,key)) is not int or getattr(self,key)<0:raise ValueError('Arrivals must be nonnegative integers')
        for key in ('days','application_days','posting_days'):
            if type(getattr(self,key)) is not int or getattr(self,key)<1:raise ValueError('Durations must be positive integers')
        if type(self.warmup) is not int or self.warmup<0:raise ValueError('warmup must be a nonnegative integer')
        if type(self.referrals) is not int or not 0<=self.referrals<=5:raise ValueError('Daily referrals must be between 0 and 5')
        if (not isinstance(self.training_window_days,(tuple,list)) or len(self.training_window_days)!=3
                or any(type(v) is not int or v<1 for v in self.training_window_days)):
            raise ValueError('training_window_days requires three positive integers')
        object.__setattr__(self,'training_window_days',tuple(self.training_window_days))
        if type(self.training_referrals_per_day) is not int or self.training_referrals_per_day<1 or type(self.negative_reservoir_size) is not int or self.negative_reservoir_size<1:
            raise ValueError('Training density and reservoir size must be positive integers')


@dataclass
class ReplayIntroductions:
    """External introduction policy for :func:`simulate` (competition replay, #35).

    The market is *not* deserialised from a huge exported snapshot.  Instead the
    whole run is re-simulated from day 0 with the same ``MarketConfig`` and seed;
    because :func:`simulate` is deterministic, the state at ``eval_start`` is bit
    identical to the frozen ``D_train`` state of the base market.  This is the
    "reproduce by re-simulation" option of COMPETITION.md §5.2/§3.1: it keeps
    memory bounded (only the live actor dicts and recent chunks, exactly as in a
    normal run) and needs no state file.  ``make_snapshot``/``resume`` remain
    available for callers that prefer an explicit snapshot export.

    Introduction policy
        ``policy`` maps a day offset to an iterable of ``(seeker_index, job_index)``
        pairs that the evaluator already selected with the §2.1 budget rules.
        Days in ``[eval_start, eval_end)`` use ``policy`` exclusively (an absent
        or empty entry means *no introductions* that day: unranked candidates are
        never introduced).  Days before ``eval_start`` use the built-in random
        pool draw, and days at/after ``eval_end`` introduce nothing at all -- the
        run simply continues until ``config.days`` so every selected row can
        mature through ``D_eval_end + H``.

    Recipe contract (issue #37)
        ``admissible`` is the exact set of ``(day, seeker, job)`` triples admitted
        by the DAILY_CANDIDATES recipe on each evaluation day.  A policy pair
        outside that set is skipped with ``not_in_recipe``; every pair inside it
        is introduced.  ``departure`` maps actor indices to their dataset
        ``departure_dt`` day (``None`` when the dataset leaves it NULL).  Because
        the recipe is a *batch* view over the frozen base dataset, an actor that
        the policy hired on an earlier evaluation day is restored from that
        schedule when a later day still admits it -- replay dynamics must not
        silently shrink the recipe's introduction universe.  Policy introductions
        are deliberately **not** added to ``seen``: the recipe's ``NOT EXISTS``
        test only consults base-run events, so re-ranking the same pair on a
        later day is legitimate and must not be treated as a duplicate.

    RNG discipline
        The policy *replaces* the built-in ``rng.randrange`` introduction draw and
        consumes no value from the market ``random.Random`` stream.  Consequently
        funnel transitions, exits, probabilities, delays and the counterfactual
        stream keep their existing per-event semantics, and identical replay
        inputs always produce identical outputs.  Relative to the base market the
        funnel stream is simply not advanced by the skipped introduction draws on
        replay days; this is the intended branch behaviour (cf. ``branch_seed``).

    ``introduced``/``skipped`` are observability fields filled by :func:`simulate`:
    ``introduced[day]`` lists pairs actually introduced, and ``skipped`` lists
    ``(day, seeker, job, reason)`` entries rejected by the candidate recipe.
    """

    eval_start: int
    eval_end: int
    policy: dict[int, list[tuple[int, int]]] = field(default_factory=dict)
    validate: bool = True
    introduced: dict[int, list[tuple[int, int]]] = field(default_factory=dict)
    skipped: list[tuple[int, int, int, str]] = field(default_factory=list)
    admissible: object = None       # optional set of (day, sid, jid) triples from the recipe
    departure: object = None        # optional {'seekers': {sid: day|None}, 'jobs': {jid: day|None}}

    def __post_init__(self):
        if type(self.eval_start) is not int or type(self.eval_end) is not int or self.eval_start >= self.eval_end:
            raise ValueError('Replay requires integer eval_start < eval_end')
        self.policy = {int(day): [(int(s), int(j)) for s, j in pairs]
                       for day, pairs in self.policy.items()}
        if self.admissible is not None:
            self.admissible = {(int(day), int(s), int(j)) for day, s, j in self.admissible}

    def mode(self, day: int) -> str:
        if day < self.eval_start:
            return 'builtin'
        if day < self.eval_end:
            return 'policy'
        return 'none'

    def pairs_for_day(self, day: int):
        return self.policy.get(day, ())

    def is_admissible(self, day: int, sid: int, jid: int) -> bool:
        """Whether the DAILY_CANDIDATES recipe admits this pair on this day."""
        return self.admissible is None or (day, sid, jid) in self.admissible

    def frozen_departure(self, kind: str, index: int):
        """Dataset departure day for an actor, or ``None`` when still live late."""
        if self.departure is None:
            return None
        return self.departure.get(kind, {}).get(index)


def replay_market(config, *, eval_start, eval_end, policy, validate=True, **simulate_kwargs):
    """Re-run a market from scratch under an external introduction policy.

    Convenience wrapper around :func:`simulate`; returns the same
    ``(rows, seekers, jobs)`` tuple so the replay output is a normal market export.
    """
    return simulate(config, replay=ReplayIntroductions(
        eval_start=eval_start, eval_end=eval_end, policy=policy, validate=validate,
    ), **simulate_kwargs)


NUMERIC_COLUMNS=(0,1,2,5,6)  # arrival, skill, salary, remote flag, hidden motivation (categories are columns 3 and 4)
# Item profiles additionally carry fee_rate in column 8. It is deliberately not a
# model feature: the fee is a deterministic attribute, never a latent value.


def numeric_profiles(profiles,indices):
    """Numeric part of the profile table as floats; text categories are not model features."""
    return np.asarray(profiles[indices][:,NUMERIC_COLUMNS],dtype=float)


def features(rows,seekers,jobs):
    s=numeric_profiles(seekers,rows['seeker'])
    j=numeric_profiles(jobs,rows['job'])
    f={'seeker_skill': s[:,1],'required_skill': j[:,1],'desired_salary': s[:,2],'offered_salary': j[:,2],
           'wants_remote': s[:,3],'allows_remote': j[:,3],'skill_match': 1-abs(s[:,1]-j[:,1]),
           'skill_shortfall': np.maximum(j[:,1]-s[:,1],0),'overqualification': np.maximum(s[:,1]-j[:,1],0),
           'salary_shortfall': np.maximum((s[:,2]-j[:,2])/s[:,2],0),'remote_mismatch': s[:,3]*(1-j[:,3]),
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
             branch_seed=None, retain='all', daily_stats=None, replay=None, population=None,
             warm_state=None, state_days=None, stop_day=None):
    """Simulate the same market; optionally retain only observed applications.

    ``warm_state`` is a full state dict captured by ``state_days`` at an earlier
    day.  It resumes the market at ``warm_state['day'] + 1`` with the exact RNG
    and case state of the original run, so a replay from a loaded snapshot is
    byte-identical to a replay that re-simulates from day 0.  ``stop_day`` ends
    the run early (used by the organiser's freeze step).  Neither option
    changes determinism for callers that do not pass them.

    Retention changes storage, never introductions, random draws or outcomes.
    Fully expired daily chunks are compacted only after all their cases close.

    If provided, daily_stats receives (day, live users, live items, introductions, new users,
    new items, applications, screenings, decisions) for each simulated day.

    ``replay`` is an optional :class:`ReplayIntroductions`.  It replaces the
    built-in random introduction draw on evaluation days with an external
    ranking policy (competition #35).  Without it the function is byte-for-byte
    the base market generator.  Replay mode is mutually exclusive with
    ``resume``/``retain='applications'`` (both are single-focal/compaction paths).

    ``population`` is an optional ``{'seekers': [...], 'jobs': [...]}`` schedule
    of pre-generated arrival profiles indexed by actor id.  It is normally
    reconstructed from the published ``user``/``item`` (and latent) tables by
    the evaluator.  Every arrival still consumes the same RNG draws as the base
    market (the values are discarded and the scheduled profile is used), so the
    market stream stays aligned up to the first replay day and submitted
    ``user_id``/``item_id`` keys keep referring to the same actors on later
    evaluation days even though the policy changes introduction draw counts.
    """
    if replay is not None and (resume is not None or retain != 'all'):
        raise ValueError('Replay requires retain=all and no resume')
    if population is not None and resume is not None:
        raise ValueError('population and resume cannot be combined')
    if warm_state is not None and (resume is not None or replay is None):
        raise ValueError('warm_state requires replay and no resume')
    if stop_day is not None and (type(stop_day) is not int or stop_day<0):
        raise ValueError('stop_day must be a nonnegative integer or None')
    population_seekers = None if population is None else population['seekers']
    population_jobs = None if population is None else population['jobs']
    if retain not in ('all','applications'):
        raise ValueError('retain must be all or applications')
    if resume is not None and retain!='all':
        raise ValueError('Future branches require retain=all')
    stream_seed=config.seed if branch_seed is None else branch_seed
    rng=random.Random(stream_seed)
    # Counterfactual draws come only from a dedicated stream, never from the market stream.
    latent_rng=np.random.default_rng(np.random.SeedSequence([stream_seed,LATENT_STREAM]))
    base=ProbabilityConfig(seed=config.seed)
    daily_counts={}
    def count_transition(stage,t):
        daily_counts.setdefault(int(t),[0,0,0])[stage]+=1
    if warm_state is not None:
        # Restore the complete mutable market state, including both RNG streams,
        # so downstream draws and case scheduling are exactly those of a run
        # that had simulated every preceding day (issue #40 item 4).
        if warm_state.get('seed')!=stream_seed:
            raise ValueError('warm_state seed does not match the market stream')
        freeze_day=int(warm_state['day'])
        if not 0<=freeze_day<config.days:
            raise ValueError('warm_state day is outside the configured horizon')
        seekers=[list(profile) for profile in warm_state['seekers']]
        jobs=[list(profile) for profile in warm_state['jobs']]
        live_s=copy.deepcopy(warm_state['live_s'])
        live_j=copy.deepcopy(warm_state['live_j'])
        chunks=[chunk.copy() for chunk in warm_state['chunks']]
        cases=dict(warm_state['cases'])
        queue=list(warm_state['queue'])
        heapq.heapify(queue)
        next_e=int(warm_state['next_e'])
        anchors=np.asarray(warm_state['anchors'])
        daily_counts={int(k):list(v) for k,v in warm_state.get('daily_counts',{}).items()}
        rng.setstate(warm_state['rng_state'])
        latent_rng.bit_generator.state=warm_state['latent_state']
        first_day=freeze_day+1
    else:
        first_day=0
    # Profiles are mutable rows: arrival, skill, salary, occupation, region, remote,
    # hidden motivation (column 6) and departure (column 7, NaN while the actor is
    # still live). Item profiles carry one more column, fee_rate (column 8).
    if warm_state is None:
        seekers=[] if resume is None else [list(profile) for profile in resume['seekers']]
        jobs=[] if resume is None else [list(profile) for profile in resume['jobs']]
    # Profile tables avoid converting the entire growing history daily.
    seeker_table=np.empty((config.days*config.seeker_arrivals,8),dtype=object) if resume is None else None
    job_table=np.empty((config.days*config.job_arrivals,9),dtype=object) if resume is None else None
    if warm_state is not None:
        for i,profile in enumerate(seekers):seeker_table[i]=profile
        for i,profile in enumerate(jobs):job_table[i]=profile
    if warm_state is None:
        live_s={} if resume is None else copy.deepcopy(resume['live_s'])
        live_j={} if resume is None else copy.deepcopy(resume['live_j'])
        chunks=[] if resume is None else [resume['rows'].copy()]
        queue=[]
        cases={}
        next_e=0
    anchors=(anchors if warm_state is not None else
             (population_rates(base).intercepts if resume is None else resume['anchors']))
    # Incremental live-job pools keyed by (occupation, region).  A remote job
    # belongs to every region of its occupation.  Rebuilding these by scanning
    # all live jobs every day was one of the top profile costs at 100/100
    # arrivals; insertions and removals now maintain sorted (min_salary, jid)
    # lists in place with bisect.
    pool_groups={}
    def make_live_s(until):
        return {'until': until, 'pending': set(), 'seen': set(), 'active': 0}
    def make_live_j(until):
        return {'until': until, 'pending': set(), 'active': 0}
    # `active` counts pending cases already past the application gate, i.e. the
    # per-row other_applications / competing_candidates features.  Keeping it
    # on each actor avoids re-summing the pending sets for every introduction.
    for actor in live_s.values():actor.setdefault('active',0)
    for actor in live_j.values():actor.setdefault('active',0)
    def job_regions(job):
        return REGIONS if job[5] else (job[4],)
    def pool_add(jid):
        job=jobs[jid]
        entry=(max(100,job[2]-100),jid)
        for region in job_regions(job):
            insort(pool_groups.setdefault((job[3],region),[]),entry)
    def pool_discard(jid):
        job=jobs[jid]
        entry=(max(100,job[2]-100),jid)
        for region in job_regions(job):
            group=pool_groups.get((job[3],region))
            if not group:continue
            pos=bisect_left(group,entry)
            if pos<len(group) and group[pos]==entry:del group[pos]
    if warm_state is not None and 'pool_groups' in warm_state:
        pool_groups={key:list(entries) for key,entries in warm_state['pool_groups'].items()}
    else:
        for jid in live_j:pool_add(jid)
    def delay(mu,sd):
        while True:
            v=rng.gauss(mu,sd)
            if v>=0:
                return v
    def natural(sid,t):
        if sid in live_s and live_s[sid]['until']<=t and not live_s[sid]['pending']:
            seekers[sid][7]=t
            del live_s[sid]
    def finish(eid,t,status=None,auto=0):
        c=cases.pop(eid,None)
        if c is None:return
        sid,jid,chunk,offset,_=c
        row=chunks[chunk][offset]
        # A hired case (finite decision time) never receives a terminal outcome,
        # even when it is a member of the cascade's pending sets.
        if status is not None and not np.isfinite(row['decision']):
            row['status']=OUTCOME_STATUS_CODES[status]
            row['outcome_dt']=t
            row['auto']=auto
        if sid in live_s and eid in live_s[sid]['pending']:
            live_s[sid]['pending'].discard(eid)
            if c[-1]>=1:live_s[sid]['active']-=1
        if jid in live_j and eid in live_j[jid]['pending']:
            live_j[jid]['pending'].discard(eid)
            if c[-1]>=1:live_j[jid]['active']-=1
        natural(sid,t)
    def process(until):
        while queue and queue[0][0]<until:
            t,_,eid,stage=heapq.heappop(queue)
            if eid not in cases:continue
            sid,jid,chunk,offset,_=cases[eid]
            if sid not in live_s or jid not in live_j:
                finish(eid,t,'cancelled_market_end');continue
            row=chunks[chunk][offset]
            if stage==0:
                if live_s[sid]['until']<=t:finish(eid,t,'cancelled_seeker_exit');continue
                row['application']=t
                count_transition(0,t)
                cases[eid]=(sid,jid,chunk,offset,1)
                if eid in live_s[sid]['pending']:live_s[sid]['active']+=1
                if eid in live_j[jid]['pending']:live_j[jid]['active']+=1
                heapq.heappush(queue,(t+delay(5,4),rng.random(),eid,1))
            elif stage==1:
                if eid!=focal_eid and (u:=rng.random())>=row['ps']:
                    finish(eid,t,observed_failure_status(u,row['ps'],row['p_employer_screening']))
                    continue
                row['screening']=t
                count_transition(1,t)
                cases[eid]=(sid,jid,chunk,offset,2)
                heapq.heappush(queue,(t+delay(30,20),rng.random(),eid,2))
            else:
                if eid!=focal_eid and (u:=rng.random())>=row['pd']:
                    finish(eid,t,observed_failure_status(u,row['pd'],row['p_employer_decision']))
                    continue
                row['decision']=t
                # Fee is fixed at the decision from the item's rate and offered salary.
                # Deterministic: no draw, so the market stream stays bit-identical.
                row['fee_yen']=float(jobs[jid][2])*float(jobs[jid][8])
                count_transition(2,t)
                seekers[sid][7]=t
                jobs[jid][7]=t
                s=live_s.pop(sid)
                j=live_j.pop(jid)
                pool_discard(jid)
                # The hired case itself is in both pending sets; close it without a
                # negative outcome, and record auto-cascades for the other cases.
                for other in s['pending']:
                    if other!=eid:finish(other,t,'withdrawn',auto=1)
                for other in j['pending']:
                    if other!=eid:finish(other,t,'ng',auto=1)
                finish(eid,t)
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
            if stage>=1:
                live_s[sid]['active']+=1;live_j[jid]['active']+=1
            heapq.heappush(queue,(origin+value,rng.random(),eid,stage))
        if focal_eid<0:raise ValueError('Focal introduction not found')
        process(resume['day']+1)
        if focal_eid not in cases:return chunks[0][focal_index].copy()
    first_day=first_day if resume is None else resume['day']+1
    last_day=config.days if stop_day is None else min(config.days,stop_day+1)
    for day in range(first_day,last_day):
        for jid in [i for i,v in live_j.items() if v['until']<=day]:
            j=live_j.pop(jid)
            pool_discard(jid)
            jobs[jid][7]=day
            for eid in list(j['pending']):finish(eid,day,'cancelled_posting_expiry')
        for sid in list(live_s):
            if sid not in live_s:continue
            if live_s[sid]['until']<=day:
                for eid in list(live_s[sid]['pending']):
                    if eid in cases and cases[eid][-1]==0:finish(eid,day,'cancelled_seeker_exit')
                natural(sid,day)
        for _ in range(config.seeker_arrivals):
            sid=len(seekers);skill=rng.random()
            # arrival, skill, desired salary, occupation, region, remote preference, hidden
            # motivation, departure (NaN while live)
            salary=max(200,300+800*skill+rng.gauss(0,100))
            occupation=rng.choice(OCCUPATIONS);region=rng.choice(REGIONS)
            remote=int(rng.random()<.5);motivation=rng.gauss(0,.5)
            if population_seekers is None:
                profile=[day,skill,salary,occupation,region,remote,motivation,np.nan]
            else:
                profile=list(population_seekers[sid]);profile[0]=day
            seekers.append(profile)
            if seeker_table is not None:seeker_table[sid]=seekers[-1]
            live_s[sid]=make_live_s(day+config.application_days)
        for _ in range(config.job_arrivals):
            jid=len(jobs);skill=rng.random();salary=max(200,300+800*skill+rng.gauss(0,150))
            # salary minimum is offered minus 100, clipped to 100.
            # TODO(issue #34 follow-up): redesign offered_salary with extra noise so it
            # is not trivially recoverable from skill; this issue keeps the current rule.
            occupation=rng.choice(OCCUPATIONS);region=rng.choice(REGIONS)
            remote=int(rng.random()<.5);motivation=rng.gauss(0,.5)
            if population_jobs is None:
                profile=[day,skill,salary,occupation,region,remote,motivation,np.nan,FEE_RATE_DEFAULT]
            else:
                profile=list(population_jobs[jid]);profile[0]=day
            jobs.append(profile)
            if job_table is not None:job_table[jid]=jobs[-1]
            live_j[jid]=make_live_j(day+config.posting_days)
            pool_add(jid)
        process(day+INTRODUCTION_OFFSET_DAYS)
        mode='builtin' if replay is None else replay.mode(day)
        selected=[]
        def add_introduction(sid,jid):
            """Build one introduction row; metadata mirrors the built-in draw exactly."""
            # Incremental counters replace two per-row scans over the pending sets.
            other=live_s[sid]['active']
            rivals=live_j[jid]['active']
            selected.append((sid,jid,day,np.nan,np.nan,np.nan,0.,0.,0.,other,rivals,
                             0.,0.,0.,0.,0.,0.,np.nan,np.nan,np.nan,0,0,
                             0,np.nan,0,np.nan))
        if mode=='none':
            # Maturation-only day: no new introductions (past the evaluation horizon).
            pass
        elif mode=='policy':
            # External ranking policy.  No rng draw is consumed for selection, so
            # the funnel stream is not perturbed by the number of introductions.
            for sid,jid in replay.pairs_for_day(day):
                # The recipe is evaluated against the frozen base dataset, not
                # against replay's own branch state.  Restore an actor that a
                # previous policy day removed but that the dataset still lists as
                # present on `day` (issue #37).  The schedule lookup and the live
                # dict mutation consume no rng draws.
                for kind,index,live,profiles in (('seekers',sid,live_s,seekers),('jobs',jid,live_j,jobs)):
                    if index in live or replay.departure is None:
                        continue
                    dep=replay.frozen_departure(kind,index)
                    if dep is not None and not dep>day:
                        continue
                    until=config.days+1 if dep is None else dep
                    live[index]=make_live_s(until) if kind=='seekers' else make_live_j(until)
                    if kind=='jobs':pool_add(index)
                    profiles[index][7]=np.nan
                s=live_s.get(sid)
                reason=None
                # Only the recipe's own conditions may exclude a pair.  A pair
                # outside `admissible` was never a candidate (e.g. a hand-written
                # policy), so it is the single legitimate exclusion reason.
                if not replay.is_admissible(day,sid,jid):
                    reason='not_in_recipe'
                elif s is None:reason='no_seeker'
                elif jid not in live_j:reason='no_item'
                else:
                    profile=seekers[sid];job=jobs[jid]
                    if profile[3]!=job[3]:reason='occupation'
                    elif not (job[4]==profile[4] or job[5]==1):reason='region'
                    elif max(100,job[2]-100)>profile[2]:reason='salary'
                    elif jid in s['seen']:reason='seen'
                if reason is not None and (replay.validate or reason in ('seen','not_in_recipe')):
                    replay.skipped.append((day,sid,jid,reason));continue
                # Do not record policy introductions in `seen`: the recipe's
                # NOT EXISTS test consults only base-run events, so the same pair
                # may legitimately be ranked again on a later evaluation day.
                add_introduction(sid,jid)
            replay.introduced[day]=[(int(r[0]),int(r[1])) for r in selected]
        else:
            for sid,s in live_s.items():
                if s['until']<=day:continue
                profile=seekers[sid]
                group=pool_groups.get((profile[3],profile[4]),())
                # All entries with effective minimum salary strictly below the
                # seeker's desired salary are eligible (the old searchsorted
                # 'left' semantics).  Sample directly from the *unseen* prefix
                # instead of rejection-sampling against an ever-growing `seen`
                # set: exactly `min(referrals, len(unseen))` draws are consumed.
                eligible=bisect_left(group,(profile[2],-1))
                if not eligible:continue
                unseen=[jid for _,jid in group[:eligible] if jid not in s['seen']]
                for _ in range(min(config.referrals,len(unseen))):
                    pos=rng.randrange(len(unseen))
                    jid=unseen[pos]
                    unseen[pos]=unseen[-1];unseen.pop()
                    s['seen'].add(jid)
                    add_introduction(sid,jid)
        rows=np.array(selected,dtype=DTYPE)
        chunks.append(rows)
        if len(rows):
            sa=seeker_table[:len(seekers)] if seeker_table is not None else np.asarray(seekers,dtype=object)
            ja=job_table[:len(jobs)] if job_table is not None else np.asarray(jobs,dtype=object)
            f=features(rows,sa,ja)
            gates=probability_arrays(base,f,sa[rows['seeker'],6].astype(float),ja[rows['job'],6].astype(float),anchors)
            rows['pa'],rows['ps'],rows['pd']=gates.prod(axis=-1).T
            rows['p_candidate_application'],rows['p_candidate_screening'],rows['p_candidate_decision']=gates[:,:,0].T
            rows['p_employer_application'],rows['p_employer_screening'],rows['p_employer_decision']=gates[:,:,1].T
            counterfactual(rows,day,latent_rng)
            for offset,row in enumerate(rows):
                if rng.random()>=row['pa']:
                    # The stage-0 gate (lottery) failed: the seeker never applies.
                    row['status']=OUTCOME_STATUS_CODES['not_applied']
                    row['outcome_dt']=day+INTRODUCTION_OFFSET_DAYS
                    continue
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
        if state_days is not None and day in state_days:
            state_days[day]=make_full_state(day,seekers,jobs,live_s,live_j,chunks,cases,
                                            queue,next_e,anchors,rng,latent_rng,
                                            daily_counts,stream_seed,config,pool_groups)
    if resume is not None:return chunks[0][focal_index].copy()
    rows=np.concatenate(chunks)
    if retain=='applications':rows=rows[np.isfinite(rows['application'])]
    return rows, np.asarray(seekers,dtype=object).reshape(-1, 8), np.asarray(jobs,dtype=object).reshape(-1, 9)


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


def make_full_state(day,seekers,jobs,live_s,live_j,chunks,cases,queue,next_e,anchors,
                    rng,latent_rng,daily_counts,seed,config,pool_groups=None):
    """Capture the complete mutable market state at the end of a day.

    Unlike :func:`make_snapshot` (a compact, index-remapped single-focal branch
    view), this state resumes the *whole* market without re-simulating earlier
    days.  Both RNG streams, the scheduled case heap and the live actor dicts
    are persisted so a replay loaded from this dict is byte-identical to one
    that started at day 0 (issue #40 item 4).  Shipped in a compact pickle by
    ``market_evaluation --freeze``; it is never distributed to participants.
    """
    return {
        'version': 1,
        'seed': seed,
        'signature': (config.seed, config.days, config.seeker_arrivals, config.job_arrivals,
                      config.application_days, config.posting_days, config.referrals),
        'day': int(day),
        'seekers': [list(profile) for profile in seekers],
        'jobs': [list(profile) for profile in jobs],
        'live_s': copy.deepcopy(live_s),
        'live_j': copy.deepcopy(live_j),
        'chunks': [chunk.copy() for chunk in chunks],
        'cases': dict(cases),
        'queue': list(queue),
        'next_e': int(next_e),
        'anchors': np.asarray(anchors).copy(),
        'rng_state': rng.getstate(),
        'latent_state': latent_rng.bit_generator.state,
        'daily_counts': {int(k): list(v) for k, v in daily_counts.items()},
        'pool_groups': ({key: list(entries) for key, entries in pool_groups.items()}
                        if pool_groups is not None else None),
    }


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
