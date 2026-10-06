# GCD API V2

## Current Milestone

`PULLBOX_METADATA_GCD_API_V2_ENABLED` defaults to `false`. While it is off,
the access card is hidden and the registry does not construct a GCD API client.
This beta integration is not a production activation or an upstream API change.

With the flag enabled, Metadata settings offers a masked GCD API token field,
explicit enable/disable, token removal, and revision-checked saves. Credentials
use the existing encrypted source-policy storage. Saving does not make a remote
connection check; the operator can test the saved configuration separately.
The optional **Sign in with GCD instead** disclosure exchanges a username and
password once, tests the returned token, and enables the source only after that
check succeeds. Only the encrypted token is saved, not the username or password.

The source supports series discovery, exact series/issue reads, and canonical
issue catalogs through the existing source-aware preview/Add, linking, and
refresh services. Selection is explicit. A GCD ID is never treated as a
ComicVine or Metron ID, and this source supplies no unproven crosswalks.

## Beta Contract

The [official beta schema](https://beta.comics.org/api/v2/schema/swagger-ui/)
was checked on 2026-10-06. The fixed endpoint is
`https://beta.comics.org/api/v2/`, authenticated with `Authorization: Token`.
The release flag must remain disabled by default until the official release
and endpoint contract are confirmed.

- Series discovery currently uses the documented `name` substring filter and
  optional `year_began`, not the proposed ranked/exact search parameter.
- Each query inspects at most 100 candidates in one remote page. Further local
  result pages reuse that response. Broader queries retain the provider total
  and report incomplete results through the existing search outcome notice.
- Issue lists use the documented `variant_of=false` filter. For example,
  Badrock (1995), GCD series 50494, declares two issues but its unfiltered list
  contains five records including three cover variants. Its base catalog is
  issues 765609 and 765610. Cover variants are not separate canonical issues.
- Catalog pages must agree with the exact series ID, have unique issue IDs,
  complete counts, and a valid same-origin continuation. Unsupported or
  inconsistent catalogs stop Add rather than silently constructing membership.
- Issue designations retain letters, dashes and negative numbers. Unknown
  date components are not converted into invented dates. Fractional page counts
  remain unknown rather than rounded into reader page counts.
- Calls have bounded time/body sizes, no automatic retry or redirect following,
  and share account cooldowns. Errors expose typed outcomes, not credentials
  or remote response bodies.

## One-Time Sign-In

The official token endpoint is `POST /api/v2/auth/token/` with JSON
`username` and `password`, returning a `token`. Pullbox sends credentials only
to the fixed beta HTTPS endpoint, without following redirects. The browser
clears both fields as soon as the request is submitted and on disclosure close.
The service clears its credential model on success, failure, and cancellation;
neither credentials nor remote response bodies are logged or returned.

The local endpoint requires an interactive operator session and CSRF protection.
Input and response sizes are bounded, errors cannot echo invalid secret inputs,
and exchange/check calls run without a database transaction. Exchange requests
share an endpoint cooldown and do not automatically retry.

The returned token must pass the existing source connection check before the
revision-checked policy save. Failure preserves the saved token, enabled state,
and source priorities. A concurrent settings edit is not overwritten. Sign-in
cannot silently discard a pending direct-token draft, and a successful save
preserves a pending source-order draft. If the response is lost, **Load saved
GCD API settings** confirms the persisted state before retrying.

## Deliberately Incomplete

This milestone does not add GCD cover ingestion, conditional refresh, Story Arc
search, recent-issue discovery, or automatic identity matching. Authenticated
live GCD search/Add still needs an operator-supplied token or successful sign-in.
No production automatic-writing default, import/recovery policy, reader state,
or file-safety guarantee changes here. Cross-series cover variants are not
implicitly reassigned; inconsistent catalog membership requires review.

## Verification And Manual Test

Offline adapter tests use the observed beta wire fields and exact public GCD
identities. Owning API tests exercise real authentication, encrypted policy
saves, exact preview/Add, repeat safety, and wrong-parent refusal. Browser tests
cover the flag, token lifecycle, concurrent priority drafts, stale revisions,
safe failures, token-only sign-in, cancellation, transaction-free network calls,
mounted pending controls, and expanded light/dark/tron accessibility.
Tests never contact the live GCD API.

For an explicitly feature-enabled development instance:

1. Open Settings > Metadata, enable GCD API v2, enter a real token, and save.
   Use Test connection after saving; never put the token in chat or diagnostics.
   Alternatively, expand **Sign in with GCD instead** and submit your GCD
   credentials. The fields clear immediately; a successful check enables GCD
   and saves only its token. Save pending direct-token edits before using this
   option. Do not send credentials to anyone assisting with testing.
2. In Add Series, select GCD API v2 and search for a specific title/year.
   A broad search may be incomplete; narrow it instead of assuming absence.
3. Preview Badrock (1995), GCD 50494. Confirm the two-issue catalog and intended
   managed library root before Add. Repeat Add must not create a duplicate.
4. Confirm the series retains its GCD identity without a fabricated ComicVine
   ID. Existing paired writing still requires its normal identity, reconciliation,
   file ownership, and explicit approval checks.
5. Disable the source and save. The token is preserved until explicitly removed;
   an unsaved token draft must never be sent by Test connection.
