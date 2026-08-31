# Setting this up in GitHub

This assumes you have a GitHub account and (for pushing files) either the
GitHub web UI or `git` installed locally. Two scenarios are covered below —
skip to whichever applies.

## A. You already have a repo running the v3 script on GitHub Actions

If v3 is already live, this is a small update, not a new setup:

1. Replace `scripts/rotation_sweep_v3_full500.py` with the new version, and
   add `scripts/indicators.py`, `scripts/scoring.py`,
   `scripts/risk_management.py` alongside it (same folder).
2. Add `tests/` somewhere in the repo (e.g. at the repo root) if you want
   CI to run them — optional but recommended.
3. Update `requirements.txt` (still just `requests`) and add
   `requirements-dev.txt` if you're adding the test workflow.
4. Your existing workflow file's `python scripts/rotation_sweep_v3_full500.py`
   step doesn't need to change — the entry point and `data/state.json`
   location are unchanged.
5. Your existing `data/state.json` doesn't need to be reset — the new
   script reads the same `universe`/`coverage` keys and adds new ones on
   its own.
6. Read `CHANGELOG.md` for the couple of behavior changes that affect
   consumers of the output (schema consistency, fetch-failure handling).
7. Optionally drop in `.github/workflows/nightly_sweep.yml` and `tests.yml`
   from this package if you'd rather standardize on them than keep
   maintaining your own — compare against what you have first.

Skip to **C** for the settings/troubleshooting notes either way.

## B. Starting from scratch (no repo yet)

1. **Create the repo.** On GitHub: New repository → give it a name (e.g.
   `nse-swing-scanner`) → private is fine, this doesn't need to be public →
   Create.

2. **Add the files**, keeping this layout:
   ```
   scripts/rotation_sweep_v3_full500.py
   scripts/indicators.py
   scripts/scoring.py
   scripts/risk_management.py
   data/state.json          <- you create this, see step 3
   requirements.txt
   requirements-dev.txt     (optional, for the test workflow)
   tests/                   (optional, for the test workflow)
   .github/workflows/nightly_sweep.yml
   .github/workflows/tests.yml   (optional)
   ```
   Easiest path: clone the empty repo locally, copy these files in, then
   `git add . && git commit -m "Initial scanner setup" && git push`. (Or
   use GitHub's "Add file → Upload files" in the web UI if you'd rather
   not use git locally.)

3. **Seed `data/state.json`.** This repo ships `data/state.json.example`
   with 3 illustrative rows — rename it to `data/state.json` and replace
   the `universe` list with your actual Nifty 500 constituents and their
   ISINs. **This part isn't something to generate from a language model**:
   the constituent list changes periodically (index reconstitutions) and
   getting an ISIN wrong silently breaks that one symbol's fetch every
   night, so pull it from NSE's official Nifty 500 list and Upstox's
   published instrument master (or reuse whatever list your v3 setup was
   already using, if you have one from before). `coverage` starts as `{}`.

4. **Push the workflow files** (`.github/workflows/nightly_sweep.yml` and
   optionally `tests.yml`) as part of the same commit.

5. Continue to **C** below.

## C. Repository settings, either way

- **Actions write permission.** The workflow declares
  `permissions: contents: write`, which is normally sufficient for the job
  to commit `data/state.json` back. If the push step still fails with a
  permissions error, check Settings → Actions → General → Workflow
  permissions, and set it to "Read and write permissions" — some
  organizations lock this down at the org level, which overrides the
  per-workflow setting and needs an org admin to adjust.

- **Branch protection.** If your default branch has protection rules
  (required PR reviews, required status checks) the Action's direct
  `git push` will be rejected the same way a human's direct push would be.
  Either exempt `github-actions[bot]` from those rules for this repo, or
  point the workflow at a separate unprotected branch (e.g. `data`) and
  read from that branch instead of `main` in whatever consumes
  `data/state.json` downstream.

- **Test it manually before trusting the schedule.** Go to the Actions tab
  → "Nightly NSE Swing Scan" → "Run workflow" → run it on demand once.
  Confirm `data/state.json` actually gets updated and committed before
  relying on the 5:15pm IST cron trigger.

- **Optional position-sizing config.** If you want `suggested_qty` and
  `suggested_position_risk_pct` populated, add repository Variables
  (Settings → Secrets and variables → Actions → Variables tab —
  "Variables," not "Secrets," is fine here since these aren't sensitive
  credentials) named `SWING_ACCOUNT_SIZE` and `SWING_RISK_PER_TRADE_PCT`.
  Leave them unset to keep those fields `null`, which is the default.

- **Verify the benchmark instrument keys.** `scripts/rotation_sweep_v3_full500.py`
  uses `"NSE_INDEX|Nifty 50"` / `"NSE_INDEX|Nifty 500"` for the market-regime
  fetch — a reasonable guess based on Upstox's `NSE_EQ|<isin>` convention
  for equities, but unverified from this environment (no network access to
  Upstox here). After your first live run, check
  `state["market_regime"]["regime"]` in the committed `data/state.json` —
  if it's `"unknown"`, the index fetch is failing and those two constants
  are the first thing to check against Upstox's current instrument master.

- **Cron timing.** `45 11 * * 1-5` is 11:45 UTC, Monday–Friday, chosen to
  land after the 3:30pm IST close with a buffer. GitHub's scheduled
  triggers are best-effort and can run several minutes late under
  platform load — this is normal, not a misconfiguration, and one more
  reason the manual `workflow_dispatch` test in the step above is worth
  doing first.

- **First-run cost/duration.** A full 500-symbol sequential sweep with the
  built-in politeness delay will typically take several minutes; the
  workflow's `timeout-minutes: 30` gives headroom above that. If it's
  regularly running close to the timeout, that's worth investigating
  before extending the limit.
