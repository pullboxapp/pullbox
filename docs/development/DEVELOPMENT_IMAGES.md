# Signed development images

The opt-in `edge` channel packages selected, validated commits from `develop`.
It is not a general-availability release and does not update `latest`, create a
version tag, or create a GitHub Release. Builds are manual; merging to `develop`
does not automatically run full validation or publish an image.

This workflow change must be merged into `develop` before using this runbook.
Local workflow tests do not demonstrate that an image has been published.

## Maintainer publication

1. Select a reviewed commit on `develop`. The application version must end in
   `-dev`. Coordinate merges while validating so all four runs test the same SHA.
2. Explicitly run the existing full validation workflows against `develop`:

   ```bash
   gh workflow run ci.yml --ref develop
   gh workflow run security.yml --ref develop
   gh workflow run workflow-hygiene.yml --ref develop
   gh workflow run docker-validate.yml --ref develop
   ```

   These are full runs, including the Python matrix, browser/accessibility
   tests, migration checks, security checks, and trusted production Docker
   validation. They consume CI resources. The image workflow never dispatches
   these checks on its own.

3. Wait for all four to complete successfully and confirm they tested the exact
   same commit that is still selected on `develop`. For each workflow, inspect
   its manual runs, for example:

   ```bash
   gh run list --workflow ci.yml --branch develop --event workflow_dispatch \
     --json databaseId,headSha,status,conclusion
   ```

4. Explicitly publish that validated development snapshot:

   ```bash
   gh workflow run docker-release.yml --ref develop
   ```

   There is no custom tag input. A manual run on a feature branch, `main`, or a
   version tag is rejected before checkout and before using a self-hosted Docker
   runner. Stable releases still use the existing signed version-tag push flow.

5. Wait for the complete Docker Release workflow, including signature
   verification and promotion, to succeed. Share its run URL, source SHA, and
   published digest with testers. The `release-image-digest` artifact contains
   `digest.txt`; `tag.txt` is intentionally empty for a development build.

The development gate reads GitHub Actions evidence with read-only `actions` and
`checks` permissions. For each of the four workflows it requires the latest
manual `develop` run for the exact SHA, from this repository, with trusted
GitHub Actions check-suite provenance. The workflow must have succeeded and its
required jobs and meaningful steps must have completed successfully. PR
aggregates, preflight-only checks, release-sync shortcuts, skipped jobs, older
successful runs superseded by failures, and another commit's checks do not
qualify. Missing or malformed API evidence blocks publication.

The existing advisory Bandit findings policy remains advisory, but Bandit must
actually run and upload its report. The development channel requires successful
whole workflow runs: an informational CodeQL job failure may therefore prevent
publication even when the aggregate Security Required check is green.

If `develop` advances between validation and dispatch, validate the newly
selected commit before retrying. The exact-SHA gate is also checked again before
promoting `edge`, so a new failed or unfinished validation run blocks promotion.

For a failed development image run, use **Re-run all jobs** or start a new manual
dispatch after resolving the failure. Do not selectively rerun a platform build,
failed jobs, or the signing/promotion job: GitHub retains earlier preparation
outputs during partial reruns, which could otherwise reuse an old build tag for
a newly rebuilt digest. The platform jobs and final promotion reject any
preparation tag that does not match the current run attempt. A full rerun creates
fresh preparation metadata and a distinct build-attempt tag.

## Tags, digests, and signatures

Both registries carry the same multi-platform digest:

- `ghcr.io/pullboxapp/pullbox`
- `docker.io/pullbox/pullbox`

Development publication uses three kinds of reference:

| Reference | Purpose |
| --- | --- |
| `edge` | Rolling opt-in channel, updated only after both registry signatures verify. |
| `sha-<full-commit-sha>-run-<run-id>-<build-attempt>` | Unique build reference; a new build or attempt gets a new tag instead of replacing a plain SHA alias. |
| `@sha256:<digest>` | Definitive immutable artifact identity; preferred for reproducible tester reports. |

The builders first publish untagged platform digests. After Grype, smoke, and
runtime checks, the manifest job creates `candidate-<run-id>-<attempt>` staging
tags. These staging tags are not the tester channel and may be unsigned if a
later step fails. The signing job signs and verifies the multi-platform digest
in both registries, then promotes that exact index to the development tags
without rebuilding it. SBOM/provenance attestations remain attached.

Run-specific tags avoid accidentally reusing a commit tag when rebuilding the
same source. This is not a claim of bit-for-bit reproducible builds or registry
enforced tag immutability: timestamps, build provenance, dependencies, or base
image updates can change the digest. Pin the digest to reproduce the exact
artifact. Staging references are retained on failure for investigation; registry
cleanup is a separate deliberate maintenance operation.

Verify the digest before running it, substituting the digest reported by the
successful workflow:

```bash
cosign verify \
  --certificate-identity 'https://github.com/pullboxapp/pullbox/.github/workflows/docker-release.yml@refs/heads/develop' \
  --certificate-oidc-issuer 'https://token.actions.githubusercontent.com' \
  'ghcr.io/pullboxapp/pullbox@sha256:<digest>'
```

For Docker Hub, use the same command with
`docker.io/pullbox/pullbox@sha256:<digest>`. Signing establishes artifact origin
and integrity, not stability or freedom from bugs. Docker does not automatically
enforce this Cosign verification when pulling an image.

Promotion is not a cross-registry transaction. If the second registry update
fails, one registry's `edge` may advance while the other remains on the previous
snapshot; the workflow fails and both candidate signatures have already been
verified. Do not advertise the build as successfully published until the entire
workflow is green. Investigate and use **Re-run all jobs** or a new manual
dispatch; do not bypass verification or manually point `edge` at an unsigned
candidate.

## Tester isolation and rollback

Use a separate development container with separate host port, appdata/database,
library, downloads, and import-source copies. Do not share a production `/data`
mount or let development automation modify the production library or downloader
queue. Use a dedicated downloader category/destination if testing acquisition.

Prefer pinning the supplied digest over following `edge` with an automatic
container updater. Keep the source SHA, image digest, architecture, and run URL
with each test report. Before upgrading a test instance, take a consistent full
appdata backup (including `config.xml`) and protect any test files you need.

Database migrations run at startup. Changing the image back to a GA tag does
not roll the database or filesystem back. Restore the matching pre-upgrade
appdata backup and necessary files into the isolated test environment, or start
again with fresh test state. Do not attach a development-migrated database to an
older production image.

## Verification references

- [GitHub workflow-run API](https://docs.github.com/en/rest/actions/workflow-runs)
- [GitHub job evidence for a specific run attempt](https://docs.github.com/en/rest/actions/workflow-jobs#list-jobs-for-a-workflow-run-attempt)
- [Docker manifest-index copying](https://docs.docker.com/reference/cli/docker/buildx/imagetools/create/)
- [Cosign verification](https://docs.sigstore.dev/cosign/verifying/verify/)
