# Codex Router Lite

Fork of [duolahypercho/codex-router](https://github.com/duolahypercho/codex-router),
stripped to one job: put native GPT and two Responses API providers into
Codex's model picker and route their traffic locally.

The merged catalog contains exactly eight entries:

- Native GPT-5.6 (ChatGPT login) — `gpt-5.6-sol`, `gpt-5.6-terra`,
  `gpt-5.6-luna`
- **MiMo** — `mimo-token-plan/mimo-v2.5-pro`,
  `mimo-token-plan/mimo-v2.5`
- **WLB relay** — `wlb-relay/gpt-5.6-sol`, `wlb-relay/gpt-5.6-terra`,
  `wlb-relay/gpt-5.6-luna`

Five entries are routed (the two MiMo entries and three WLB entries); the
three native entries continue to use Codex's ChatGPT backend.

## What was removed from upstream

Everything not needed for the two providers above: LiteLLM and other gateway or
forwarder layers, Python, OAuth flows, provider presets, tray/desktop apps, and
the self-updater. The service is Node-only: `src/start.mjs` supervises one
`src/router.mjs` child, with no extra gateway or forwarder processes. The
checked-in checks are `test/catalog-metadata.mjs` and `scripts-check.mjs`.

## Architecture

Both providers speak the Responses API natively, so the chain is just

```
Codex ──(config.toml: openai_base_url + model_catalog_json)──▶ router.mjs
    ├─ native slug        → ChatGPT Codex backend (Codex auth passthrough)
    ├─ mimo-token-plan/*  → MiMo Token Plan (MIMO_API_KEY injected)
    └─ wlb-relay/*        → WLB Relay (WLB_API_KEY injected)
```

- **Catalog injection**: `src/catalog.mjs` captures Codex's native catalog,
  emits only the three official GPT-5.6 native slugs, merges `config/`, and
  writes `~/.codex/codex-router/merged-models.json`. The full native capture is
  retained for exact WLB template lookup.
- **Metadata**: each WLB entry clones the exact native entry named by its
  `upstreamModel`, changing only the namespaced slug and WLB display name; a
  missing native entry fails the build. MiMo entries use the explicit Xiaomi
  field set in `src/catalog.mjs` and never inherit GPT metadata.
- **Proxy**: upstream fetches honor `HTTPS_PROXY` (default
  `http://127.0.0.1:7897`, the Clash mixed port) via
  `NODE_USE_ENV_PROXY`. Clash's GEOIP rules keep domestic endpoints on DIRECT.

## Install / daily use (Windows)

```powershell
.\codex-router.ps1 install                    # deps + secrets + catalog + service
.\codex-router.ps1 provider-key mimo-token-plan set
.\codex-router.ps1 provider-key wlb-relay set
.\codex-router.ps1 enable
.\codex-router.ps1 start
.\codex-router.ps1 disable
.\codex-router.ps1 uninstall
```

POSIX: `bin/install`, `bin/provider-key`, `bin/enable`, `bin/disable`,
`bin/uninstall`, `bin/start`.

State lives in `~/.codex/codex-router/` (catalog, secrets, logs).
The service auto-starts at logon via a "Codex Router" scheduled task.

## Maintenance

- Official model updates: `node src/catalog.mjs` (auto re-captures when the
  Codex CLI version changes), reload the router process, then restart Codex if
  its picker still has the old catalog.
- Adding/changing a routed model: edit `config/<provider>/models.json`, run
  `node src/catalog.mjs`, reload the router (`node src/service.mjs restart`),
  and restart Codex when the picker needs to reload.
- Checks: `node test/catalog-metadata.mjs` and `node scripts-check.mjs`.
- Request-level debugging: start with `CODEX_ROUTER_REQUEST_LOG=1`.

## Branches

- `main` — untouched upstream snapshot
- `lite` — this stripped version
