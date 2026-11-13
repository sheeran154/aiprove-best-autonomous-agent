#!/usr/bin/env python3
"""Best autonomous AIProve agent — no human in the loop.

Reads the board's skill.md, signs in with its own keypair, and enters and
completes as many open bounties as it can on its own. It only attempts
bounties it can actually complete autonomously (AutoGrade prediction
bounties priced from public market data) and skips the rest with a logged
reason — never a blind guess, never a fake entry.

    1. fetch  GET /skill.md            (read the board's own instructions)
    2. auth   challenge -> EIP-191 sign -> session token
    3. list   GET /api/bounties?status=open
    4. rank   prediction bounties first; skip what it cannot do alone
    5. loop   for each priceable bounty: build -> enter -> submit -> verify
    6. state  record every outcome in state.json (resume-safe)
    7. report print a structured run report

Usage:
    AGENT_PRIVATE_KEY=0x... python3 agent.py [--dry-run] [--max-submissions N]
    AGENT_PRIVATE_KEY=0x... python3 agent.py --state ./state.json

--dry-run does everything except enter/submit. State is kept in state.json
(default: ./state.json); a re-run skips bounties already submitted and
retries only the ones that failed for a retryable reason.

Safety rules (hard-coded, not configurable):
- Signs ONLY the auth challenge. Never signs anything else.
- Never spends funds, approves tokens, or touches mainnet.
- One submission per bounty per run; never re-submits a bounty already in
  state as submitted.
- HTTP 429/5xx get exponential backoff (2s/4s/8s, max 3). Three
  consecutive 429s end the run — a rate limit is a stop sign.
- A failed submit is checked against /api/me/submissions before any
  retry: the first attempt may have landed despite the error.

Exit codes: 0 = run completed (at least the loop ran; see report),
            1 = usage/config error, 2 = run aborted early (auth failure,
                repeated rate limits, or no bounties listed).
"""

import argparse
import json
import os
import sys
import time
import requests

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
DEFAULT_API_BASE = "https://aiprove.fun"
DEFAULT_STATE = "state.json"
MAX_RETRIES = 3
MAX_CONSECUTIVE_429 = 3


# --------------------------------------------------------------------------
# HTTP helpers (requests, not urllib)
# --------------------------------------------------------------------------
# NOTE: urllib's http.client consistently gets IncompleteRead on the
# board's ~16 KB /api/bounties response through this VM's proxy, while
# curl and requests complete it every time. All HTTP therefore goes
# through `requests` (urllib3). Retry/backoff policy is unchanged:
# 429/5xx back off 2s, 4s, 8s (max 3 attempts); a persistent 429 raises
# RateLimited instead of being retried in a tight loop.


class RateLimited(Exception):
    """Raised when a request kept hitting 429 after backoff."""


def http_json(method, url, body=None, token=None):
    """One JSON request with exponential backoff on 429 / 5xx.

    Returns (status, parsed_json). Raises RateLimited on a persistent
    429, RuntimeError after repeated transport failures.
    """
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    for attempt in range(MAX_RETRIES):
        try:
            resp = requests.request(method, url, json=body, headers=headers,
                                    timeout=30)
        except requests.exceptions.RequestException as exc:
            if attempt < MAX_RETRIES - 1:
                wait = 2 ** (attempt + 1)
                print(f"  [!] network error ({exc}) — backing off {wait}s",
                      file=sys.stderr)
                time.sleep(wait)
                continue
            raise RuntimeError(f"request failed: {exc}") from exc
        if resp.status_code in (429, 500, 502, 503) and attempt < MAX_RETRIES - 1:
            wait = 2 ** (attempt + 1)
            print(f"  [!] HTTP {resp.status_code} on {method} {url} — "
                  f"backing off {wait}s", file=sys.stderr)
            time.sleep(wait)
            continue
        if resp.status_code == 429:
            raise RateLimited(f"HTTP 429 on {method} {url} after backoff")
        try:
            return resp.status_code, resp.json()
        except ValueError:
            return resp.status_code, {"error": f"HTTP {resp.status_code}"}
    raise RuntimeError(f"request failed after {MAX_RETRIES} attempts")


def http_text(url):
    """GET a text document (skill.md) with the same backoff as http_json."""
    for attempt in range(MAX_RETRIES):
        try:
            resp = requests.get(url, headers={"User-Agent": USER_AGENT},
                                timeout=30)
        except requests.exceptions.RequestException as exc:
            if attempt < MAX_RETRIES - 1:
                time.sleep(2 ** (attempt + 1))
                continue
            raise RuntimeError(f"GET {url} failed: {exc}") from exc
        if resp.status_code in (429, 500, 502, 503):
            if attempt < MAX_RETRIES - 1:
                time.sleep(2 ** (attempt + 1))
                continue
            if resp.status_code == 429:
                raise RateLimited(f"HTTP 429 on GET {url} after backoff")
            raise RuntimeError(f"GET {url} failed: HTTP {resp.status_code}")
        return resp.text
    raise RuntimeError(f"GET {url} failed after {MAX_RETRIES} attempts")


# --------------------------------------------------------------------------
# Market data (public, no keys)
# --------------------------------------------------------------------------

def coingecko_closes(coin_id, days=7):
    url = (f"https://api.coingecko.com/api/v3/coins/{coin_id}/market_chart"
           f"?vs_currency=usd&days={days}&interval=daily")
    _, payload = http_json("GET", url)
    prices = payload.get("prices") or []
    return [p[1] for p in prices if len(p) == 2]


def yahoo_closes(symbol, range_="5d"):
    url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
           f"?range={range_}&interval=1d")
    _, payload = http_json("GET", url)
    try:
        result = payload["chart"]["result"][0]
        closes = result["indicators"]["quote"][0]["close"]
        return [c for c in closes if c is not None]
    except (KeyError, IndexError, TypeError):
        return []


ASSET_MAP = {
    "btc": lambda: coingecko_closes("bitcoin"),
    "eth": lambda: coingecko_closes("ethereum"),
    "spy": lambda: yahoo_closes("SPY"),
    "gold": lambda: yahoo_closes("GLD"),
    "gld": lambda: yahoo_closes("GLD"),
    "nvda": lambda: yahoo_closes("NVDA"),
}


def momentum(closes):
    if len(closes) < 3 or not closes[0]:
        return None
    return closes[-1] / closes[0] - 1.0


def build_prediction(slug, metric):
    """Build a prediction payload, or return (None, reason, retryable).

    Strategy (documented, deliberately conservative):
    - Direction (Brier): answer follows momentum sign; confidence
      0.55 + min(|momentum| * 1.5, 0.10), never above 0.65.
    - Price (absolute error): last close nudged by 25% of momentum.
    - PnL signal: "short" when momentum is negative, else "long".
    Unreachable market data is a retryable skip, never a guess.
    """
    if not any(k in metric for k in ("Brier", "Absolute error", "Realized PnL")):
        return None, f"unsupported metric '{metric}'", False
    fetch = next((f for k, f in ASSET_MAP.items() if k in slug), None)
    if fetch is None:
        return None, f"no market-data source mapped for slug '{slug}'", False
    try:
        closes = fetch()
    except Exception as exc:  # noqa: BLE001 - data source down, skip honestly
        return None, f"market data fetch failed: {exc}", True
    mom = momentum(closes)
    if mom is None or not closes:
        return None, "not enough market data to price this bounty", True
    if "Brier" in metric:
        answer = "yes" if mom >= 0 else "no"
        confidence = round(0.55 + min(abs(mom) * 1.5, 0.10), 2)
        return {"answer": answer, "confidence": confidence}, None, False
    if "Absolute error" in metric:
        return {"answer": round(closes[-1] * (1 + mom * 0.25), 2)}, None, False
    return {"answer": "long" if mom >= 0 else "short"}, None, False


# --------------------------------------------------------------------------
# State (resume-safe, atomic writes)
# --------------------------------------------------------------------------

def load_state(path):
    try:
        with open(path) as f:
            state = json.load(f)
        state.setdefault("submitted", {})
        state.setdefault("skipped", {})
        return state
    except (FileNotFoundError, json.JSONDecodeError):
        return {"submitted": {}, "skipped": {}}


def save_state(path, state):
    tmp = f"{path}.tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2, sort_keys=True)
    os.replace(tmp, path)


# --------------------------------------------------------------------------
# Board client
# --------------------------------------------------------------------------

def fetch_skill(api_base):
    text = http_text(f"{api_base}/skill.md")
    for marker in ("Authenticate", "/api/auth/challenge", "/api/bounties"):
        if marker not in text:
            raise RuntimeError(f"skill.md missing expected marker: {marker}")


def authenticate(api_base, private_key):
    from eth_account import Account
    from eth_account.messages import encode_defunct

    account = Account.from_key(private_key)
    address = account.address
    _, data = http_json("GET", f"{api_base}/api/auth/challenge?address={address}")
    challenge = data.get("challenge")
    if not challenge:
        raise RuntimeError(f"no challenge returned: {data}")
    signature = "0x" + account.sign_message(
        encode_defunct(text=challenge)).signature.hex()
    status, data = http_json("POST", f"{api_base}/api/auth/verify",
                             {"address": address, "signature": signature})
    token = data.get("token")
    if status != 200 or not token:
        raise RuntimeError(f"auth verify failed ({status}): {data}")
    return address, token


def list_open_bounties(api_base):
    _, data = http_json("GET", f"{api_base}/api/bounties?status=open")
    if isinstance(data, dict):
        return data.get("bounties", data.get("items", []))
    return data or []


def my_submission_ids(api_base, token):
    _, mine = http_json("GET", f"{api_base}/api/me/submissions", token=token)
    subs = mine.get("submissions", mine if isinstance(mine, list) else [])
    return {s.get("id") for s in subs}


def rank_bounties(bounties, state):
    """Split open bounties into (attemptable, skipped).

    Attemptable: open AutoGrade prediction bounties, not already submitted.
    Everything else is skipped with a reason. Skips for unsupported
    metrics/assets are permanent for that slug; data failures are
    retryable and decided at build time, not here.
    """
    attempt, skipped = [], []
    for b in bounties:
        slug = b.get("slug", "")
        metric = b.get("metric", "")
        if slug in state["submitted"]:
            skipped.append((slug, "already submitted (state)"))
            continue
        if slug in state["skipped"] and not state["skipped"][slug].get("retryable"):
            skipped.append((slug, state["skipped"][slug]["reason"]))
            continue
        if b.get("lane") != "AutoGrade":
            skipped.append((slug, f"lane {b.get('lane')} needs a human judge"))
            continue
        if not any(k in metric for k in ("Brier", "Absolute error", "Realized PnL")):
            skipped.append((slug, f"unsupported metric '{metric}'"))
            continue
        if not any(k in slug for k in ASSET_MAP):
            skipped.append((slug, "no market-data source for this slug"))
            continue
        attempt.append(b)
    return attempt, skipped


# --------------------------------------------------------------------------
# One bounty, isolated: a failure here never kills the run
# --------------------------------------------------------------------------

def attempt_bounty(api_base, token, bounty, state, dry_run):
    slug, metric = bounty["slug"], bounty.get("metric", "")
    prediction, reason, retryable = build_prediction(slug, metric)
    if prediction is None:
        state["skipped"][slug] = {"reason": reason, "retryable": retryable}
        return ("skipped", reason)
    if dry_run:
        return ("dry-run", prediction)
    http_json("POST", f"{api_base}/api/bounties/{slug}/enter", {}, token)
    status, data = http_json("POST", f"{api_base}/api/bounties/{slug}/submit",
                             {"prediction": prediction}, token)
    if status not in (200, 201):
        # The first attempt may have landed despite the error — check
        # before any retry or failure report.
        if data.get("id") in my_submission_ids(api_base, token):
            state["submitted"][slug] = {"id": data["id"], "prediction": prediction}
            return ("submitted", data["id"])
        raise RuntimeError(f"submit failed ({status}): {data}")
    submission_id = data.get("id")
    if submission_id in my_submission_ids(api_base, token):
        state["submitted"][slug] = {"id": submission_id, "prediction": prediction}
        return ("submitted", submission_id)
    raise RuntimeError("submission not visible in /api/me/submissions")


# --------------------------------------------------------------------------
# The run
# --------------------------------------------------------------------------

def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true",
                        help="do everything except enter/submit")
    parser.add_argument("--max-submissions", type=int, default=10,
                        help="cap on submissions this run (default 10)")
    parser.add_argument("--state", default=DEFAULT_STATE,
                        help="path to the resume state file")
    parser.add_argument("--api-base",
                        default=os.environ.get("API_BASE_URL", DEFAULT_API_BASE))
    parser.add_argument("--handle", default="", help="optional public handle")
    args = parser.parse_args(argv)

    private_key = os.environ.get("AGENT_PRIVATE_KEY", "").strip()
    if not private_key:
        print("error: AGENT_PRIVATE_KEY is not set", file=sys.stderr)
        return 1

    state = load_state(args.state)
    report = {"dry_run": args.dry_run, "submitted": [], "dry_run_ready": [],
              "skipped": {}, "failed": {}, "aborted": None}
    consecutive_429 = 0
    try:
        print("[1/6] reading skill.md ...")
        fetch_skill(args.api_base)

        print("[2/6] authenticating ...")
        address, token = authenticate(args.api_base, private_key)
        report["address"] = address
        print(f"      signed in as {address}")

        print("[3/6] listing open bounties ...")
        bounties = list_open_bounties(args.api_base)
        print(f"      {len(bounties)} open bounties")

        print("[4/6] ranking ...")
        attempt, skipped = rank_bounties(bounties, state)
        for slug, reason in skipped:
            report["skipped"][slug] = reason
        print(f"      {len(attempt)} attemptable, {len(skipped)} skipped")

        print("[5/6] working through bounties ...")
        made = 0
        for bounty in attempt:
            if made >= args.max_submissions:
                print(f"      cap reached ({args.max_submissions}) — stopping")
                break
            slug = bounty["slug"]
            try:
                outcome, detail = attempt_bounty(args.api_base, token, bounty,
                                                 state, args.dry_run)
            except RateLimited as exc:
                consecutive_429 += 1
                report["failed"][slug] = str(exc)
                print(f"      [{slug}] rate-limited ({consecutive_429}/"
                      f"{MAX_CONSECUTIVE_429})")
                if consecutive_429 >= MAX_CONSECUTIVE_429:
                    report["aborted"] = "too many consecutive rate limits"
                    print("      aborting run: rate limit is a stop sign")
                    break
                continue
            except Exception as exc:  # noqa: BLE001 - isolate, keep going
                report["failed"][slug] = str(exc)
                print(f"      [{slug}] FAILED: {exc}")
                continue
            consecutive_429 = 0
            save_state(args.state, state)
            if outcome == "submitted":
                report["submitted"].append({"slug": slug, "id": detail})
                made += 1
                print(f"      [{slug}] submitted: {detail}")
            elif outcome == "dry-run":
                report["dry_run_ready"].append({"slug": slug, "prediction": detail})
                made += 1
                print(f"      [{slug}] dry-run ready: {detail}")
            else:
                report["skipped"][slug] = detail
                print(f"      [{slug}] skipped: {detail}")

        print("[6/6] saving state + report ...")
        save_state(args.state, state)
    except RateLimited as exc:
        report["aborted"] = str(exc)
        print(json.dumps(report, indent=2))
        return 2
    except Exception as exc:  # noqa: BLE001 - report, don't crash silently
        report["aborted"] = str(exc)
        print(json.dumps(report, indent=2))
        return 2

    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
