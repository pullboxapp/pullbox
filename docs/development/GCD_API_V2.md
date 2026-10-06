# GCD API V2

## Current Milestone

`PULLBOX_METADATA_GCD_API_V2_ENABLED` defaults to `false`. While it is off,
the access card is hidden and the registry does not construct a GCD API client.
This beta integration is not a production activation or an upstream API change.

With the flag enabled, Metadata settings offers a masked GCD API token field,
explicit enable/disable, token removal, and revision-checked saves. Credentials
use the existing encrypted source-policy storage. Saving does not make a remote
connection check; the operator can test the saved configuration separately.

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

## Deliberately Incomplete

This milestone does not add GCD cover ingestion, conditional refresh, Story Arc
search, recent-issue discovery, or automatic identity matching. One-time
username/password token exchange remains a separate user-facing milestone.
No production automatic-writing default, import/recovery policy, reader state,
or file-safety guarantee changes here. Cross-series cover variants are not
implicitly reassigned; inconsistent catalog membership requires review.

## Verification And Manual Test

Offline adapter tests use the observed beta wire fields and exact public GCD
identities. Owning API tests exercise real authentication, encrypted policy
saves, exact preview/Add, repeat safety, and wrong-parent refusal. Browser tests
cover the flag, token lifecycle, concurrent priority drafts, stale revisions,
safe failures, mounted pending controls, and light/dark/tron accessibility.
Tests never contact the live GCD API.

For an explicitly feature-enabled development instance:

1. Open Settings > Metadata, enable GCD API v2, enter a real token, and save.
   Use Test connection after saving; never put the token in chat or diagnostics.
2. In Add Series, select GCD API v2 and search for a specific title/year.
   A broad search may be incomplete; narrow it instead of assuming absence.
3. Preview Badrock (1995), GCD 50494. Confirm the two-issue catalog and intended
   managed library root before Add. Repeat Add must not create a duplicate.
4. Confirm the series retains its GCD identity without a fabricated ComicVine
   ID. Existing paired writing still requires its normal identity, reconciliation,
   file ownership, and explicit approval checks.
5. Disable the source and save. The token is preserved until explicitly removed;
   an unsaved token draft must never be sent by Test connection.
