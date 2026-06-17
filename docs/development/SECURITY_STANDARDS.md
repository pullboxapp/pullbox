# Pullbox Security Standards

**Author:** Adam Hernandez
**Version:** 1.1
**Last Modified:** 2026-05-16

## Purpose

This document is the working security reference for Pullbox contributors. It
captures the security behavior already present in the app, the standards new
code should follow, and the checks that help keep self-hosted deployments safe
without making development miserable.

Security work in Pullbox should be practical and explicit. Prefer clear trust
boundaries, safe defaults, boring cryptography, small attack surfaces, and tests
that lock down important behavior.

## Current Baseline Notes

- Pullbox targets Python 3.14 in the production container while keeping Python
  3.12+ compatibility in the project metadata and test matrix.
- The production Docker image uses Docker Hardened Images through
  `docker/Dockerfile`.
- Browser sessions use signed cookies with per-session CSRF tokens.
- API keys are generated once, shown once, and stored only as SHA-256 hashes.
- Provider and integration credentials are encrypted at rest with Fernet derived
  from `PULLBOX_SECRET_KEY`.
- CSRF protection is enforced for unsafe session-authenticated requests.
- Local auth bypass exists for trusted local deployments, but it is disabled by
  default and gated by explicit settings plus client address checks.
- Static security checks cover workflow pinning, secret scanning, dependency
  scanning, container scanning, Bandit, SQL-string safety, unsafe template sinks,
  and outbound HTTP timeout contracts.
- CSP is documented exactly as implemented. It still allows inline script/style
  and app-side `'unsafe-eval'`, so CSP tightening remains a good future
  hardening target.
- CodeQL is configured for public repositories and can be opt-in while private;
  it is informational in the required aggregate gate until explicitly promoted.

## Table of Contents

1. [Authentication And Sessions](#1-authentication-and-sessions)
2. [CSRF Protection](#2-csrf-protection)
3. [Secret Management](#3-secret-management)
4. [Input Validation And Injection Prevention](#4-input-validation-and-injection-prevention)
5. [HTTP Security](#5-http-security)
6. [Dependency And Supply Chain Security](#6-dependency-and-supply-chain-security)
7. [Rate Limiting And Abuse Controls](#7-rate-limiting-and-abuse-controls)
8. [Logging And Audit Trail](#8-logging-and-audit-trail)
9. [Docker And Deployment Security](#9-docker-and-deployment-security)
10. [Security Audit Checklist](#10-security-audit-checklist)
11. [Primary References](#11-primary-references)

## 1. Authentication And Sessions

### 1.1 Password Storage

**Current Pullbox implementation**

- `src/pullbox/services/auth_service.py` hashes user passwords directly with
  the `bcrypt` package.
- The current bcrypt cost factor is `12`.
- `src/pullbox/core/password_policy.py` enforces:
  - minimum 8 characters
  - maximum 128 characters
  - maximum 72 UTF-8 bytes before bcrypt hashing
  - at least one uppercase letter
  - at least one lowercase letter
  - at least one digit
  - at least one special character

**Required standard**

- Bcrypt may remain the password-hash algorithm for now.
- Cost factor must stay at least `12`.
- Verification should stay below roughly one second on production-class
  hardware.
- Password input must respect bcrypt's 72-byte input limit explicitly.
- Future password-hash migration should prefer Argon2id for new hashes and plan
  rehash-on-login behavior.
- Passwords are never encrypted, reversible, logged, or returned by any API.
- Do not require periodic password rotation without compromise evidence.
- Password-manager behavior matters. Paste, autofill, and long generated
  passwords should remain usable.

**Current repo nuances**

- Password blocklist checks are not a current runtime feature. They remain a
  future hardening option for known compromised passwords, common passwords, and
  context-specific passwords such as username or service name.
- Current character-class composition rules are stricter than modern NIST
  guidance. That is the current product decision, not an accident. If those
  rules are relaxed later, pair the change with blocklist checks.
- Test fixtures may contain fake plaintext passwords for auth-flow tests. Those
  are not production secrets.

**Audit checks**

- [ ] `bcrypt` is the only user password hashing library in use.
- [ ] Cost factor is at least `12` everywhere.
- [ ] No plaintext production passwords exist in the database, logs, or config.
- [ ] Password changes always re-hash with the current cost factor.
- [ ] The bcrypt 72-byte limit is handled explicitly.
- [ ] Password-manager, paste, and autofill behavior remains usable.
- [ ] Any password policy change is covered by tests.

### 1.2 Session Tokens And Cookies

**Current Pullbox implementation**

- UI auth uses a signed `itsdangerous.URLSafeTimedSerializer` token.
- The session token payload contains:
  - `user_id`
  - per-session CSRF token
  - `sv`, the session version
- Session lifetime comes from the database-backed `session_lifetime_hours`
  setting and defaults to `24` hours.
- The session cookie is set with:
  - `HttpOnly=True`
  - `SameSite=Lax`
  - `Secure` when the request is HTTPS or trusted-proxy
    `X-Forwarded-Proto=https`
- Pullbox invalidates sessions by comparing token `sv` against
  `user.session_version`.
- Username and password changes increment `session_version`, log the event, and
  delete the current cookie.

**Required standard**

- Session cookie contents must stay minimal and non-sensitive.
- Do not add roles, permissions, provider credentials, internal paths, or other
  operational detail to the signed payload.
- `Secure`, `HttpOnly`, and `SameSite=Lax` remain mandatory browser-session
  defaults.
- Cookie scope should stay narrow:
  - avoid broad `Domain` attributes unless a deployment explicitly needs them
  - keep `Path` scoped to the application root
  - prefer a `__Host-` cookie prefix if deployment constraints allow it later
- Privilege-sensitive changes must invalidate prior sessions.
- Logout must delete the cookie client-side.
- Invalidated sessions must fail server-side even if a client keeps an old
  cookie.
- A strong application secret is mandatory.

**Current repo nuances**

- Session expiration is absolute. There is no separate idle timeout today.
- Secure-cookie detection honors `X-Forwarded-Proto=https` only when the raw
  client address matches configured trusted proxies.
- Startup validates weak, sample, default, and low-variety
  `PULLBOX_SECRET_KEY` override values outside explicit test opt-in paths.
- When no override is provided, `config.xml` is generated with a strong
  application secret on first run.

**Audit checks**

- [ ] Session cookies are always `HttpOnly`.
- [ ] Session cookies are always `SameSite=Lax`.
- [ ] `Secure` is set when traffic is HTTPS behind direct or trusted-proxy
  deployment.
- [ ] Expired or tampered tokens are rejected.
- [ ] `session_version` mismatch invalidates old sessions.
- [ ] Credential changes invalidate prior sessions.
- [ ] Secret strength and non-default validation is enforced for runtime
      overrides.
- [ ] Forwarded scheme trust is tied to trusted proxy configuration.

### 1.3 API Key Authentication

**Current Pullbox implementation**

- API keys use the prefix `pb_k1_`.
- Raw key material is generated with `secrets.token_hex(32)` and then prefixed.
- Only the SHA-256 hash is stored in the database.
- `expires_at`, `is_active`, and `last_used_at` are enforced or updated during
  authentication.
- API key auth skips CSRF because it is header-based bearer-style auth.
- Malformed API-key headers are rejected before database lookup.
- API-key names are normalized before persistence.

**Required standard**

- Raw API keys are shown once at creation and never persisted.
- Revocation must be immediate through `is_active = False`.
- Expired keys fail closed.
- API keys must be rotatable without password changes.
- API-key list responses must never expose stored hashes.
- Rate limiting remains per client/IP at the middleware layer unless key-scoped
  throttling is added deliberately.

**Current repo nuances**

- API keys are automation credentials, not browser sessions.
- A request with both a session cookie and an API key header still follows the
  browser-session CSRF rules.

**Audit checks**

- [ ] Raw API keys are never stored after creation.
- [ ] API key creation uses a CSPRNG.
- [ ] SHA-256 hash comparison is the only lookup path.
- [ ] Expiry and revocation are enforced on every authenticated request.
- [ ] `last_used_at` updates only on successful use.
- [ ] Malformed API-key headers are rejected before database lookup.
- [ ] API-key create and list responses never expose stored hashes.

### 1.4 Local Auth Bypass

**Current Pullbox implementation**

- `src/pullbox/api/deps.py` supports local auth bypass for trusted local
  deployments.
- `src/pullbox/core/local_auth_bypass.py` handles policy loading, address
  normalization, trusted proxy handling, client IP resolution, and CSRF token
  derivation.
- The feature is disabled unless explicit enablement and address allowlisting
  conditions are met.
- Enabling bypass requires a trusted local address or CIDR.
- If multiple active users exist, bypass must target a specific active username.
- Local bypass writes still require CSRF.

**Required standard**

- Local bypass remains off by default.
- Local bypass is acceptable only for explicitly trusted local or self-hosted
  deployments.
- Local bypass must never become silently enabled through weak defaults.
- Trusted proxy headers are honored only from configured trusted proxy
  addresses.
- Bypass decisions and toggle changes must be auditable.

**Current repo nuances**

- Default local addresses may exist as configuration suggestions, but the bypass
  toggle itself defaults to disabled.
- This feature gives full operator access when enabled. Treat it like a sharp
  tool, not a convenience login mode.

**Audit checks**

- [ ] Local bypass is off by default.
- [ ] Local bypass is gated by explicit config plus trusted client address
  checks.
- [ ] Local bypass writes require CSRF.
- [ ] Local bypass access and toggle activity are logged clearly.
- [ ] Ambiguous bypass identity fails closed.

## 2. CSRF Protection

### 2.1 Browser Session CSRF

**Current Pullbox implementation**

- Pullbox uses a lightweight synchronizer-token pattern:
  - a per-session CSRF token is embedded inside the signed session token
  - unsafe requests send it in the `X-CSRF-Token` header
- `src/pullbox/api/middleware.py` enforces CSRF on session-authenticated unsafe
  methods.
- Safe methods are `GET`, `HEAD`, and `OPTIONS`.
- Exempt paths include login, logout, setup, `/ping`, and `/health`.
- API key-authenticated requests are exempt.
- The main UI template injects HTMX defaults through `hx-headers`, so HTMX
  requests carry the token automatically.

**Required standard**

- Session-authenticated `POST`, `PUT`, `PATCH`, and `DELETE` requests require a
  valid CSRF token unless a documented exemption exists.
- CSRF tokens must be regenerated whenever Pullbox issues a new session.
- API key authentication remains CSRF-exempt.
- CSRF failures return `403` with a safe, clear payload.
- New UI code should use shared HTMX/header plumbing instead of custom per-page
  token wiring unless there is a clear reason.
- SameSite remains defense in depth, not a replacement for CSRF tokens.

**Current repo nuances**

- API-key CSRF exemption applies to API-key-only requests.
- If a browser session cookie is present, CSRF is still enforced even when an
  `X-API-Key` header is also present.
- Browser-facing unauthenticated state-changing routes that remain CSRF-exempt
  block modern cross-site browser requests through Fetch Metadata.
- Missing Fetch Metadata remains allowed for non-browser clients and older
  self-hosted browser contexts.

**Audit checks**

- [ ] State-changing session-authenticated routes pass through CSRF middleware.
- [ ] HTMX requests inherit `X-CSRF-Token` from shared template plumbing.
- [ ] API key-authenticated requests bypass CSRF and still authenticate
  correctly.
- [ ] Mixed session-cookie plus API-key-header requests still require CSRF.
- [ ] CSRF failures return consistent `403` JSON responses.
- [ ] Browser cross-site unsafe requests to CSRF-exempt login, setup, and
  logout paths are blocked through Fetch Metadata.

## 3. Secret Management

### 3.1 Encryption At Rest

**Current Pullbox implementation**

- `src/pullbox/core/encryption.py` uses Fernet from `cryptography`.
- The Fernet key is derived from the resolved application secret using
  PBKDF2-HMAC-SHA256 with 480,000 iterations. The resolved secret comes from
  `PULLBOX_SECRET_KEY` when set, otherwise from `config.xml`.
- Encrypted values are stored with an `enc:` prefix.
- Decrypting with the wrong secret key fails closed.
- Secret-bearing `system_config` rows use `value_type='secret'`.
- Current secret-bearing database values include:
  - `download_client_configs.api_key`
  - `download_client_configs.password`
  - `indexer_configs.api_key`
  - `system_config.value` for `comicvine_api_key`
  - `system_config.value` for `prowlarr_api_key`
- Current one-way authentication secrets include:
  - `users.password_hash`
  - `api_keys.key_hash`

**Required standard**

- Secrets at rest are encrypted, not merely obfuscated.
- Secret-derived keys are never logged.
- Decryption happens only when needed.
- New secret writes must use `encrypt_secret()`.
- Key rotation limitations must stay documented in operator-facing guidance.
- `decrypt_secret()` may keep plaintext passthrough only for legacy
  compatibility.

**Current repo nuances**

- Changing the resolved application secret without the old key makes encrypted
  secrets unrecoverable.
- API keys and passwords are not encrypted because they are one-way
  authentication secrets. They must remain hashed.

**Audit checks**

- [ ] Database-stored provider credentials use `encrypt_secret()` and
  `decrypt_secret()`.
- [ ] Secret-bearing `system_config` rows are typed as `secret`.
- [ ] Application secret change failure mode is documented.
- [ ] No audited code path stores decrypted credentials back to the database by
  accident.
- [ ] No audited code path logs decrypted credentials.

### 3.2 Secret Key Requirements

**Current Pullbox implementation**

- Startup validates the resolved application secret.
- Generated `config.xml` secret material is strong by default.
- Weak override behavior is limited to explicit test opt-in paths.

**Required standard**

- Normal Docker installs should let `config.xml` generate and persist the
  application secret under durable `/data`.
- Env-managed `PULLBOX_SECRET_KEY` overrides must be random and
  deployment-specific.
- Target guidance:
  - minimum 32 random bytes of entropy
  - generate with a cryptographically secure source
  - keep out of Git and sample configs
- Startup validation covers:
  - known sample or default key
  - obviously weak or too-short key material

**Current repo nuances**

- Test-only bypasses must stay impossible to trigger accidentally in ordinary
  runtime.

**Audit checks**

- [ ] Missing `config.xml` creates a strong generated secret on first run.
- [ ] Weak application secret overrides fail startup.
- [ ] Known sample/default secrets fail startup.
- [ ] Test opt-in behavior is explicit and narrow.

### 3.3 Logging And Error Surfaces

**Current Pullbox implementation**

- `src/pullbox/logging.py` registers `sanitize_sensitive_data` from
  `src/pullbox/core/log_sanitizer.py`.
- Redaction targets common secret-like keys, URL or DSN embedded credentials,
  secret query parameters, bearer tokens, and free-form secret-looking
  `key=value` assignments.
- Sanitization runs after exception and stack rendering.
- Direct stdlib `logging` imports are intentionally limited to logging
  infrastructure modules.
- Generic unhandled errors return real exception strings only in debug mode.

**Required standard**

- Secrets, session tokens, API keys, passwords, encryption keys, and database
  connection strings must never appear in logs.
- Sensitive paths and internal network details should be kept out of
  user-facing errors.
- Security logging must fail safely.
- Logging failure should not crash the app or leak secrets.
- Log and event fields from outside the trust boundary must be sanitized against
  log injection, including CR/LF and delimiter characters.

**Current repo nuances**

- Debug mode can expose more detail by design. Production mode must keep error
  responses generic.

**Audit checks**

- [ ] Structlog sanitization is active for console and file output.
- [ ] Application log messages do not interpolate secret values directly.
- [ ] Production error responses do not expose stack traces or internal paths.
- [ ] Session IDs and access tokens are not logged verbatim.
- [ ] Log/event data is sanitized against log-injection characters.

## 4. Input Validation And Injection Prevention

### 4.1 SQL Injection

**Current Pullbox implementation**

- Pullbox is ORM/Core-first.
- Static `text()` queries exist for narrow health and metadata checks such as
  `SELECT 1`, SQLite version checks, and Alembic version checks.
- Static guard coverage rejects SQL f-strings passed to application
  `execute()` or SQLAlchemy `text()`.
- Application `text()` calls must use local string literals.
- Alembic migration raw SQL is accepted only in migration files.

**Required standard**

- Application queries use SQLAlchemy ORM/Core.
- Static literal SQL is allowed only for liveness, SQLite or Alembic metadata,
  PRAGMAs, diagnostics, or other narrow cases with no user input.
- User input must never be interpolated into SQL text.

**Current repo nuances**

- Migration SQL must not become request-driven application query code.
- The database standards document has the deeper query guidance. This security
  section focuses on injection boundaries.

**Audit checks**

- [ ] No user input reaches `text()` or raw SQL execution.
- [ ] `text()` usage remains limited to static liveness, PRAGMA, and metadata
  queries.
- [ ] There is no manual SQL string construction for request-driven queries.

### 4.2 XSS Prevention

**Current Pullbox implementation**

- Pullbox uses `Jinja2Templates` and relies on Jinja autoescape for HTML
  templates.
- Provider issue descriptions render through a dedicated rich-HTML sanitizer
  before being marked safe.
- Settings pages use `tojson` or `tojson|forceescape` for structured values
  injected into JavaScript or Alpine attribute contexts.
- Settings media naming preview results are escaped before being composed into
  preview HTML.
- Remaining `|safe` usage is covered by a static allowlist contract.
- A repo-wide scan does not find `x-html` usage in UI JavaScript or templates.

**Required standard**

- Autoescaping remains the default for HTML templates.
- `|safe` is only allowed for reviewed, bounded, non-user-controlled content.
- New code should prefer `tojson` for structured JavaScript data injection.
- Third-party HTML must be sanitized before rendering or rendered as plain text.

**Current repo nuances**

- Internal component slots and formatter output can be safe when inputs are
  bounded and the call site is covered by the static contract.
- Provider HTML should always be treated as hostile until sanitized.

**Audit checks**

- [ ] All `|safe` usages are enumerated and guarded by a static contract.
- [ ] Provider-sourced rich text is sanitized through a reviewed allowlist before
  rendering.
- [ ] No new `x-html` or equivalent unsanitized HTML sinks are introduced.
- [ ] Third-party rich text is sanitized or escaped.

### 4.3 Path Traversal And Filesystem Safety

**Current Pullbox implementation**

- `src/pullbox/core/file_safety.py` centralizes dangerous-file and archive
  checks.
- `src/pullbox/core/naming.py` provides filesystem-safe name sanitization.
- Filename sanitization removes illegal characters, trims reserved endings,
  handles Windows reserved names, and bounds UTF-8 byte length.
- Completed-download post-processing runs the central file-safety gate before
  moving files into the library.
- Manual/import workflows use configured extension allowlists and sanitized
  destination naming before registration.

**Required standard**

- User- and provider-derived filesystem names must be sanitized before use.
- User-supplied paths must be resolved and validated against allowed roots.
- Security checks happen before import, post-processing, or manual file
  operations.
- Archive extraction helpers reject unsafe member paths before extraction.

**Current repo nuances**

- Library paths, download paths, and import paths are operational data. Treat
  them as untrusted until resolved and checked against the intended root.

**Audit checks**

- [ ] Provider-derived filenames are sanitized before filesystem use.
- [ ] User-supplied paths are resolved and checked against allowed roots.
- [ ] File-safety checks happen before import or post-processing moves.
- [ ] Archive member paths are checked before extraction.

### 4.4 Archive Safety

**Current Pullbox implementation**

- ZIP-based archives, including `.cbz` and `.zip`, are checked for:
  - path traversal
  - dangerous inner extensions
  - decompressed-size limit
- Default decompressed-size limit is `2000 MB`.
- ZIP/CBZ safety checks fail closed when an archive cannot be inspected.
- CBZ repack plus CBR/CB7 conversion reject unsafe archive member names before
  extraction.
- Non-ZIP archive families do not receive equivalent decompressed-size
  inspection today.

**Required standard**

- ZIP-slip and path traversal detection stay mandatory.
- Archive size limits remain configurable and fail closed.
- Unsafe member names are rejected before extraction.
- RAR/7z archive-bomb parity remains a documented limitation unless product
  requirements make deep inspection mandatory.

**Current repo nuances**

- Archive format support has real toolchain tradeoffs. Do not add helper tools
  to the runtime image without reviewing image size, attack surface, and scanner
  impact.

**Audit checks**

- [ ] ZIP-based archives are checked before extraction or processing.
- [ ] Archive decompressed-size limit is enforced for ZIP/CBZ archives.
- [ ] Dangerous extensions inside ZIP/CBZ archives are blocked when configured.
- [ ] RAR/7z size-inspection gaps remain documented.

### 4.5 SSRF And Configurable Peer URLs

**Current Pullbox implementation**

- Pullbox integrates with operator-configured peers such as indexers and
  download clients.
- Download client, indexer, and Prowlarr sync URL schemas use a shared operator
  peer URL validator.
- Peer URL validation accepts only `http` and `https`, requires a host, rejects
  embedded credentials, rejects whitespace, and normalizes trailing slashes.

**Required standard**

- New features must not accept arbitrary fetch URLs from request data and then
  request them server-side.
- Operator-configured peer URLs must:
  - allow only explicit `http` or `https` schemes
  - parse and normalize before use
  - reject malformed URLs early
  - keep redirects, callbacks, and connectivity tests narrow
- Prefer structured host, port, and path config over free-form URLs when a
  future feature can avoid full URLs.

**Current repo nuances**

- Self-hosted deployments may legitimately use local or private hosts.
- SSRF controls should distinguish trusted operator-configured peers from
  arbitrary user-controlled destinations.

**Audit checks**

- [ ] No request-driven generic URL fetch endpoint exists.
- [ ] Configured peer URLs use strict scheme parsing and validation.
- [ ] Connectivity tests do not become accidental open proxies.

## 5. HTTP Security

### 5.1 Outbound HTTP Calls

**Current Pullbox implementation**

- Pullbox uses `httpx.AsyncClient`.
- Provider classes set explicit client-level or request-level timeouts.
- ComicVine, Prowlarr, download clients, and health/update checks use async HTTP
  clients.
- Static contract tests reject:
  - application `requests` imports
  - `httpx.AsyncClient` construction without explicit timeout
  - literal `verify=False` call sites

**Required standard**

- Every outbound HTTP path must have explicit timeout behavior.
- External services use TLS verification by default.
- Any option to disable TLS verification for local or self-hosted peers must be
  explicit, narrow, and documented.
- Clients should be reused rather than recreated per call.
- Operator-configured peer URLs enforce explicit `http` or `https` schemes and
  reject malformed hostless values before connectivity tests or provider calls.

**Current repo nuances**

- Timeout can be client-level or request-level. What matters is that the path is
  bounded.

**Audit checks**

- [ ] Outbound HTTP clients have explicit timeout behavior.
- [ ] No application code imports `requests`.
- [ ] TLS verification is on by default.
- [ ] Peer URL validation rejects malformed or credential-bearing URLs.

### 5.2 Security Headers

**Current Pullbox implementation**

- `SecurityHeadersMiddleware` in `src/pullbox/api/middleware.py` sets:
  - `X-Content-Type-Options: nosniff`
  - `X-Frame-Options: DENY`
  - `X-XSS-Protection: 0`
  - `Referrer-Policy: strict-origin-when-cross-origin`
  - `Permissions-Policy: camera=(), microphone=(), geolocation=()`
  - `Content-Security-Policy`
  - `Strict-Transport-Security` when debug is false
- Current app CSP:

```text
default-src 'self'; script-src 'self' 'unsafe-inline' 'unsafe-eval'; style-src 'self' 'unsafe-inline'; img-src 'self' data: https://comicvine.gamespot.com https://*.gamespot.com; connect-src 'self'; font-src 'self'; frame-ancestors 'none'
```

- Current docs CSP:

```text
default-src 'self'; script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; img-src 'self' data: https://fastapi.tiangolo.com; connect-src 'self'; font-src 'self'; frame-ancestors 'none'
```

**Required standard**

- The listed headers remain mandatory.
- CSP can be tightened when feasible, but changes must be tested against HTMX,
  Alpine, Swagger/ReDoc, and current inline-script usage.
- `'unsafe-eval'` is a hardening target, not a permanent goal.
- HSTS should only be relied on for HTTPS deployments.

**Current repo nuances**

- The current CSP is intentionally documented as implemented, not as an ideal
  policy.
- Frontend refactors that reduce inline script/style usage can make CSP
  tightening much easier later.

**Audit checks**

- [ ] Security header middleware is registered for all HTTP responses.
- [ ] HSTS is emitted only for non-debug deployments.
- [ ] Current CSP is documented exactly.
- [ ] CSP inline-script and eval allowances remain tracked hardening targets.

### 5.3 Browser History Cache And Sensitive Pages

**Current Pullbox implementation**

- Sensitive detail pages opt out of HTMX history snapshots with
  `hx-history="false"`.
- Examples include:
  - `src/pullbox/ui/templates/pages/issue_detail.html`
  - `src/pullbox/ui/templates/pages/series_detail.html`

**Required standard**

- Pages that render sensitive data or secret-bearing workflows should not be
  cached into HTMX history or localStorage snapshots.
- Do not remove `hx-history="false"` from sensitive views as cosmetic cleanup.
- Any future page displaying plaintext secrets, recovery material, or sensitive
  operational detail should opt out of client-side history caching.

**Current repo nuances**

- This is easy to miss during UI cleanup because nothing obvious breaks. Treat
  the attribute as security behavior.

**Audit checks**

- [ ] Sensitive pages opt out of HTMX history snapshots.
- [ ] Secret-bearing workflows avoid client-side history caching.
- [ ] UI refactors preserve `hx-history="false"` where needed.

### 5.4 CORS

**Current Pullbox implementation**

- Pullbox does not enable permissive CORS by default.
- Debug mode enables a narrow localhost development CORS allowlist for local
  browser/API workflows.

**Required standard**

- Cross-origin API access remains opt-in and deployment-specific.
- Production CORS is normally handled at the reverse proxy, not by opening the
  app broadly.
- Debug CORS behavior must not become the production/default contract.

**Current repo nuances**

- Self-hosted operators can choose proxy-level CORS behavior, but the app should
  not ship a permissive default.

**Audit checks**

- [ ] Production CORS is not permissive by default.
- [ ] Debug CORS is scoped to local development.
- [ ] New cross-origin behavior is explicit and documented.

## 6. Dependency And Supply Chain Security

### 6.1 Repository Controls

**Current Pullbox implementation**

- `.github/workflows/security.yml` runs gitleaks, `pip-audit`, Safety, and
  Bandit, plus CodeQL when the repo is public or `PULLBOX_ENABLE_CODEQL=true`.
  `Security Required` treats CodeQL as informational unless
  `PULLBOX_REQUIRE_CODEQL=true`.
- `.github/workflows/docker.yml` runs Grype container scanning before publish.
- GitHub Actions are SHA-pinned with version comments.
- Workflows define explicit default permissions and per-job permissions.
- `.github/dependabot.yml` covers `pip`, `github-actions`, `docker`, and `npm`.
- Local and CI virtual environment bootstrap keeps packaging tools current
  enough to avoid known vulnerable toolchain versions.
- Public branch rulesets require the stable aggregate checks `CI Required`,
  `Security Required`, and `Workflow Hygiene Required` rather than brittle
  matrix or path-filtered job names.

**Required standard**

- GitHub Actions remain pinned to full SHAs with version comments.
- `pull_request_target` stays forbidden.
- Workflows define explicit default permissions.
- Jobs declare required permissions.
- Dependency scanning remains active in CI.
- Container scanning remains active for Docker artifacts.
- CodeQL must run on GitHub-hosted runners only.
- CodeQL should only become merge-blocking after an explicit policy change and
  backlog triage.
- Security checks should not silently degrade from blocking to advisory.
- Safety remains advisory unless a deliberate policy change says otherwise.

**Current repo nuances**

- CodeQL is skipped while the repository is private unless
  `PULLBOX_ENABLE_CODEQL=true`; it runs automatically after the repository
  becomes public, but remains informational unless `PULLBOX_REQUIRE_CODEQL=true`.
- Safety's legacy command still needs valid JSON artifact generation while it
  remains advisory.
- Before public visibility, scan the current tree, full Git history, release
  notes, PR/issue metadata, and refs for secrets and internal tool/provenance
  references.

**Audit checks**

- [ ] All `uses:` lines in workflows remain full-SHA pinned.
- [ ] Every pinned action line includes a version comment.
- [ ] No workflow uses `pull_request_target`.
- [ ] Workflows and jobs declare explicit permissions.
- [ ] `pip-audit` remains active in CI.
- [ ] Grype remains active for container images.
- [ ] Dependabot covers primary dependency surfaces.
- [ ] CodeQL status is documented accurately.
- [ ] Required branch checks use stable aggregate jobs.
- [ ] No untrusted workflow path runs on a self-hosted runner.

### 6.2 Release And Registry Hygiene

**Current Pullbox implementation**

- Release guidance lives in `docs/development/GIT_WORKFLOW.md`.
- Infrastructure and registry guidance lives in
  `docs/development/INFRASTRUCTURE.md`.
- Container publication uses GHCR and Docker Hub.
- Published container images are signed with keyless Sigstore/Cosign using
  GitHub Actions OIDC after registry publication.
- The Docker workflow publishes SBOM/provenance attestations and verifies GHCR
  and Docker Hub signatures by digest before reporting success.
- Docker metadata rules may publish semver-derived aliases depending on the
  release event.
- Untagged `main` merges may run Docker validation, but they do not publish
  registry images.

**Required standard**

- Release tags should be signed with the documented release process.
- Release images should be signed with keyless Sigstore/Cosign and verified by
  digest against the Docker workflow identity before the publish workflow
  succeeds.
- Release images should include SBOM/provenance attestations.
- GHCR and Docker Hub package tags should be reviewed after each release.
- Ordinary untagged `main` merges should not publish registry images.
- Unwanted registry aliases should be deleted deliberately rather than changing
  metadata behavior without a release-process decision.
- Release automation must not publish images from untrusted code.

**Current repo nuances**

- Registry tags are operational artifacts. A tag that is technically valid can
  still be confusing enough to remove.

**Audit checks**

- [ ] Release tag signing process is documented and followed.
- [ ] GHCR and Docker Hub image signatures verify with Cosign by digest.
- [ ] Release images publish SBOM/provenance attestations.
- [ ] GHCR and Docker Hub tags are reviewed after publication.
- [ ] Unwanted aliases are removed deliberately.
- [ ] Release workflows build from trusted refs.

## 7. Rate Limiting And Abuse Controls

### 7.1 Login Rate Limiting

**Current Pullbox implementation**

- `src/pullbox/core/rate_limiter.py` provides an in-memory, per-IP failed-login
  limiter.
- Defaults are:
  - `5` failed attempts
  - `300` second window
  - `900` second lockout
- State resets on process restart.
- Rate-limited login attempts return `429` with `LOGIN_RATE_LIMITED`, include
  `Retry-After`, and are audited.

**Required standard**

- Login abuse protection remains enabled by default.
- In-memory state is acceptable for self-hosted deployments, but it must stay
  documented as non-persistent.
- Brute-force defenses should not create easy denial-of-service behavior.

**Current repo nuances**

- Process-local rate limiting is a pragmatic self-hosted default. Distributed
  deployments may eventually need shared storage or proxy-level controls.

**Audit checks**

- [ ] Login limiter thresholds match code.
- [ ] Login rate-limit responses return `429` and `Retry-After`.
- [ ] Login rate-limited events are audited.
- [ ] Rate limiter state reset behavior is documented.

### 7.2 API Rate Limiting

**Current Pullbox implementation**

- `src/pullbox/core/api_rate_limiter.py` rate-limits by IP and endpoint tier.
- Defaults come from config:
  - tier 1: `60/min` for expensive endpoints
  - tier 2: `120/min` for write endpoints
  - tier 3: `300/min` for read endpoints
- HTMX requests are rate-limited normally.
- Client-supplied HTMX headers do not bypass throttling.
- Certain cover/static-style paths are exempt.
- Middleware adds `X-RateLimit-Limit` and `X-RateLimit-Remaining`.
- Exceeded API limits return `429` with `Retry-After: 60`.

**Required standard**

- API rate limiting stays on by default.
- Exemptions remain narrow and intentional.
- Future key-scoped or user-scoped throttling should complement IP-based
  controls unless a documented design replaces them.

**Current repo nuances**

- Expensive endpoints deserve stricter limits than ordinary reads. Keep tiers
  meaningful instead of flattening everything to one number.

**Audit checks**

- [ ] API limiter thresholds match code.
- [ ] API rate-limit responses return `429` and `Retry-After`.
- [ ] Exemptions are reviewed and justified.
- [ ] Rate limiting can be disabled only through explicit config.

## 8. Logging And Audit Trail

### 8.1 Sensitive Data Redaction

**Current Pullbox implementation**

- Structlog sanitization redacts common secret-like keys and bearer-token
  patterns.
- Structlog sanitization also redacts secret-looking free-form `key=value`
  assignments after exception traceback rendering.
- UTC timestamps are added centrally.
- Logs go to stdout and optionally a rotating file.

**Required standard**

- Security logs record enough context for incident review without recording
  secrets.
- Session IDs, access tokens, passwords, and primary secrets must not be logged
  directly.
- When session correlation is needed, prefer derived or hashed identifiers over
  raw token values.

**Current repo nuances**

- Redaction is a backstop, not permission to log secrets first and clean them up
  later.

**Audit checks**

- [ ] Logs redact common secret-like fields.
- [ ] Bearer tokens and API keys are not logged verbatim.
- [ ] Error logging does not leak secrets after traceback rendering.

### 8.2 Audit Events

**Current Pullbox implementation**

- Pullbox audits major auth/security events such as:
  - login success
  - login failure
  - login rate-limited
  - password changed
  - username changed
  - API key created or revoked
  - session invalidated
  - security configuration changed
  - local auth bypass toggled
- Request-handler audit writes use a shared best-effort writer.
- Audit logs are not pruned by ordinary cleanup or retention tasks.

**Required standard**

- Audit entries should capture the useful parts of "when, where, who, what":
  - timestamp
  - source
  - actor
  - action/event
  - outcome/reason
- Security-relevant configuration changes must be auditable.
- Audit data must be queryable through supported admin surfaces and protected
  from casual tampering.

**Current repo nuances**

- Any future audit-log retention policy must be explicit and tested separately.
- Best-effort audit logging should not break primary user flows, but silent
  failure should still be visible in logs.

**Audit checks**

- [ ] Security-significant events are audited consistently.
- [ ] Audit entries include UTC timestamps.
- [ ] Audit entries include source IP where applicable.
- [ ] Sensitive tokens are not stored verbatim in audit detail fields.

## 9. Docker And Deployment Security

### 9.1 Canonical Docker Source

**Current Pullbox implementation**

- The repo-root `docker/` folder is the source of truth for container guidance.
- `docker/Dockerfile` defines the production image.

**Required standard**

- Docker guidance should point to `docker/`, not an assumed repo-root
  `Dockerfile`.
- Container changes should be reviewed against runtime needs, image size,
  scanner findings, and startup behavior.

**Current repo nuances**

- Compose files used for local development may add mounts and conveniences that
  should not be copied into production hardening guidance without review.

**Audit checks**

- [ ] Docker guidance points to the repo-root `docker/` folder.
- [ ] Production Docker changes are made in `docker/Dockerfile`.
- [ ] Local-only compose behavior is not documented as production default.

### 9.2 Current Docker Posture

**Current Pullbox implementation**

- Builder image: `dhi.io/python:3.14-debian13-dev`
- Runtime image: `dhi.io/python:3.14-debian13`
- Runtime user: UID/GID `65532:65532`
- Builder-stage packages include:
  - `build-essential`
  - `curl`
  - `gzip`
  - `p7zip-full`
  - `poppler-utils`
  - `tzdata`
  - pinned official RARLAB UnRAR source compiled into `/usr/bin/unrar`
  - `ca-certificates`
- The final runtime image copies only required archive/PDF helper closures.
- The final runtime image intentionally does not include a shell, package
  manager, or `curl`.
- Health check runs through `python -m pullbox.docker_healthcheck`.
- Entrypoint runs through `python -m pullbox.docker_entrypoint`.
- Native HTTPS can be enabled through Settings > General or
  `PULLBOX_HTTPS_*` environment overrides. Certificate and key paths are
  constrained to `PULLBOX_HTTPS_CERT_ROOT`, which defaults to `/config/certs`.
- Library permission management is chmod-only. Pullbox can apply configured
  file and folder modes to Pullbox-created library artifacts, and the Utilities
  workflow can preview or apply recursive chmod changes under configured
  library roots.
- Pullbox does not attempt user-facing `chown` or `chgrp` changes.
- Symlinks and hardlinks are skipped by the permission engine so permission
  maintenance does not accidentally cross storage boundaries or mutate seeded
  torrent content.

**Required standard**

- Non-root execution remains mandatory.
- Runtime image stays hardened and intentionally minimal.
- Added OS packages must be justified by a runtime feature.
- Health-check tooling is part of the attack surface.
- Prefer the Python healthcheck over copying extra network tools into the
  runtime image.
- Native HTTPS certificate mounts should be read-only and readable by UID/GID
  `65532:65532`; certificate validation must fail closed at startup instead of
  silently falling back to HTTP.
- Native HTTPS uses the normal Pullbox port. Port separation should be handled
  by Docker/network mapping, not by adding a second in-process listener.
- Filesystem permission tools must stay scoped to configured library roots.
- Recursive permission utilities must support dry-run review before apply.
- Permission changes must log applied, skipped, failed, unsupported, and ignored
  outcomes clearly enough to diagnose mounted-storage problems.
- Do not add `chown` or `chgrp` support without a separate design that covers
  Docker UID/GID mapping, NAS behavior, rollback limits, and logging.
- Container hardening should consider:
  - read-only root filesystem where practical
  - explicit writable mounts only
  - dropped Linux capabilities where practical

**Current repo nuances**

- Archive and PDF helper tooling is intentionally present because it supports
  product features. Adding more tooling needs the same level of justification.
- Some host, NAS, and remote-storage permission problems cannot be fixed safely
  from inside a non-root container. Pullbox should report those cases clearly
  instead of pretending chmod can repair ownership, group, ACL, or mount-option
  issues.

**Audit checks**

- [ ] Runtime user is non-root.
- [ ] Package list is reviewed for necessity.
- [ ] Healthcheck behavior is documented accurately.
- [ ] `/data` permissions and secret exposure risks are documented.
- [ ] Runtime image does not grow shell/package-manager conveniences by accident.
- [ ] Permission tools remain chmod-only unless ownership support is designed
      deliberately.
- [ ] Recursive permission changes stay under configured library roots.
- [ ] Symlinks and hardlinks are skipped unless a future policy explicitly
      changes that behavior.
- [ ] Permission logs include enough detail to explain applied, skipped, failed,
      unsupported, and ignored outcomes.

## 10. Security Audit Checklist

Use this checklist when touching security-sensitive code. It is not meant to be
fancy. It is a quick guardrail for the places where regressions hurt.

### Authentication

- [ ] Bcrypt cost factor and password policy match code.
- [ ] Bcrypt 72-byte input-limit handling is explicit.
- [ ] Password-manager-friendly behavior remains intact.
- [ ] Session invalidation through `session_version` is preserved.
- [ ] Secret-key strength validation remains enforced.
- [ ] API keys are generated securely, hash-only at rest, revocable, expirable,
  and exposed raw only once.
- [ ] Local auth bypass remains disabled by default and fails closed.

### CSRF

- [ ] Session-authenticated unsafe methods require `X-CSRF-Token`.
- [ ] HTMX token propagation works from shared templates.
- [ ] API key auth remains CSRF-exempt.
- [ ] Mixed session plus API-key requests still require CSRF.
- [ ] Cross-site browser requests to unauthenticated CSRF-exempt state-changing
  endpoints are blocked.

### Secrets

- [ ] Encrypted, hashed, and plain config values are understood before changes.
- [ ] Secret rotation limitations remain documented.
- [ ] Secrets do not leak through logs or production error payloads.
- [ ] Decrypted credentials are not persisted back by accident.

### Injection And XSS

- [ ] No user-driven raw SQL exists.
- [ ] Static `text()` usage is narrow and justified.
- [ ] All `|safe` usage is reviewed.
- [ ] Third-party rich text is sanitized or escaped.
- [ ] Archive safety limitations for non-ZIP formats are documented.
- [ ] SSRF boundaries for configured peers and arbitrary URLs remain clear.

### HTTP And Headers

- [ ] Outbound HTTP clients have explicit timeout behavior.
- [ ] Operator-configured peer URLs enforce explicit `http` or `https` schemes.
- [ ] Current CSP is documented exactly.
- [ ] CSP hardening opportunities are tracked without breaking HTMX, Alpine, or
  docs.
- [ ] Sensitive HTMX views opt out of history/localStorage caching where needed.

### Supply Chain

- [ ] Workflow SHA pinning remains intact.
- [ ] `pull_request_target` is absent.
- [ ] Workflow and job permissions are explicit.
- [ ] `pip-audit` and Grype remain active.
- [ ] Safety artifact generation remains valid JSON while Safety is advisory.
- [ ] Dependabot covers `pip`, `github-actions`, `docker`, and `npm`.
- [ ] CodeQL status is documented accurately.
- [ ] Full-history secret and provenance scans are clean before public release.
- [ ] Release tags are signed through the documented process.

### Abuse Controls

- [ ] Login and API rate-limiter thresholds match code.
- [ ] Abuse-control responses and audit logging are verified.
- [ ] Exemptions are reviewed and justified.

### Deployment

- [ ] Docker guidance uses the repo-root `docker/` directory as source of truth.
- [ ] Non-root execution and package footprint are reviewed.
- [ ] Healthcheck design and package tradeoffs are documented.
- [ ] Runtime image remains intentionally minimal.

## 11. Primary References

- OWASP Password Storage Cheat Sheet: <https://cheatsheetseries.owasp.org/cheatsheets/Password_Storage_Cheat_Sheet.html>
- OWASP CSRF Prevention Cheat Sheet: <https://cheatsheetseries.owasp.org/cheatsheets/Cross-Site_Request_Forgery_Prevention_Cheat_Sheet.html>
- OWASP Session Management Cheat Sheet: <https://cheatsheetseries.owasp.org/cheatsheets/Session_Management_Cheat_Sheet.html>
- OWASP Logging Cheat Sheet: <https://cheatsheetseries.owasp.org/cheatsheets/Logging_Cheat_Sheet.html>
- OWASP File Upload Cheat Sheet: <https://cheatsheetseries.owasp.org/cheatsheets/File_Upload_Cheat_Sheet.html>
- OWASP SSRF Prevention Cheat Sheet: <https://cheatsheetseries.owasp.org/cheatsheets/Server_Side_Request_Forgery_Prevention_Cheat_Sheet.html>
- Python `secrets` docs: <https://docs.python.org/3/library/secrets.html>
- Docker Hardened Images docs: <https://docs.docker.com/dhi/>
