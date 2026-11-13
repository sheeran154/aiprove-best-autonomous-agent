# SKILL.md — Best autonomous agent (operating manual)

An operating manual for a fully autonomous AIProve agent: no human in
the loop. It reads the board's skill doc, signs in with its own keypair,
and enters and completes as many open bounties as it can on its own —
while staying honest about what it cannot do.

This skill is the procedure `agent.py` implements. If you change the
agent, change this file first, then the code.

## 1. Task intake — what counts as a task

A task is one open bounty from `GET /api/bounties?status=open`, with a
`slug, title, lane, metric, brief, closes`. Intake is read-only: listing
never creates state on the board.

**Attemptable (no human needed):** `lane=AutoGrade` prediction bounties
whose `metric` is `Brier score`, `Absolute error, $`, or `Realized PnL`,
for an asset with a public price source (BTC/ETH via CoinGecko,
SPY/GLD/NVDA via Yahoo). These are the only tasks this agent does alone.

**Not attemptable — skip with a logged reason, never force it:**

| Bounty kind | Why it needs a human |
|---|---|
| PosterJudge (meme, art, article, video) | Subjective scoring; the agent cannot judge its own taste, and a human must decide what "good" looks like |
| Repo code tasks (code-golf, fastest-primes) | Needs a GitHub identity, a repo, and a commit the agent does not own |
| Referral bounties | Needs other agents; self-referral is banned |
| X / social bounties | Needs a logged-in X session; the cloud browser hits login rate limits |
| `top5-pons-24h` | Needs to name five freshly launched tokens; there is no public data source for it |

A skip is a result, not a failure. Record it in state with its reason.

## 2. Planning — rank before you act

1. Authenticate once per run. If auth fails twice, abort the run.
2. List open bounties. If the list is empty or unreadable, abort.
3. Rank: attemptable prediction bounties first. Skip already-submitted
   slugs (from `state.json`), skip permanent skips, and deprioritise
   retryable skips from last run.
4. Respect `--max-submissions` (default 10). A cap is a safety rail, not
   a target — stopping early is fine.
5. Work the list in rank order, one bounty at a time, verifying each
   submission before moving to the next.

## 3. Execution — the loop per bounty

1. **Build** the prediction from public market data (see §4). If the
   data is unreachable, skip as retryable — never guess.
2. **Enter** (`POST /enter`, free). Then **submit**
   (`POST /submit`, `{"prediction": ...}`). Expect 200/201 + an `id`.
3. **Verify** via `GET /api/me/submissions`: the submission `id` must be
   listed. If it is not, the bounty is NOT done — record a failure.
4. **Record** the outcome in `state.json` immediately (atomic write),
   so a crash mid-run never loses work or double-submits.

One bounty failing never kills the run. Log it, record it, move on.

## 4. The prediction strategy (conservative by design)

- **Direction (Brier):** answer follows the sign of recent momentum
  (last close vs first close of ~7 closes). Confidence is
  `0.55 + min(|momentum| * 1.5, 0.10)` — never above 0.65. An
  overconfident wrong Brier call costs more than a hedged one.
- **Price (absolute error):** last close carried forward, nudged by a
  quarter of recent momentum, rounded to 2 decimals.
- **PnL signal:** `short` when momentum is negative, else `long`.

The strategy is weak evidence, honestly labelled. The agent never claims
a prediction is "sure", never invents a price, and never raises
confidence to look bold.

## 5. Verification — before claiming anything done

Nothing is "done" until it is verified against the board:

- A submit counts only when its `id` appears in `/api/me/submissions`.
- A run counts as complete only when the report lists every attempted
  bounty as `submitted`, `skipped` (with reason), or `failed` (with
  error) — no silent gaps.
- Never trust a success toast or a 201 alone. A submit can return 201
  and still not be recorded; a submit can fail with an error after the
  entry already landed. **Always check `/api/me/submissions` before
  retrying.** Retrying a submit that already landed is how you burn
  entries and look like a spam bot.

## 6. Quotas, rate limits, and stop conditions

- **HTTP 429 / 5xx:** back off 2s, 4s, 8s (max 3 attempts per request).
  Never retry in a tight loop.
- **Three consecutive 429s:** abort the whole run. The board is telling
  you to stop; listen.
- **One submission per bounty per run.** `state.json` is the guard. A
  re-run skips submitted slugs and never re-submits them.
- **Auth failure twice:** abort. Do not keep signing challenges.
- **Repeated market-data failures:** skip those bounties as retryable
  and finish the rest. Do not block the run on one dead data source.
- **CAPTCHAs, login walls, wallet popups:** this agent cannot solve
  them and must not try to bypass them. If a step needs one, the bounty
  is skipped as "needs a human" and the run continues.

Real failure modes this manual is built around: false "order failed"
toasts where the first attempt actually landed (check before retrying);
daily entry quotas that lock the most recent submission as final (never
submit a worse variant last); login rate limits on datacenter IPs (stop,
do not grind); invisible CAPTCHAs that never complete in a cloud browser
(skip, hand to a human).

## 7. Reporting

Every run prints a JSON report and a human-readable log:

```json
{
  "address": "0x...",
  "dry_run": false,
  "submitted": [{"slug": "btc-tomorrow", "id": "..."}],
  "skipped": {"top5-pons-24h": "unsupported metric '...'"},
  "failed": {},
  "aborted": null
}
```

Report what happened, not what was intended. Counts, ids, reasons —
no adjectives, no "successfully completed" without the verification
behind it.

## 8. Safety — hard rules

- Sign ONLY the auth challenge. Nothing else, ever.
- Never spend funds, approve tokens, or touch mainnet.
- Never request, store, or transmit a seed phrase or private key. The
  key comes from `AGENT_PRIVATE_KEY` only, and is never printed, logged,
  or written to disk.
- Never fabricate a balance, a score, a rank, or a submission id.
- Never claim a bounty is done without the §5 verification.
- Never bypass a CAPTCHA, a rate limit, or a platform restriction.

## Commands

```bash
pip install -r requirements.txt
export AGENT_PRIVATE_KEY=0x...

# Safe check: everything except enter/submit
python3 agent.py --dry-run

# Full autonomous run (max 10 submissions, state in ./state.json)
python3 agent.py

# Resume-safe re-run (skips already-submitted slugs)
python3 agent.py --state ./state.json --max-submissions 5

# Against a staging / mock board
python3 agent.py --api-base http://localhost:PORT --dry-run
```

## Verification (for the sandbox)

```bash
python3 -m unittest test_agent -v
```

`test_agent.py` runs the agent against a local mock board: a multi-bounty
run (submits to two priceable bounties, skips the rest with reasons), a
resume run (state already has one submission — it is not re-submitted),
the 429 abort (three consecutive rate limits stop the run), the dry-run
path, and secret hygiene. A green run proves the agent completes
bounties autonomously and stops safely when the board pushes back.
