# Codex Router Lite — agent instructions

This is a stripped personal fork. `main` is the untouched upstream snapshot;
all work happens on `lite`.

## Scope

Only two third-party providers exist, both native Responses API, both reached
by direct forwarding from `src/router.mjs` (no LiteLLM, no forwarders, no
Python):

- `mimo-token-plan` — `config/mimo/` — `https://token-plan-cn.xiaomimimo.com/v1`
- `wlb-relay` — `config/wlb/` — `https://codex.wlbclub.com`

Do not reintroduce gateway/forwarder layers, provider presets, OAuth flows,
the tray app, or the self-updater.

## Layout

- `src/router.mjs` — the entire request path (native passthrough + direct
  routed forwarding with per-provider credential injection)
- `src/catalog.mjs` — builds `merged-models.json` from the native capture and
  `config/`; per-field fallback to the native template
- `src/model-registry.mjs` — loads and validates `config/**`; listed-model
  fields are optional-but-validated
- `src/start.mjs` — supervises the single router child and injects the proxy
  environment (`NODE_USE_ENV_PROXY=1`, `HTTPS_PROXY` default
  `http://127.0.0.1:7897`)
- `codex-router.ps1` / `install.ps1` — Windows entry points; `bin/` — POSIX

## Rules

- Run `node --check` on every touched `src/*.mjs`; the whole tree must stay
  import-clean (`grep -h 'from "./' src/*.mjs` vs existing files).
- After changing `config/` or catalog code: `node src/catalog.mjs` and confirm
  the three routed slugs still appear.
- Registry edits must keep slug namespacing (`<provider>/<model>`) and pass
  the validator in `src/model-registry.mjs`.
- Never log or commit credentials. Keys live only in
  `~/.codex/codex-router/*.secret` via `provider-key set` or
  `writeProviderCredential`.
- Node's fetch ignores the Windows system proxy — anything spawning fetches
  outside `start.mjs`'s environment must carry the proxy variables too.
