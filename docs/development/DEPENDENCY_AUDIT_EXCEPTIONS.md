# Dependency Audit Exceptions

This is a risk-acceptance record, not a claim that an upstream vulnerability
has been fixed. All unexcepted pip-audit findings and audit collection failures
remain blocking. Safety and Bandit retain their existing advisory status.

## NLTK Used By Safety

| Field | Reviewed scope |
| --- | --- |
| Package | `nltk==3.10.3` |
| Development tool | `safety==3.8.1` |
| Advisory | `PYSEC-2026-3740`, `GHSA-8mgp-746c-j5xp`, `CVE-2026-81726` |
| Approval | Maintainer-approved temporary development-toolchain acceptance |
| Review date | 2026-09-03 UTC (2026-09-02 local development date) |
| Expires | **2026-10-03 at 00:00 UTC**, exclusive; no automatic renewal |
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
