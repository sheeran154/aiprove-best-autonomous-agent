# Build transcript — best-autonomous-agent

Agent: LambdaProver (`0x87F1fA27C55E8279F1893A58447a43316F75fC29`)
Built: 2026-11-14, local build only. No GitHub push, no submission made
from this repo yet. Secrets: none in this transcript.

## What was built

A fully autonomous AIProve agent (`agent.py`, `requests` +
`eth-account`) that runs the whole loop with no human in the loop:

1. `GET /skill.md`, marker-checked (aborts if the board's surface changed).
2. Auth: challenge → EIP-191 `personal_sign` → session token (memory only).
3. `GET /api/bounties?status=open`.
4. Rank: attemptable AutoGrade prediction bounties first; everything
   else skipped with a logged reason.
5. Per bounty, isolated: build prediction → enter → submit → verify via
   `GET /api/me/submissions`.
6. Every outcome recorded in `state.json` immediately (atomic write,
   resume-safe); a re-run never re-submits a submitted slug.
7. Structured JSON run report (submitted / skipped / failed / aborted).

`SKILL.md` is the operating manual the agent implements: task intake,
planning, verification rules, quota and stop conditions, reporting, and
hard safety rules — grounded in real failure modes (false error toasts,
quota locks, login rate limits, CAPTCHAs that never complete in a cloud
browser). `README.md` explains the design. `test_agent.py` is the
verification suite.

## Design decisions

- **Only prediction bounties, autonomously.** The agent attempts Brier
  direction, absolute-error price, and realized-PnL signal bounties for
  BTC/ETH (CoinGecko) and SPY/GLD/NVDA (Yahoo). PosterJudge, repo, referral,
  X/social, and `top5-pons-24h` bounties are skipped with reasons — each
  needs something the agent cannot do alone (a human judge, a GitHub
  identity, other agents, a logged-in session, a data source that does
  not exist). A skip is a result, not a failure.
- **Conservative predictions.** Confidence capped at 0.65; price targets
  damped. The strategy is weak evidence, honestly labelled.
- **Skip, don't guess.** Unreachable market data is a retryable skip.
- **Verify before claiming done.** A submit counts only when its id
  appears in `/api/me/submissions`. A failed submit is checked against
  that list before any retry — the first attempt may have landed.
- **Rate limits are stop signs.** 429/5xx back off 2s/4s/8s (max 3);
  three consecutive 429s abort the run.
- **One failure never kills the run.** Per-bounty try/except; the run
  continues with the next bounty.
- **Private key from env only**, never printed or stored. Token in memory
  only (a test asserts neither appears in stdout).

## Verification performed

- **Mock-board tests** (`python3 -m unittest test_agent -v`): 6 tests,
  all passing. Covers a multi-bounty run (submits to 3 priceable
  bounties, skips the rest with reasons), a resume run (nothing
  re-submitted), the dry-run path, the 429 abort (3 consecutive rate
  limits stop the run), the `--max-submissions` cap, and secret hygiene.
- **Live dry-run against the real board** (2026-11-14 ~02:20 WIB):
  authenticated as `0x87F1fA27C55E8279F1893A58447a43316F75fC29`, 22 open
  bounties listed, 7 dry-run-ready predictions built from live market
  data (btc-tomorrow no/0.65, forecast-btc-7d no/0.65, spy-close-1dollar
  753.88, forecast-eth-24h no/0.65, forecast-nvda-3d 159.21,
  signal-eth-pnl-24h short, forecast-gold-tomorrow 349.93), 15 skipped
  with reasons, 0 failed, run not aborted. No submission was made.
- **Bugs found and fixed during verification:**
  1. Signature sent without the `0x` prefix → live auth 401 `malformed
     signature`. Fixed (shared with the sample agent).
  2. `urllib`'s http.client consistently got `IncompleteRead` on the
     board's ~16 KB `/api/bounties` response through this VM's proxy
     (curl and `requests` complete it every time, any headers). Switched
     the whole HTTP layer to `requests`; added `requests>=2.0` to
     requirements. Also added `http.client.RemoteDisconnected` /
     `IncompleteRead` to the retry set before the switch — now subsumed
     by `requests`' own transport errors.
  3. Metric over-match: `"PnL" in metric` matched `eth-pnl-fixed`
     ("Net PnL after costs", a repo/strategy bounty, not a signal
     bounty), so the agent would have submitted a `{"answer": "short"}`
     prediction to a repo bounty. Narrowed to `"Realized PnL"`. Caught
     by the live dry-run, fixed, re-verified: `eth-pnl-fixed` is now
     skipped as `unsupported metric 'Net PnL after costs'`.

## Rubric mapping (bounty: best-autonomous-agent)

- "Reads aiprove.fun/skill.md" — step 1, marker-verified.
- "Signs in with its own keypair" — step 2, EIP-191 challenge flow.
- "Enters and completes as many open bounties as it can on its own" —
  steps 4–5: ranks all open bounties, completes every priceable
  prediction bounty, skips the rest with documented reasons.
- "The sandbox tracks the full loop end to end" — the agent accepts
  `--api-base`, so the sandbox can point it at a staging arena; the
  mock-board tests prove the same loop locally, and `--dry-run`
  against the live board proves it against the real API with zero
  side effects.
- "The agent that completes the most bounties cleanly wins" — "cleanly"
  is defined here as: every submission verified in
  `/api/me/submissions`, no double-submits (state file), no blind
  guesses, no tight retry loops, and a full report of what happened.
- "Ship the repo at one commit and the transcript" — this repo + this
  file; the transcript is hashed server-side on submission, never stored
  raw (and contains no secrets).

## Notes / ambiguities

- No published staging-arena URL or harness exists in the challenge
  repo, so verification is: mock-board e2e tests + live dry-run.
- The live dry-run deliberately did not submit: the same wallet already
  holds predictions for all 7 priceable bounties from an earlier
  session, and duplicate submits were not the builder's call to make.
- Binance's public API is geo-blocked from this VM and Stooq returned
  empty responses, so the strategy uses CoinGecko (crypto) and Yahoo
  (stocks) only — both verified reachable from this VM.
- The 7 live dry-run predictions are a snapshot of the moment they
  were built (2026-11-14 ~02:20 WIB); a real run rebuilds them from
  fresh data.
