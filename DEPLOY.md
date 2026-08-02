# Deploying the web reader

One shared feed, always on, reachable by you and whoever you add. Read state is
global — there is no user concept in the schema, so marking an item read marks
it read for everyone. That is deliberate; see "Shared state" below.

## What has to happen in your name

Three steps need your credit card, your Claude subscription, or a browser login.
Nobody can do them on your behalf:

1. **Provision the VPS.** ~€4/mo buys 2 vCPU / 4GB at Hetzner (CAX11, ARM) or
   equivalent at Netcup/Vultr. 2GB is the floor: Claude Code is a Node process
   and textual-serve spawns one Python process per visitor. Verify current
   pricing — it moves.
2. **Mint the Claude token.** Run `claude setup-token` **on your Mac**, where
   you are already authenticated. It returns a long-lived token backed by your
   subscription rather than per-token API billing. Everything the README says
   about the LLM layer's cost model stays true.
3. **Set up the access gate.** Requires a Cloudflare account (and a domain), or
   a Tailscale account. Both are dashboard/browser flows.

## The port itself

The app is portable already — the only macOS-specific pieces are the hardcoded
`/opt/homebrew/bin/claude` default in `agg/enrich.py` (overridden by
`NEWS_CLAUDE_BIN`) and the launchd plist (replaced by `systemd/`).

On the box, as a non-root user:

```bash
sudo apt update && sudo apt install -y python3.13-venv git
git clone <your remote> ~/news-aggregator && cd ~/news-aggregator
python3.13 -m venv .venv && .venv/bin/pip install -r requirements.txt

curl -fsSL https://claude.ai/install.sh | bash    # native build, no node needed
which claude                                      # note this path

.venv/bin/python tests/test_logic.py              # 35 passed, 0 failed
```

Copy `data/items.db` over from the Mac (372K) so you keep read state and run
history, then install the units:

```bash
cd ~/news-aggregator
for f in systemd/*; do
  sed -e "s|__DIR__|$PWD|g" \
      -e "s|__USER__|$USER|g" \
      -e "s|__CLAUDE_BIN__|$(which claude)|g" \
      "$f" | sudo tee "/etc/systemd/system/$(basename "$f")" >/dev/null
done

sudo systemctl daemon-reload
sudo systemctl enable --now news-web.service news-refresh.timer
systemctl status news-web.service
systemctl list-timers news-refresh.timer
```

Then confirm the LLM path works under systemd's environment — this is the step
that catches a wrong `NEWS_CLAUDE_BIN` or an unset `HOME`, and it is worth doing
before you trust the 07:00 timer:

```bash
sudo systemctl start news-refresh.service
journalctl -u news-refresh.service -f
```

## Expect some sources to degrade

`fetch.py` never raises and collects errors, so a blocked source shows up as a
smaller run rather than a crash. But datacenter IPs are treated worse than a
home connection: Reddit rate-limits and often blocks VPS ranges outright, and
BleepingComputer already 403s anything that does not look like a browser. RSS,
Bluesky, CISA KEV, arXiv and the GitHub scrape should be unaffected. Run
`./news stats` after the first refresh and compare per-source counts against the
Mac before assuming the port is clean.

## The access gate

The reader has no authentication and `r` triggers an LLM run on your quota, so
the gate is load-bearing — an open URL gets crawled, and strangers can spend
your subscription. `news-web.service` binds to `127.0.0.1` so the only reachable
path is through whichever gate you install.

**Cloudflare Tunnel + Access** — share a URL, each person logs in with their own
email. Needs a domain on Cloudflare (~$10/yr). A tunnel *alone* is public; the
Access policy is what restricts it. Add both emails to one Allow rule.

**Tailscale** — free, no domain, nothing public exists at all. Each person
installs the Tailscale client and you add them to your tailnet.

## Shared state

`store.connect()` opens SQLite in WAL mode with a 30s busy timeout, because the
web reader is multi-process: one NewsApp per visitor, all writing to the same
file. Under the default rollback journal, marking an item read while a refresh
holds write locks fails with "database is locked".

Read and saved state are global. If you later want them per-person, the identity
is available — Cloudflare Access passes `Cf-Access-Authenticated-User-Email` on
every request. That is a schema change, and it is much cheaper to do before
there is history to migrate.
