import json
import os
import sys
from pathlib import Path


def report(path):
    r=json.loads((path/'results.json').read_text())
    b=r['best']
    s=b['settings']
    scale=r['scale']
    lines=['# 市場人数を維持する期間の試行','',
           '2026-11-09。日次流入を実規模換算で求職者5,000人・求人5,000件と仮定し、100分の1の市場で期間を探索した。商用実測値ではなく、人数設計のための初期仮定である。','',
           f'候補：応募可能{s["application_days"]}日、求人掲載{s["posting_days"]}日。1人1日最大5紹介。ウォームアップ{s["warmup_days"]}日、その後{s["measure_days"]}日を測定。','',
           '|応募可能日数|掲載日数|求職者の平均・実規模換算|求人の平均・実規模換算|',
           '|---:|---:|---:|---:|']
    for trial in r['search']:
        lines.append(f'|{trial["settings"]["application_days"]}|{trial["settings"]["posting_days"]}|{trial["population"]["seekers"]["mean"]/scale:,.0f}|{trial["population"]["jobs"]["mean"]/scale:,.0f}|')
    lines+=['','## 探索に使わなかったseedでの確認','',
            '|seed|求職者平均・換算|求人平均・換算|求職者前半→後半の平均・換算|',
            '|---:|---:|---:|---|']
    for trial in r['validation']:
        p=trial['population']
        lines.append(f'|{trial["settings"]["seed"]}|{p["seekers"]["mean"]/scale:,.0f}|{p["jobs"]["mean"]/scale:,.0f}|{p["seekers"]["first_half"]/scale:,.0f} → {p["seekers"]["second_half"]/scale:,.0f}|')
    lines+=['','## この試行が再現したこと','',
            '- 毎日の一定流入、応募可能期間終了後の結果待ち、全件終了での自然退出。',
            '- 複数紹介、同一求人への複数応募、求職者・求人の決定時の双方退出と関連選考の取消。',
            '- 求人の掲載期限での退出と取消。応募期限後にまだ未応募の紹介は取消。',
            '- 成功・失敗の選考判明日時を非負の正規分布から生成。応募3/2日、通過5/4日、決定30/20日。',
            '- 同じペアの基礎確率はIDとseedで固定し、紹介のたびに変えない。再紹介を禁止。','',
            '## 簡略化と解釈','',
            '今回は人数探索用パイロットで、詳細なプロフィール・学習スコア・競合状態による確率変動・oracleは実装していない。ペアの基礎確率は指定された各範囲の一様な固定乱数を使った。非応募の試行は結果待ちを延長しないため選考キューへの登録を省略したが、紹介数には含める。応募率1%に達する前に応募期限・求人掲載期限で取消される場合もある。','',
            '人数の平均を100倍した値は実規模の目安であり、10万人／20万件の本番シミュレーションで確認した値ではない。小規模市場と大規模市場では再紹介回避・競合・確率式の影響が変わり得る。また、小規模の日次ばらつきをそのまま100倍して本番の予測区間とみなさない。正式エンジンで同じ探索を再実行する必要がある。','',
            '流入数を増減すれば別の期間の組合せも可能。今回の期間は一意の正解ではない。目標人数への距離は両市場の相対誤差絶対値の和で比較し、結果を見て設計seedを選び直していない。','',
            '生の小規模市場の日次人数・イベント件数・平均退出在籍日数はresults.jsonに保存。人数保存則、紹介上限、応募ゼロ退出、乱数再現性、選考なしの場合の在籍人数についてテストした。','',
            '![別seedの日次人数](population.png)','',
            '図の縦軸は小規模市場の人数÷目標人数。1が目標。','',
            '再現には `python -m synthetic_funnel.market_population_pilot --output runs/market_population_pilot_v1` を使う。']
    (path/'REPORT.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')
    config=dict(status='pilot_candidate_not_production_validated',new_seekers_per_day=5000,new_jobs_per_day=5000,
                application_window_days=s['application_days'],job_posting_days=s['posting_days'],max_referrals_per_seeker_per_day=5,
                target_seekers=100000,target_jobs=200000,source='market_population_pilot_v1',scale_used=.01)
    config_path=path/'candidate_world_config.json'
    existing=json.loads(config_path.read_text(encoding='utf-8-sig')) if config_path.exists() else {}
    if not existing.get('status','').startswith('user_selected'):
        config_path.write_text(json.dumps(config,indent=2),encoding='utf-8')
    os.environ['MPLCONFIGDIR']=str((path/'mpl').resolve())
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    fig,axes=plt.subplots(2,1,figsize=(10,6),constrained_layout=True)
    for trial in r['validation']:
        for ax,key,target in zip(axes,('seekers','jobs'),(1000,2000)):
            ax.plot([d['day']-s['warmup_days'] for d in trial['daily']],[d[key]/target for d in trial['daily']],label=f'seed {trial["settings"]["seed"]}',alpha=.8)
            ax.set(title=key,ylabel='Population / target',xlabel='Measurement day')
    for ax in axes:
        ax.set_ylim(.9,1.15)
        ax.axhline(1,color='black',linestyle='--')
        ax.legend()
    fig.savefig(path/'population.png',dpi=150)
    plt.close(fig)
    print(path/'REPORT.md')


if __name__=='__main__':
    report(Path(sys.argv[1]))
