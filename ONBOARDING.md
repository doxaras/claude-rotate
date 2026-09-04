# Team onboarding

How a team member gets Claude Code access through claude-rotate.

## For team members

Send the admin two things:

1. **your username** (short, lowercase — e.g. `maria`)
2. **a device name** for the machine you'll use (e.g. `laptop`, `desktop`,
   `gpu-box` — one request per machine)

You'll get back two `export` lines. Put them in your shell profile
(`~/.zshrc` / `~/.bashrc`), open a new terminal, run `claude` — no Anthropic
login needed. Your usage dashboard link comes with the reply.

Rules of the road:

- The key is **personal to that device**. Don't share it, don't commit it,
  don't reuse it on a second machine — ask for another key instead.
- If a machine is lost or the key leaks, tell the admin — revocation is
  instant and only affects that device.

## For the admin

On the server:

```bash
cd /opt/claude-rotate            # or wherever it lives
./setup.sh onboard maria laptop
```

This mints the key `maria-laptop`, restarts the service so it's live, and
prints a paste-ready welcome block (it uses `public_url` from `config.json`
for the exact env values). Paste the block back to the requester over a
reasonably private channel — the key is a bearer credential.

**Revoke**: delete the device's entry from `config.json` `devices{}` and
`systemctl restart claude-rotate`. The panel's device roster shows every key
(suffix only) with last-seen times, so stale devices are easy to spot.

**Naming convention** `<username>-<device>` is what makes the analytics
readable: the panel's per-device tables become per-person tables for free.
