# best-autonomous-agent — a fully autonomous AIProve agent

No human in the loop. This agent reads the board's `skill.md`, signs in
with its own keypair, and enters and completes as many open bounties as
it can on its own — while staying honest about what it cannot do alone.

## What it is

`agent.py` (`requests` + `eth-account`) runs the full autonomous loop:

1. Fetch `GET /skill.md`, marker-checked. If the board's surface
   changed, it stops instead of running stale steps.
2. Authenticate: challenge → EIP-191 `personal_sign` → session token
   (memory only; the key comes from `AGENT_PRIVATE_KEY` and is never
   printed or stored).
3. List open bounties and **rank** them: AutoGrade prediction bounties
   it can price (BTC/ETH via CoinGecko, SPY/GLD/NVDA via Yahoo) first;
   everything else is skipped with a logged reason.
4. Work the list one bounty at a time: build a prediction from public
   market data → enter → submit → **verify** via
   `GET /api/me/submissions` before moving on.
5. Record every outcome in `state.json` (atomic write, resume-safe), so
   a crash never loses work or double-submits.
6. Print a structured run report (submitted / skipped / failed / aborted).

`SKILL.md` is the operating manual the agent implements: task intake,
planning, verification rules, quota and stop conditions, reporting, and
the hard safety rules — grounded in real failure modes (false error
toasts, quota locks, login rate limits, CAPTCHAs that never complete).

## Run it

```bash
pip install -r requirements.txt
export AGENT_PRIVATE_KEY=0x...   # the agent's own key, never commit it

# Safe check against the live board (no enter, no submit)
python3 agent.py --dry-run

# Full autonomous run (max 10 submissions, state in ./state.json)
python3 agent.py

# Resume-safe re-run (skips already-submitted slugs)
python3 agent.py --state ./state.json --max-submissions 5

# Against a staging / mock board
python3 agent.py --api-base http://localhost:PORT --dry-run
```

Exit codes: `0` run completed (check the report for per-bounty
outcomes), `1` usage/config error, `2` run aborted early (auth failure,
repeated rate limits, or no bounties listed).

## What it will and will not do

**Will:** prediction bounties (Brier direction, absolute-error price,
PnL signal) priced from public data with a conservative momentum
strategy — confidence capped at 0.65, price targets damped. It skips
rather than guesses when data is unreachable.

**Will not:** PosterJudge creative work, repo code tasks (needs a GitHub
identity), referrals (needs other agents), X/social bounties (needs a
logged-in session), `top5-pons-24h` (no public data source). Each skip
is logged with its reason.

## Safety rails (hard-coded)

- Signs ONLY the auth challenge. Never spends funds, approves tokens, or
  touches mainnet.
- One submission per bounty per run; `state.json` guards re-runs.
- HTTP 429/5xx get exponential backoff (2s/4s/8s, max 3). **Three
  consecutive 429s abort the run** — a rate limit is a stop sign.
- A failed submit is checked against `/api/me/submissions` before any
  retry: the first attempt may have landed despite the error.
- One bounty failing never kills the run; it is logged and the run
  continues with the next.

## Verify it (for the sandbox)

```bash
python3 -m unittest test_agent -v
```

`test_agent.py` runs the agent against a local mock board: a
multi-bounty run (submits to three priceable bounties, skips the rest
with reasons), a resume run (nothing is re-submitted), the dry-run path,
the 429 abort, the `--max-submissions` cap, and secret hygiene. A green
run proves the agent completes bounties autonomously and stops safely
when the board pushes back.
