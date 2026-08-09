# Security guide

Codex Router Lite is a local Node service. It has one router process, forwards
Responses API requests directly to the selected upstream, and does not include
LiteLLM, a forwarder, Python, OAuth, or a tray application.

## Credentials

- The only routed providers are `mimo-token-plan` and `wlb-relay`. Set their
  keys with `provider-key mimo-token-plan set` and
  `provider-key wlb-relay set`.
- By default, protected key files are
  `~/.codex/codex-router/mimo-api-key.secret` and
  `~/.codex/codex-router/wlb-api-key.secret`. `CODEX_ROUTER_STATE_DIR` or
  `MODEL_ROUTER_STATE_DIR` may select another state directory.
- Files are restricted to the current user (`600` on POSIX and a current-user
  ACL on Windows). Environment variables are supported for a foreground run;
  use the protected files for a background service.
- Keys are read only to create the upstream `Authorization` header. They are
  not written to the catalog, Codex config, or logs. Never commit or paste a
  key, a full generated caller URL, or a live state-directory file.
- Routed requests do not forward Codex's incoming account/authentication
  headers. Native GPT requests use the allow-listed Codex headers needed by
  the native backend.

The state directory also contains `caller-secret`. It authenticates Codex's
loopback URL and is separate from provider keys; treat it as sensitive.
Processes running as the same operating-system user can generally read the
user's Codex config and state, so this is not a same-user malware boundary.

## Network and proxy boundary

The router binds to loopback by default (port `4102` unless configured). Do not
change it to a public listener, tunnel it, or expose it on a shared network.
The caller capability is checked before a model request is read or forwarded.

The configured provider URLs use HTTPS and the router calls each provider's
`/responses` endpoint directly. Keep normal TLS verification enabled and only
trust a proxy you operate. `src/start.mjs` sets `NODE_USE_ENV_PROXY=1` and
passes `HTTP_PROXY`/`HTTPS_PROXY`; the default proxy is
`http://127.0.0.1:7897`, with loopback in `NO_PROXY`. A proxy can observe or
modify traffic if it terminates TLS, so review proxy configuration and logs.

Request logging is off by default. `CODEX_ROUTER_REQUEST_LOG=1` records request
method, URL, status, and duration for troubleshooting; redact the caller
capability and any private paths before sharing logs. Router errors do not log
request or response bodies or provider key values, but logs can still contain
model names and paths.

## Supported runtime and updates

Use Node.js `>=22.19.0` as declared in `package.json` and keep the lockfile in
sync. After changing a routed model, run `node src/catalog.mjs`, reload the
router process, and restart Codex if its picker has not reloaded. Run the
checked-in metadata and syntax checks before distributing a change.

## Reporting a vulnerability

Use [GitHub Private Vulnerability Reporting](https://github.com/duolahypercho/codex-router/security/advisories/new).
Include the router revision, operating system, Node.js version, and Codex CLI
version, plus a minimal reproduction and redacted logs. Do not include API
keys, caller URLs, credential files, prompts, response bodies, or unredacted
state-directory contents in an issue.
