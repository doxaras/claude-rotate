# claude-rotate

A tiny self-hosted proxy that lets **Claude Code** ride multiple Claude
Max/Pro subscriptions and switches accounts automatically when one hits its
rate-limit window — with a built-in analytics panel showing quota gauges,
per-device consumption, and cost-equivalent dollars.

One process, one config file, no database. Built for individual developers who
own more than one subscription and are tired of "You've hit your session
limit".

> **Terms-of-Service note.** This tool automates switching between accounts
> *you personally own and pay for*. Each account's limits are still fully
> enforced by Anthropic. Rotating accounts to work past limits is a gray area
> under Anthropic's consumer terms — use at your own risk, and do **not** use
> consumer subscriptions to back a shared/commercial service.

## How it works

```
 laptop ──┐   Claude Code sends            claude-rotate (this proxy)          Anthropic
 desktop ─┼─  Authorization: Bearer   ──▶  · authenticates the device    ──▶  api.anthropic.com
 ci box ──┘   <device key>                 · swaps in the active account's
                                             long-lived setup-token
                                           · reads anthropic-ratelimit-unified-*
                                             response headers (exact 5h/7d
                                             utilization per account)
                                           · consume-first rotation: burn the
                                             soonest-resetting weekly quota
                                             first; quota 429 → rotate, burst
                                             429 → pace; all spent → hold
                                           · logs usage per device/model
```

Key insight: Claude Code respects `ANTHROPIC_BASE_URL`, and `claude
setup-token` mints a ~1-year OAuth token per account. The proxy holds those
tokens; devices only ever hold an internal *device key*. Every response from
Anthropic carries exact quota telemetry headers, so switching at 80% is
measured, not guessed.

## Quickstart (server)

```bash
git clone <this repo> && cd claude-rotate
pip install -r requirements.txt
./setup.sh                      # creates config.json, tokens/, logs/
./setup.sh add-account acct-1   # paste token from: claude setup-token  (account 1's browser)
./setup.sh add-account acct-2   # …account 2
./setup.sh add-device my-laptop # prints the env vars for that device
python3 rotator.py
```

## Quickstart (each device)

Two env vars in your shell profile — that's the whole client install:

```bash
export ANTHROPIC_BASE_URL=http://<server>:8484
export CLAUDE_CODE_OAUTH_TOKEN=<device key printed by add-device>
```

Run `claude` as usual. When account 1's 5-hour window fills, the next request
rides account 2. Use a VPN/overlay like Tailscale between devices and server —
the proxy speaks plain HTTP and device keys are bearer secrets.

## Analytics panel

Open `http://<server>:8484/rotate/panel?key=<any device key>`:

- **Devices** — every registered device: online status, last seen, last model,
  which account its traffic rode, requests + tokens in the last hour.
- **Accounts** — live 5-hour and weekly utilization gauges (from Anthropic's
  own headers), active account, reset countdowns.
- **Alerts** — device over N tokens/hour, expensive-model usage (Opus/Fable),
  account near the switch threshold. Thresholds in `config.json`.
- **Consumption (24h)** — by device / model / account, with a *cost-equivalent*
  column: what the usage would cost at API list prices (incl. cache read 0.1× /
  write 1.25×) — i.e. what the subscriptions are saving you.

JSON endpoints: `/rotate/status` (accounts + switch events) and
`/rotate/stats` (rollups + alerts), same `?key=` or `Authorization: Bearer`
auth. Audit trail: `logs/audit.jsonl`, one JSON record per request.

## Configuration (`config.json`)

| Field | Meaning |
|---|---|
| `accounts[]` | `{name, token_file}` — one `claude setup-token` per subscription, stored under `tokens/` (chmod 600) |
| `devices{}` | `name → device key`; key is what the device puts in `CLAUDE_CODE_OAUTH_TOKEN` |
| `switch_threshold` | 5h-window utilization that triggers rotation (default 0.8) |
| `switch_threshold_7d` | weekly-window utilization that makes an account unusable (default 0.98) |
| `strategy` | `consume-first` (default) or `least-used` — see below |
| `switch_cooldown_s` | min seconds between voluntary switches (default 300); hard limits ignore it |
| `switch_margin` | hysteresis: a threshold switch needs a candidate this much better (default 0.05) |
| `consume_first_margin_s` | proactive switch only if the candidate's weekly reset is this much sooner (default 3600) |
| `hold_max_s` | when *every* account is spent, hold the request open up to this long waiting for a window reset instead of returning 429 (default 0 = off) |
| `prices_per_mtok` | substring-matched `[input, output]` $ per MTok for the cost columns |
| `alerts` | `device_tokens_per_hour`, `expensive_model_patterns`, `util_warn` |

### Rotation policy

An account is *usable* while its 5h window is under `switch_threshold` and its
weekly window under `switch_threshold_7d`; a window past its reset counts as
0%. State survives restarts in `state.json`.

- **`consume-first`** (default): burn the usable account whose **weekly window
  resets soonest** — weekly quota is use-it-or-lose-it, so the perishable
  account is spent first and no paid quota expires unused. The proxy also
  switches *proactively* (below the threshold) when another usable account's
  weekly reset is at least `consume_first_margin_s` sooner.
- **`least-used`**: classic — lowest 5h utilization wins.
- A cooldown plus a hysteresis margin stop accounts ping-ponging at the
  threshold; a hard limit (quota actually rejected) always switches
  immediately.
- **Burst vs quota 429s**: a per-minute rate-limit 429 (utilization not
  exhausted) does *not* rotate — rotating would move the burst to the next
  account and throw away its warm prompt cache. The account is paced for
  `retry-after` seconds and the request retried.
- **Hold-until-reset** (`hold_max_s` > 0): when every account is spent, the
  proxy keeps the request open and retries after the soonest 5h reset instead
  of failing — an unattended CI/agent run finishes on its own instead of dying
  at 2am. Make sure your client's request timeout tolerates the wait (Claude
  Code's default is generous; other clients may need tuning).

## Run as a service

- **macOS**: `deploy/com.example.claude-rotate.plist` (read its comments —
  launchd needs the full python3 path and a local-disk install).
- **Linux**: `deploy/claude-rotate.service` (systemd).

## Security notes

- `tokens/*.token` are year-long bearer credentials for your Anthropic
  accounts. They stay on the server, mode 600, and are gitignored along with
  `config.json`, `state.json`, and `logs/`.
- Device keys authenticate devices to the proxy only; revoke one by deleting
  its entry in `config.json` and restarting.
- Don't expose port 8484 to the public internet; bind to a tailnet/LAN
  interface or keep `bind: 0.0.0.0` behind a firewall.

---

## For the next agent (continuation notes)

Everything lives in **`rotator.py` (~450 lines, FastAPI)** — read it top to
bottom before changing anything. `phase0_proxy.py` is a standalone logging
passthrough kept for debugging header behavior; not part of the service.

Load-bearing implementation facts (each was verified empirically — keep them):

1. **Transparent passthrough.** The proxy forwards all paths/methods to
   `api.anthropic.com`, replacing only the `Authorization` header. Claude Code
   sends its own `anthropic-beta: oauth-2025-04-20` etc. — do not strip or
   reorder client headers.
2. **Encoding trick.** Forwarded requests force `accept-encoding: identity`
   and the response's `content-encoding` header is dropped — otherwise httpx
   auto-decompresses while the original header survives and the client throws
   Zlib/Brotli errors.
3. **Quota telemetry** comes from `anthropic-ratelimit-unified-5h-utilization`
   / `-7d-utilization` / `-5h-reset` / `-status` response headers (present on
   Max subscription traffic; undocumented — re-verify after Anthropic API
   changes).
4. **SSE usage capture** parses `data:` lines containing `"usage"` while
   streaming chunks through untouched; audit is written in the stream's
   `finally`.
5. **Raw `/v1/messages` calls work** with a setup-token if you add
   `anthropic-beta: oauth-2025-04-20` — verified; this is what makes the
   roadmap's OpenAI-compat endpoint possible without Claude Code in the loop.

Roadmap, in intended order:

- [x] **Switch-logic tests.** `tests/test_rotator.py` — 20 offline checks for
      `pick_account` / `note_response` (consume-first ordering, burst-vs-quota
      429, cooldown, hysteresis, hold/exhausted verdicts, window-reset
      recovery). Run with `python3 tests/test_rotator.py` (needs a valid
      `config.json` to import the module). `aggregate_audit` still untested.
- [ ] **OpenAI-compatible endpoint.** `POST /v1/chat/completions` translating
      OpenAI ↔ Anthropic `/v1/messages` (incl. SSE chunk translation), so any
      OpenAI-format client or router can use the rotated capacity — not just
      Claude Code. Inject `anthropic-version: 2023-06-01` +
      `anthropic-beta: oauth-2025-04-20` on translated requests.
- [ ] **Exhaustion signal.** When *all* accounts are saturated **and holding
      is off or exhausted**, return 503 + `Retry-After: <earliest reset>`
      instead of passing through Anthropic's 429, so upstream routers/breakers
      can degrade gracefully. (Partially superseded: `hold_max_s` now holds
      the request open until the soonest reset; the 503 shape is still open.)
- [ ] **Webhook alerts** (Slack/Teams/generic POST) firing on the same rules
      as the panel's alerts section.
- [ ] **Dockerfile** (+ compose example) — also enables running as a sidecar
      next to a router in k8s; tokens mounted as secrets.
- [ ] **Hot config reload** (`SIGHUP` or mtime check) so `add-account` /
      `add-device` don't need a restart.
- [ ] **Token renewal automation** — setup-tokens last ~1 year; at minimum
      alert on auth failures (401 from upstream on a known-good account
      usually means the token died).

Style: keep it one file until it genuinely hurts; stdlib + fastapi/httpx only;
every new claim about Anthropic behavior gets verified against the live API
before being relied on (the `phase0_proxy.py` harness exists for exactly
that).
