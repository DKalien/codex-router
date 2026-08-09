# Codex Router Lite

Fork of [duolahypercho/codex-router](https://github.com/duolahypercho/codex-router),
stripped to one job: put **native GPT + two third-party providers** into Codex's
native model picker and route their traffic locally.

- Native GPT (ChatGPT login) — proxied straight to `chatgpt.com`
- **MiMo** (`mimo-v2.5-pro`, `mimo-v2.5`) — Xiaomi Token Plan endpoint
- **WLB relay** (`gpt-5.6-sol`) — `codex.wlbclub.com` relay

## What was removed from upstream

Everything not needed for the two providers above: LiteLLM gateway and its
Python venv, OAuth forwarders, 20+ provider presets, tray/desktop apps,
self-updater, doctor/setup/multi-agent/vision tooling, tests and CI.
No Python dependency remains; the whole service is a single Node process.

## Architecture

Upstream: `Codex → router → LiteLLM → api-forwarder → provider`.
Lite: both providers speak the Responses API natively, so the chain is just

```
Codex ──(config.toml: openai_base_url + model_catalog_json)──▶ router.mjs
    ├─ native slug        → chatgpt.com (Codex auth passthrough, via system proxy)
    ├─ mimo-token-plan/*  → token-plan-cn.xiaomimimo.com (MIMO_API_KEY injected)
    └─ wlb-relay/*        → codex.wlbclub.com (WLB_API_KEY injected)
```

- **Catalog injection**: `src/catalog.mjs` captures Codex's native catalog
  (`codex debug models`), merges the routed entries from `config/`, and writes
  `~/.codex/codex-router/merged-models.json`.
- **Metadata inheritance**: listed registry fields are optional; anything
  absent falls back to the native gpt-5.5 template (`wlb-relay/gpt-5.6-sol`
  inherits everything except its display name). MiMo entries carry Xiaomi's
  officially recommended metadata verbatim.
- **Proxy**: upstream fetches honor `HTTPS_PROXY` (default
  `http://127.0.0.1:7897`, the Clash mixed port) via Node 24's
  `NODE_USE_ENV_PROXY`. Clash's GEOIP rules keep domestic endpoints on DIRECT.

## Install / daily use (Windows)

```powershell
.\codex-router.ps1 install                    # deps + secrets + catalog + service
.\codex-router.ps1 provider-key mimo-token-plan set
.\codex-router.ps1 provider-key wlb-relay set
.\codex-router.ps1 enable | disable | uninstall | start
```

POSIX: `bin/install`, `bin/provider-key`, `bin/enable`, `bin/disable`,
`bin/uninstall`, `bin/start`.

State lives in `~/.codex/codex-router/` (catalog, secrets, logs).
The service auto-starts at logon via a "Codex Router" scheduled task.

## Maintenance

- Official model updates: `node src/catalog.mjs` (auto re-captures when the
  Codex CLI version changes), then fully restart Codex.
- Adding/changing a routed model: edit `config/<provider>/models.json`, run
  `node src/catalog.mjs`, restart Codex.
- Request-level debugging: start with `CODEX_ROUTER_REQUEST_LOG=1`.

## Branches

- `main` — untouched upstream snapshot
- `lite` — this stripped version
