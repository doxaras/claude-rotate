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

## Generating account tokens (`claude setup-token`)

Each subscription account contributes one long-lived OAuth token. Facts below
are from the [official authentication docs](https://code.claude.com/docs/en/authentication.md)
unless flagged otherwise.

**Minting a token:**

```bash
claude setup-token
```

- Opens the same browser OAuth flow as `/login`; if no browser can reach the
  local callback (headless server, SSH session), it falls back to a paste-a-code
  flow — so you can mint tokens on the proxy server itself.
- Prints a token starting `sk-ant-oat01-…` to the terminal and does **not**
  save it anywhere — copy it straight into `./setup.sh add-account <name>`,
  which stores it under `tokens/` (chmod 600, gitignored).
- Valid for **about one year**. Requires a Claude subscription (Pro, Max,
  Team, or Enterprise).

**Minting for multiple accounts from one machine.** The account that completes
the browser OAuth is the account the token belongs to — the CLI's current
login doesn't decide it. (Officially undocumented; this is the behavior in
practice.) Two workable recipes:

1. **Browser profiles**: keep one browser profile (or incognito window) logged
   into claude.ai per account. Run `claude setup-token`, and complete the OAuth
   in the profile of the account you're minting for — copy the URL into that
   profile if the wrong one opens.
2. **Config-dir isolation**: `CLAUDE_CONFIG_DIR=~/.claude-acct2 claude
   setup-token` keeps a fully separate CLI login per directory, useful if you
   also want to *run* Claude Code as different accounts.

After adding an account, send one request through the proxy and check
`/rotate/status` — the unified utilization headers it reports are per-account,
so a token minted against the wrong account shows up immediately as the wrong
gauge moving.

**Lifetime & revocation:**

- There is currently **no CLI command to list or revoke** setup tokens
  ([open feature request](https://github.com/anthropics/claude-code/issues/57400));
  revoke manually from claude.ai settings. `/logout` only revokes the CLI's
  *active* session credential, not previously minted setup tokens.
- Treat each token file as a year-long bearer credential for that Anthropic
  account (see [Security notes](#security-notes)). When a token dies early, the
  proxy's upstream calls start returning 401 for that account — the roadmap has
  an alert for this.
- Scope note: subscription OAuth tokens are meant for Claude Code traffic, and
  per the 2026 docs they are rejected by the raw Messages API. This proxy
  forwards Claude Code's own requests, which is exactly the supported shape
  (the `anthropic-beta: oauth-2025-04-20` behavior for raw calls is documented
  in the continuation notes and should be re-verified before relying on it).

## Quickstart (each device)

Two env vars in your shell profile — that's the whole client install:

```bash
export ANTHROPIC_BASE_URL=http://<server>:8484
export CLAUDE_CODE_OAUTH_TOKEN=<device key printed by add-device>
```

Run `claude` as usual. When account 1's 5-hour window fills, the next request
rides account 2. Use a VPN/overlay like Tailscale between devices and server —
the proxy speaks plain HTTP and device keys are bearer secrets. See
[Connecting distributed devices](#connecting-distributed-devices) for setups.

## Connecting distributed devices

The proxy is plain HTTP and device keys are bearer secrets, so the transport
between devices and server must be private. Pick one:

### Tailscale (recommended)

Zero-config WireGuard mesh; free tier covers personal use easily.

```bash
# on the server AND every device:
#   macOS:  brew install tailscale && brew services start tailscale
#   Linux:  curl -fsSL https://tailscale.com/install.sh | sh
tailscale up                      # login once per machine, same tailnet
tailscale status                  # note the server's name / 100.x.y.z address
```

With MagicDNS on (default on new tailnets), devices reach the server by name:

```bash
export ANTHROPIC_BASE_URL=http://<server-hostname>:8484   # e.g. http://ais-mac-mini:8484
export CLAUDE_CODE_OAUTH_TOKEN=<device key>
```

Tighten the listener so the proxy is *only* reachable over the tailnet — set
`bind` in `config.json` to the server's Tailscale IP instead of `0.0.0.0`:

```json
{ "bind": "100.x.y.z", "port": 8484 }
```

Optional TLS: `tailscale serve --bg http://127.0.0.1:8484` publishes the proxy
as `https://<server>.<tailnet>.ts.net` (valid cert, tailnet-only). Then use
that URL as `ANTHROPIC_BASE_URL` and set `bind` to `127.0.0.1`.

CI runners and containers work too: ephemeral auth keys
(`tailscale up --auth-key=tskey-...`) join a runner to the tailnet for the
duration of a job; there's a ready-made GitHub Action (`tailscale/github-action`).

### Plain WireGuard

Same effect, no third party. Sketch: generate a keypair per machine
(`wg genkey | tee private.key | wg pubkey > public.key`), give the server a
`wg0` with an internal subnet (e.g. `10.84.0.1/24`), add each device as a
`[Peer]` with `AllowedIPs = 10.84.0.X/32`, and point devices at
`http://10.84.0.1:8484`. Set `bind` to `10.84.0.1`. More manual than
Tailscale (key distribution, NAT traversal is on you), but fully self-hosted.

### SSH tunnel (zero install)

Any device that can SSH to the server needs nothing else:

```bash
ssh -N -L 8484:127.0.0.1:8484 user@server &
export ANTHROPIC_BASE_URL=http://127.0.0.1:8484
```

With `bind: 127.0.0.1` on the server, this is the tightest setup — the proxy
never listens on a network interface at all. Use `autossh` (or
`ServerAliveInterval 30` in `~/.ssh/config`) to keep the tunnel up; fine for a
laptop or a single CI box, tedious beyond a few devices.

### Cloudflare Tunnel (device without VPN access)

For a device that can't join your tailnet (locked-down corp machine, hosted
CI you can't install agents on), `cloudflared` can expose the proxy through
Cloudflare without opening ports:

```bash
# server:
cloudflared tunnel login
cloudflared tunnel create claude-rotate
cloudflared tunnel route dns claude-rotate rotate.example.com
cloudflared tunnel run --url http://127.0.0.1:8484 claude-rotate
# device:
export ANTHROPIC_BASE_URL=https://rotate.example.com
```

**This makes the proxy internet-reachable** — the only thing between the
world and your Anthropic tokens is the device-key check. If you go this
route, put [Cloudflare Access](https://developers.cloudflare.com/cloudflare-one/)
(a Zero Trust service-token policy) in front so unauthenticated requests never
reach the proxy, and treat device keys as revocable: delete a leaked one from
`config.json` and restart. Prefer any of the VPN options above when possible.

### Other overlays

ZeroTier and NetBird work identically to Tailscale for this purpose (private
overlay IP + `bind` to it); use whichever your fleet already runs. Whatever
the transport, the checklist is the same: proxy bound to a private interface,
HTTP never exposed publicly, one device key per machine so any single machine
can be revoked alone.

## Analytics panel

Open `http://<server>:8484/rotate/panel?key=<any device key>`:

![claude-rotate analytics panel — devices, account gauges, consumption and cost-equivalent tables](screenshots/panel.png)

Live 5-hour and weekly gauges per account, straight from Anthropic's own
rate-limit headers:

![account quota gauges with reset countdowns](screenshots/accounts.png)

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
| `accounts[].priority` | lower = preferred; accounts form tiers, backup tiers only used when every preferred account is spent (default 100) |
| `accounts[].disabled` | `true` benches an account: never rotated onto, still shown in the panel |
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

### Preferring one account over another

Three levels of control, from lazy to pro — all in the same `config.json`,
deliberately **no separate rules file** (see design note below):

**Level 0 — do nothing.** The defaults (consume-first + thresholds + cooldown)
already make a sane global decision. Most single-owner setups need nothing else.

**Level 1 — priority tiers.** Give accounts a `priority` (lower = preferred):

```json
"accounts": [
  { "name": "max-20x",  "token_file": "tokens/max-20x.token",  "priority": 1 },
  { "name": "max-5x",   "token_file": "tokens/max-5x.token",   "priority": 1 },
  { "name": "old-pro",  "token_file": "tokens/old-pro.token",  "priority": 2 }
]
```

Rotation happens *within* the lowest-numbered tier that still has a usable
account; `old-pro` above is touched only when both Max accounts are spent.
When a preferred account's window resets, traffic is pulled back automatically
(`priority_recovery` in the events feed) after the cooldown — the backup is a
spillway, not a new home. Accounts without a `priority` share one default tier,
which is why Level 0 works unchanged.

**Level 2 — bench an account.** `"disabled": true` takes an account out of
rotation entirely (a work account you don't want touched, one you're resting)
while keeping it visible in the panel. If the *active* account is disabled in
config, the proxy abandons it on the next response, cooldown or not. Re-enable
by deleting the flag; both changes need a restart (hot reload is on the roadmap).

Combine with `strategy` for the remaining temperament choice: `consume-first`
(spend perishable weekly quota first) or `least-used` (spread evenly).

**Design note — why no rules YAML:** a rules engine (per-model routes,
time-of-day windows, per-device pinning) would add a parser, a second config
file, and an ordering semantics to a one-file tool, and every use case we've
actually hit decomposes into the four knobs above (strategy, priority,
disabled, thresholds). If a real need appears that doesn't decompose — say
per-device account pinning — add it as another plain field on the existing
config objects, not as a DSL.

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

- [x] **Tests.** `python3 tests/run_all.py` — 75 offline checks, no network
      (needs a valid `config.json` to import the module):
      `test_rotator.py` switch logic (consume-first ordering, burst-vs-quota
      429, cooldown, hysteresis, hold/exhausted verdicts, window-reset
      recovery); `test_analytics.py` SSE usage capture, pricing/cost math,
      `aggregate_audit` rollups/alerts/roster; `test_proxy.py` end-to-end HTTP
      through the real ASGI app against a mocked upstream (auth, header
      rewriting incl. the encoding trick, SSE relay, transparent quota-429
      rotate+retry, burst pacing, 429 passthrough vs hold-until-reset, admin
      endpoints).
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
