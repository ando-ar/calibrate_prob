# Test reduction report (issue #20)

## Scope and method

Applied the `python-test-reduction` skill (pytest-gremlins mutation-informed
reduction). Only `synthetic_funnel/test/` tests added during this effort plus
duplicated coverage in `test_market.py` were considered. No `calibration_pipeline`
tests, no `src/` production code, and no pytest-gremlins configuration in the
project were changed. Each candidate was evaluated **one at a time**; a deletion
was adopted only when the baseline-detected mutation set (exact `gremlin_id`s,
same target/operators/settings) was still fully detected afterwards, with no
`zapped -> survived/error/timeout/pardon` transitions. Tests that guard real
bugs or encode spec guarantees (issue #38/#40 timing budgets, exact row counts
and golden determinism hashes) were kept by default.

### Environment / fixed comparison settings

- Worktree HEAD and base branch tip: `c5a67e3` (verified before starting).
- uv 0.9.30, CPython 3.14.3, pytest 9.2.0, pytest-gremlins 1.14.3
  (installed ad hoc with `uv run --with pytest-gremlins`; not added to dev deps),
  coverage 7.17.3, numpy 2.5.4.
- Gremlin targets: one source module per candidate (`market.py` 399 gremlins,
  `gui/server.py` 213 gremlins). Operators: all five defaults (arithmetic,
  boolean, boundary, comparison, return). No excludes, no pardons, no cache,
  coverage-guided selection on, `--gremlin-parallel --gremlin-workers=12`,
  `--gremlin-mutant-timeout=40`, `--gremlin-report=json`. The same settings were
  used before and after every candidate. Raw reports and full metadata live in
  the ignored working dir `synthetic_funnel/runs/reduce/`.

### Time budget

Budget declared for mutation verification: **~90 minutes**. Actual use was
~25 minutes of mutation runs (7 gremlin runs: one pilot, six paired/final
comparisons), well inside the budget. Stopped once promising candidates were
evaluated; no exact-minimal search was attempted.

## Candidates

### A. Delete `test_market.py::MarketTest::test_daily_stats_funnel_totals` — REJECTED

- Guarantee recorded: `daily_stats` has one 9-field entry per day and the
  funnel sums equal the row count, arrival counts and finite-stage counts. The
  same assertions appear verbatim in
  `test_issue40_performance.py::MarketStructuralInvariantTest::test_funnel_and_foreign_key_invariants`
  (which also checks FK integrity, stage ordering and the referral cap, and is
  a #40 regression guard, so it was kept).
- Mutation on `market.py`, selection = the candidate + the #40 test:
  baseline **71/399 zapped**, 328 survived, 0 error/timeout; after deletion
  **68/399 zapped**, 331 survived.
- Lost kills: `market_8684_g030`, `market_8684_g034` (both `market.py:81`,
  `MarketConfig.warmup` comparison/boundary) and `market_8684_g189`
  (`market.py:434`, stage-transition boolean). All three went `zapped ->
  survived` and are exactly the kind of transition the skill forbids using as
  adoption evidence. The change was reverted; `test_market.py` is unchanged.
- Reports: `runs/reduce/candidateA_baseline_market.json`,
  `runs/reduce/candidateA_after_market.json`.

### B. Merge `test_gui.py::RunInspectionTest::test_market_manifest_summary` and
`test_row_counts_are_dynamic_including_optional_latent_tables` — ADOPTED

- Guarantees recorded: (1) `market_counts_table` renders fixed labels and
  values (`求職者（user）`, `events_latent`, `12`) for a standard manifest;
  (2) it also renders optional/unknown row-count keys and their values.
  Merged into `test_market_counts_table_static_and_dynamic_keys` with **every
  original assertion retained** (same two manifests, same asserts).
- Mutation on `gui/server.py`, selection = the two tests, then the merged one:
  baseline **9/213 zapped**, 204 survived; after **9/213 zapped**, 204 survived.
  Identical zapped-id set; zero status changes, zero errors/timeouts.
- Reports: `runs/reduce/candidateB_baseline_gui.json`,
  `runs/reduce/candidateB_after_gui.json`.

### C. Merge `test_gui.py::PageRenderingTest::test_pages_render_all_tabs_with_defaults`
and `test_market_form_exposes_all_fields_and_no_legacy_fields` — ADOPTED

- Guarantees recorded: (1) both tabs render with no unresolved `${` template
  markers, the results tab shows its empty-state text, and the market form
  has no legacy fields; (2) the market form exposes every current field and
  the `--seeker-arrivals`/`--job-arrivals` CLI hints. Duplicated rendering and
  `legacy_models`/`retain` asserts were folded into
  `test_pages_render_all_tabs_and_market_form_fields`; all original assertions
  retained.
- Mutation on `gui/server.py`, selection = the two tests, then the merged one:
  baseline **30/213 zapped**, 183 survived; after **30/213 zapped**, 183 survived.
  Identical zapped-id set; zero status changes, zero errors/timeouts.
- Reports: `runs/reduce/candidateC_baseline_gui.json`,
  `runs/reduce/candidateC_after_gui.json`.

### Candidates inspected but not run (kept)

- `test_issue38_performance.py` / `test_issue40_performance.py`: all tests kept;
  timing-budget assertions and golden byte/determinism checks are spec
  guarantees, and none showed full assertion-level duplication with another test.
- `test_daily_candidates.py`: the two snapshot-count tests overlap on the
  `other_applications` SQL predicate but differ in boundary (DAY vs D+1) and in
  that one validates a live `simulate()` snapshot and `competing_candidates`.
  Kept. (`market_candidates.py` only has 6 gremlins, all on SQL string
  concatenation, so mutation there cannot decide this guarantee.)
- `test_seed_batch.py`: byte-identity, CLI batch and argument-validation tests
  exercise distinct entry points and distinct source lines. Kept.
- `test_market_export.py` / `test_market_evaluation.py` / `test_market_replay.py`
  / `test_market.py` outcome/fee family: distinct layers (raw simulate, export
  formats, replay, evaluator), configs and assertions. Kept.

## Final verification

- Full suite: **106 passed in 40.33s** (wall 41.2s), down from **108 passed in
  41.67s** (wall 42.5s). Delta: -2 collected cases, ~1.3s faster (within noise;
  the dataset fixtures dominate runtime).
- Final mutation check: `gui/server.py` with all 22 remaining GUI tests
  selected, cache explicitly cleared — **151/213 zapped**, 62 survived,
  0 error/timeout/pardon. Both adopted candidates' detected sets are subsets of
  this final set. Report: `runs/reduce/final_gui.json`.
- No experimental skips or temporary changes remain; the only source-file
  change is the two merges in `test/test_gui.py`.

## Conclusion

Under this mutation configuration and the recorded spec checks, the two adopted
merges preserved every assertion and every baseline-detected mutation; the one
deletion candidate was rejected because it lost three detected mutations. This
is a guarantee claim for the mutation set and specs checked here, not a claim of
equivalence against all possible faults.
