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
  `config/`; emits only official GPT-5.6 native slugs, WLB clones exact native
  upstream entries, while MiMo emits its explicit Xiaomi metadata
- `src/model-registry.mjs` — loads and validates `config/**`; listed-model
  fields are optional-but-validated
- `src/start.mjs` — supervises the single router child and injects the proxy
  environment (`NODE_USE_ENV_PROXY=1`, `HTTPS_PROXY` default
  `http://127.0.0.1:7897`)
- `codex-router.ps1` / `install.ps1` — Windows entry points; `bin/` — POSIX

## Rules

- The merged catalog must contain exactly 8 models: native
  `gpt-5.6-sol/terra/luna`, MiMo `mimo-v2.5-pro` and `mimo-v2.5`, and WLB
  `gpt-5.6-sol/terra/luna`. Exactly 5 are routed under the two provider
  namespaces.
- Run `node --check` on every touched `src/*.mjs`; the whole tree must stay
  import-clean (`grep -h 'from "./' src/*.mjs` vs existing files).
- After changing `config/` or catalog code: run `node src/catalog.mjs` and
  confirm all 8 catalog slugs and all 5 routed slugs still appear; reload the
  router process and restart Codex if its picker has not reloaded.
- Run `node test/catalog-metadata.mjs` and `node scripts-check.mjs` for catalog
  or source changes.
- Registry edits must keep slug namespacing (`<provider>/<model>`) and pass
  the validator in `src/model-registry.mjs`.
- Never log or commit credentials. Persistent provider keys belong in
  `~/.codex/codex-router/*.secret` via `provider-key set` or
  `writeProviderCredential`; transient environment keys must not be recorded.
- Node's fetch ignores the Windows system proxy — anything spawning fetches
  outside `start.mjs`'s environment must carry the proxy variables too.
