# Dependency Audit Exceptions

This is a risk-acceptance record, not a claim that an upstream vulnerability
has been fixed. All unexcepted pip-audit findings and audit collection failures
remain blocking. Safety and Bandit retain their existing advisory status.

## NLTK Used By Safety

### October 14 Remediation: Remove The Dependency

The acceptance below expired and has **not** been renewed. NLTK still has no
patched release. Instead, development installs pin Safety to its merged upstream
[PR #913](https://github.com/pyupio/safety/pull/913), commit
`ac505430fc55403af338ece39489f3e3b01a7d97`. It removes NLTK and retains only the
pure edit-distance algorithm, not the vulnerable model-file APIs. Pullbox's
direct NLTK requirement is removed too; no scanner is removed or downgraded.
Production continues to exclude both packages. The immutable commit archive
is SHA-256 pinned, so installation also works in the Git-free development
image; no additional system package is needed.

This source still identifies itself as Safety 3.8.1. It is three commits beyond
the release and also includes upstream authentication updates, requiring
Authlib >=1.6.12, joserfc >=1.6.8 and safety-schemas 0.0.19. Verify the actual
Safety CLI and JSON scan output, not just its edit-distance helper. Replace the
source pin with a published release only after verifying that release includes
the NLTK removal and passes the same checks.

Pip-audit cannot audit a direct URL requirement. The shared wrapper therefore
normalizes **only** this exact Safety repository/commit to its installed
`safety==3.8.1` metadata version. It first verifies PEP 610 source provenance,
the archive's exact SHA-256, version, noneditable install and absence of an NLTK requirement.
Other URLs or changed/missing provenance fail closed. Every frozen transitive
dependency remains in the blocking audit; findings for Safety itself are not
suppressed. The raw scanner report remains unchanged, with source provenance
saved separately in `dependency-audit-report-provenance.json` and uploaded even
on failure. This version-based vulnerability check does not independently scan
the upstream source code; the exact merged patch must also be reviewed.

An existing virtual environment can retain orphaned NLTK after installing the
new Safety build. Pip may also keep the released Safety wheel because the
upstream source has the same version: verify `direct_url.json`, and explicitly
reinstall the exact pinned Safety source with `--force-reinstall --no-deps` after
resolving the updated development requirements if provenance is missing.
Verify no other installed distribution requires NLTK before removing that
orphan, then run `pip check` and the complete security gate. CI's
manifest-keyed environment cache creates a fresh environment for this change.

The expired record and expiry regressions below remain as historical evidence;
they grant no current exception. Source-pin coverage is in
`tests/unit/test_dependency_audit_source_pin.py`.

### Historical Acceptance (Expired)

| Field | Reviewed scope |
| --- | --- |
| Package | `nltk==3.10.3` |
| Development tool | `safety==3.8.1` |
| Advisory | `PYSEC-2026-3740`, `GHSA-8mgp-746c-j5xp`, `CVE-2026-81726` |
| Approval | Maintainer-approved temporary development-toolchain acceptance, explicitly renewed on 2026-10-07 |
| Review date | 2026-10-07 UTC; original review 2026-09-03 UTC |
| Expires | **2026-10-14 at 00:00 UTC**, exclusive; no automatic renewal |
| Owner | Pullbox maintainer |
| Production applicability | None; production image checks reject Safety or NLTK |

### Exposure And Rationale

The [NLTK advisory](https://github.com/nltk/nltk/security/advisories/GHSA-8mgp-746c-j5xp)
covers model-artifact APIs that bypass path containment when callers supply
untrusted filesystem paths. At review time, the maintainer advisory lists
versions through 3.10.3 as affected and no patched version. Do not change feeds
or assume 3.10.3 is fixed merely because a secondary database disagrees.

Safety 3.8.1 imports NLTK in `safety/tool/typosquatting.py` and calls
`nltk.edit_distance()` on package names. The reviewed flow does not use the
vulnerable model read/write APIs. Pullbox has no NLTK application imports.
Safety and NLTK are development dependencies; the production Docker build
installs `.[prod]` into a fresh environment, not `.[dev]`. The local production
image was also checked for both packages' absence during review.

The October 7 renewal retained exactly the same package versions, advisory,
and development-only scope. The upstream advisory still listed no patched
release, PyPI still published 3.10.3 as current, and the freshly built local
production image again passed the Safety/NLTK exclusion check. This renewal
does not assert that the vulnerability is fixed.

Development-only does not mean harmless or sandboxed: an added model-path
consumer could expose developer/runner filesystem access. This exception
accepts the presently reviewed exposure, not arbitrary future use.

### Enforcement And Evidence

- Both `scripts/security_check.sh` and `.github/workflows/security.yml` call
  `scripts/run_dependency_audit.py`, using the actual selected Python environment.
- The wrapper runs pip-audit with `--strict`, explicit PyPI service, aliases,
  descriptions, and JSON output. It does **not** pass a global NLTK ignore flag.
- The complete pinned `pip freeze` inventory includes transitive dependencies.
  `--no-deps --disable-pip` audits those exact versions directly instead of
  recreating an environment whose preinstalled packaging tools can be omitted
  from pip's installation report. This does not exclude transitive packages.
- Only this finding for NLTK 3.10.3 alongside Safety 3.8.1 can be accepted,
  before the fixed expiry and while neither is declared in runtime dependency
  groups. A changed version, newly reported fixed version, or other finding
  blocks the gate. Non-development optional dependency groups are checked too.
- Invalid, skipped, duplicate, empty, stale, partial, or inconsistent scanner
  evidence cannot turn a failed audit into success. The scanned inventory must
  include every package at its exact frozen version in the requirements export.
  Expiry is checked when the completed audit is evaluated, not when it starts.
- `dependency-audit-report.json` retains the raw report, including this finding;
  GitHub uploads it even when the audit fails. Logs explicitly identify accepted
  findings, expiry, and any remaining blocking findings.
- `scripts/verify_container_security_runtime.py` independently checks actual
  production images for Safety/NLTK absence before accepting image validation.
- The pre-existing Pygments `CVE-2026-4539` exception is unchanged.

### Removal And Re-Review

Upgrade when an upstream fix is available and remove the NLTK exception after
verifying the actual toolchain. Re-review any Safety/NLTK version change,
application dependency/use change, or new model-artifact consumer. At expiry,
an affected toolchain fails again until upgraded or explicitly re-reviewed;
do not extend the date merely to obtain green CI. A clean audit with no
remaining NLTK finding continues to pass after expiry.

Regression coverage lives in `tests/unit/test_dependency_audit_policy.py`,
`tests/unit/test_verify_container_security_runtime.py`, and the shared-workflow
contracts. Run those first, then `make security-check`, `make workflow-hygiene`,
and `make ci-full` before merging.
