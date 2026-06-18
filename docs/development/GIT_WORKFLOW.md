# Pullbox Git Workflow

**Author:** Adam Hernandez
**Version:** 1.0
**Last Modified:** 2026-05-15

## Purpose

This document is the working Git and release workflow reference for Pullbox
contributors. It explains how branches move, how commits should be shaped, what
validation is expected before merge, and how releases are prepared without
losing sync between `main` and `develop`.

The workflow is intentionally simple. Feature work happens away from protected
branches, pull requests create review history, CI proves the branch, and release
tags are signed before automation publishes artifacts.

## Current Baseline Notes

- `main` is the release branch.
- `develop` is the integration branch.
- Feature, fix, docs, refactor, and hotfix branches are temporary.
- Pull requests are the normal merge path for release and cross-cutting work.
- Commits use conventional commit prefixes.
- TDD is the expected development loop for behavior changes.
- `make validate`, `make ci-local`, and `make ci-full` are the main local gates.
- Release tags are signed.
- Tag pushes trigger release and Docker publication workflows.
- After a release, `develop` is bumped back to the next `-dev` version.

## Table of Contents

1. [Branch Model](#1-branch-model)
2. [Branch Naming](#2-branch-naming)
3. [Daily Development Flow](#3-daily-development-flow)
4. [Commit Strategy](#4-commit-strategy)
5. [Commit Messages](#5-commit-messages)
6. [Validation Gates](#6-validation-gates)
7. [Pull Requests](#7-pull-requests)
8. [Release Flow](#8-release-flow)
9. [Post-Release Sync](#9-post-release-sync)
10. [Contributor PRs](#10-contributor-prs)
11. [Common Recovery Commands](#11-common-recovery-commands)
12. [Workflow Audit Checklist](#12-workflow-audit-checklist)

## 1. Branch Model

### 1.1 Current Pullbox implementation

Pullbox uses a three-layer branch model:

```text
main       release-ready code only
develop    integration branch for completed work
topic      short-lived branches for focused work
```

| Branch | Purpose | Lifetime |
|---|---|---|
| `main` | Release-ready code. Tags are cut from here. | Permanent |
| `develop` | Integration branch for completed work before release. | Permanent |
| topic branches | Feature, fix, docs, refactor, release prep, or hotfix work. | Temporary |

### 1.2 Required standard

- Do not work directly on `main`.
- Do not use `main` as a scratch branch.
- Keep `develop` releasable enough that a release PR can be opened without a
  large cleanup scramble.
- Use topic branches for meaningful work.
- Delete topic branches after merge.
- Sync local branches before starting new work.

### 1.3 Current repo nuances

- Pullbox is often developed by one primary maintainer, but PRs still matter.
  They preserve context, CI history, and a clean diff for future review.
- Feature branches can be broad when work is cohesive. Split them when the diff
  becomes too hard to review or validate.

### 1.4 Audit checks

- [ ] Work starts from an up-to-date base branch.
- [ ] Topic branches are short-lived and named clearly.
- [ ] `main` receives code only through the release flow.
- [ ] Merged topic branches are removed locally and remotely.

## 2. Branch Naming

### 2.1 Current Pullbox implementation

Common branch prefixes:

```text
feature/search-reliability-speed
fix/manual-search-blocklist
docs/standards-refresh
refactor/search-service-seams
hotfix/docker-smoke-startup
release/prepare-x-y-z
```

### 2.2 Required standard

- Branch names should be lowercase and descriptive.
- Use hyphen-separated words.
- Use a prefix that matches the work.
- Avoid personal, tool, or throwaway labels in branch names.
- Avoid references to implementation assistants, experiments, or temporary chat
  context.

### 2.3 Current repo nuances

- `docs/*` branches are fine for documentation-only work.
- `release/*` branches are useful when release prep needs a PR before merging to
  `main`.
- Hotfix branches should stay narrow and should be back-merged or otherwise
  synchronized into `develop`.

### 2.4 Audit checks

- [ ] Branch name describes the work.
- [ ] Branch name uses the right prefix.
- [ ] Branch name avoids personal or tool references.
- [ ] Hotfix branches have a plan to sync back to `develop`.

## 3. Daily Development Flow

### 3.1 Current Pullbox implementation

The normal development loop is:

```text
sync base -> create branch -> write tests -> implement -> validate -> commit -> push -> PR
```

### 3.2 Required standard

Start new work from a clean, current base:

```bash
git switch develop
git pull --ff-only origin develop
git switch -c feature/example-work
git push -u origin feature/example-work
```

For behavior changes, follow the TDD loop from `docs/development/CODE_STANDARDS.md`:

1. Read the existing code and tests.
2. Add or update the failing test.
3. Run the focused test and confirm the failure is meaningful.
4. Implement the smallest behavior change.
5. Run the focused test until it passes.
6. Run the relevant broader suite.
7. Run the full validation gate when ready.

End a work session with a clear state:

```bash
git status --short --branch
git log --oneline -5
git push
```

### 3.3 Current repo nuances

- A dirty worktree is fine during active work. It should not be ambiguous at
  handoff.
- Push useful checkpoints after meaningful steps, especially on long-running
  technical work.
- Do not commit generated or environment-specific files unless they are part of
  the repo contract.

### 3.4 Audit checks

- [ ] Base branch was synced before the topic branch started.
- [ ] Focused tests were used while developing.
- [ ] Worktree state is understood before handoff.
- [ ] Useful checkpoints are pushed for long-running branches.

## 4. Commit Strategy

### 4.1 Current Pullbox implementation

- Commits use conventional commit prefixes.
- One commit should represent one logical change.
- Test and implementation changes usually live in the same commit unless the
  test setup is large enough to split cleanly.

### 4.2 Required standard

- Keep commits focused.
- Include tests with the implementation when that makes the change easier to
  review.
- Split tests and implementation only when it improves review clarity.
- Avoid vague WIP commits in final PR history.
- Squash noisy local commits before merge when needed.
- Do not amend published commits unless the branch is still private or the
  rewrite is coordinated.

### 4.3 Current repo nuances

- A feature branch can have messy local commits while work is still active.
- Before merge, the history should explain the work, not the debugging path.
- Use `--force-with-lease`, not plain force push, after an intentional rebase.

### 4.4 Audit checks

- [ ] Each commit is one logical change.
- [ ] Tests are included where behavior changes.
- [ ] No vague WIP commits remain before merge.
- [ ] History rewrites use `--force-with-lease`.

## 5. Commit Messages

### 5.1 Current Pullbox implementation

Pullbox uses conventional commit prefixes:

| Type | Use |
|---|---|
| `feat` | New feature or user-visible functionality |
| `fix` | Bug fix |
| `test` | Test-only changes |
| `docs` | Documentation changes |
| `refactor` | Code restructuring without behavior change |
| `chore` | Tooling, config, dependency, or maintenance work |
| `ci` | Workflow or CI changes |
| `perf` | Performance improvement |
| `style` | UI polish or visual changes without logic changes |

GitHub Release notes are generated from these prefixes. Commits without a
recognized prefix are grouped under "Other Changes", so every mergeable commit
should use a prefix.

### 5.2 Required standard

Use this shape:

```text
type: short imperative summary

Optional body that explains what changed and why.
```

Good examples:

```text
fix: keep blocked manual-search results visible
```

```text
docs: refresh development standards
```

```text
ci: require signed release tags in release workflow
```

If a test expectation changes, explain why in the commit body:

```text
fix: apply blocklist filter to automatic searches

Manual searches still return blocked results for operator visibility.
Automatic searches now exclude them, so the expected result count changed in
the automated-search characterization test.
```

### 5.3 Current repo nuances

- Commit messages should not reference tools, chat sessions, or implementation
  assistants.
- A commit body is useful when the change modifies a contract, fixes a subtle
  bug, or explains an expected test update.

### 5.4 Audit checks

- [ ] Commit message uses a conventional prefix.
- [ ] Summary is specific enough to understand in `git log`.
- [ ] Contract or test expectation changes are explained.
- [ ] Commit text avoids tool or assistant references.

## 6. Validation Gates

### 6.1 Current Pullbox implementation

Main local validation commands:

| Command | Purpose |
|---|---|
| `make validate` | CSS build, lint, format, typecheck, non-E2E tests |
| `make ci-local` | GitHub-aligned CI shape, including migration and E2E checks |
| `make ci-full` | Full local gate with security and Docker smoke validation |
| `make test-a11y` | Contrast and accessibility browser checks |
| `make workflow-hygiene` | Workflow linting and hygiene checks |
| `make security-check` | Local security checks |

### 6.2 Required standard

- Run focused tests during development.
- Run `make validate` before ordinary code review.
- Run browser tests for UI behavior changes.
- Run `make test-a11y` for accessibility-sensitive UI changes.
- Run `make ci-local` before large or risky PRs.
- Run `make ci-full` before release, Docker, workflow, security, dependency, or
  broad cross-cutting merges.
- Do not knowingly merge with failing required CI.

### 6.3 Current repo nuances

- CSS drift is a real failure. If Tailwind input changes, rebuild and commit the
  generated CSS.
- E2E failures should be investigated from logs and artifacts before guessing.
- Security and Docker workflows can surface issues not covered by `make
  validate`.

### 6.4 Audit checks

- [ ] Focused tests pass.
- [ ] `make validate` passes for normal changes.
- [ ] UI changes have the appropriate browser coverage.
- [ ] Security, workflow, Docker, or release changes use the broader gates.
- [ ] Generated CSS is current when UI styles change.

## 7. Pull Requests

### 7.1 Current Pullbox implementation

- PRs are used to merge feature work into `develop` and release work into
  `main`.
- CI is expected to pass before merge.
- PR descriptions capture summary, validation, and review context.

### 7.2 Required standard

- PR title should be clear and free of tool or assistant references.
- PR description should include:
  - what changed
  - why it changed
  - validation performed
  - follow-up risks, if any
- Merge only after required checks are green.
- Protected branches require the stable aggregate checks `CI Required`,
  `Security Required`, and `Workflow Hygiene Required`.
- Do not make path-filtered workflows, such as Docker PR validation, required
  branch checks unless they emit an always-present aggregate status.
- Prefer squash or merge commits based on what preserves useful history for the
  branch.

Example PR body:

```markdown
## Summary
- Refreshes database, security, and code standards as living contributor docs.
- Moves standards into `docs/development`.

## Validation
- Reviewed docs for stale metadata and absolute local paths.
- Checked internal links.

## Notes
- No runtime code changed.
```

### 7.3 Current repo nuances

- PRs are useful even for solo-maintained work because they preserve CI and
  review history.
- Dependabot PRs should be merged in an order that keeps CI understandable.
- Release PRs should stay boring and easy to audit.

### 7.4 Audit checks

- [ ] PR title is clear and tool-neutral.
- [ ] PR body explains summary and validation.
- [ ] Required CI checks are green.
- [ ] Follow-up risks are called out.

## 8. Release Flow

### 8.1 Current Pullbox implementation

- Releases are prepared from `develop`.
- Release PRs merge `develop` into `main`.
- Release tags are created from `main`.
- Release tags are signed.
- Tag pushes trigger:
  - GitHub Release creation
  - Docker Release build, Grype scan, smoke test, GHCR/Docker Hub publish,
    Cosign signing, signature verification, and digest artifact upload
- Ordinary untagged `main` merges must not publish registry images.
- Pre-release tags are supported.

### 8.2 Required standard

Do not merge directly to `main`. Use a release PR.

Step 1: validate `develop`.

```bash
git switch develop
git pull --ff-only origin develop
make ci-full
```

Step 2: bump the version for release.

```bash
git switch -c release/prepare-X.Y.Z

# Edit src/pullbox/__init__.py from X.Y.Z-dev to X.Y.Z
# Update CHANGELOG.md by moving curated Unreleased notes into X.Y.Z
make release-changelog-check VERSION=X.Y.Z
git add src/pullbox/__init__.py CHANGELOG.md
git commit -m "chore: bump version to X.Y.Z for release"
git push -u origin release/prepare-X.Y.Z
```

Step 3: open a PR from the release branch to `main`.

```bash
gh pr create --base main --head release/prepare-X.Y.Z --title "Release vX.Y.Z" --body "Release vX.Y.Z

## Summary
- Release summary goes here.

## Validation
- [ ] make ci-full passes
- [ ] CI is green
- [ ] Docker smoke tests pass
- [ ] Manual verification complete"
```

Step 4: merge the release PR after CI is green.

```bash
gh pr merge <PR-NUMBER> --merge
```

Step 5: tag the release from `main`.

```bash
git switch main
git pull --ff-only origin main
git tag -s vX.Y.Z -m "Version X.Y.Z"
git tag -v vX.Y.Z
git push origin vX.Y.Z
```

### 8.3 Changelog And Release Notes

Pullbox has two release-note artifacts:

| Artifact | Source | When Updated | Purpose |
|---|---|---|---|
| `CHANGELOG.md` | Curated by maintainers | During release prep PR | Human-readable project history in the repo |
| GitHub Release notes | Curated `CHANGELOG.md` release section plus generated commit details from `.github/workflows/release.yml` | After a signed version tag and successful Docker Release workflow | Public release summary, detailed release event log, Docker pull commands, and image signature verification commands |

`CHANGELOG.md` is not generated automatically. Keep it concise and user-facing:
summarize the release, do not paste every commit. During release prep, move
completed `Unreleased` entries into a dated version section and create a fresh
empty `Unreleased` section above it.

The release workflow requires a curated `CHANGELOG.md` section for the tagged
version. It places that curated section first in the GitHub Release body, then
appends generated commit details. Run `make release-changelog-check VERSION=X.Y.Z`
during release prep so missing or empty release sections fail locally before the
tagged release pipeline runs.

Generated commit details are grouped by conventional commit prefixes. If a
commit lacks a recognized prefix, it lands under "Other Changes". This is why
commit prefixes are required even for solo maintenance work.

Recommended mapping:

| Prefix | Generated Release Category | Root Changelog Bucket |
|---|---|---|
| `feat` | Features | Added |
| `fix` | Bug Fixes | Fixed |
| `perf` | Performance | Performance |
| `test` | Testing | Testing |
| `docs` | Docs | Documentation |
| `ci` | CI / Build | CI / Build |
| `style` | Style / UI Polish | Changed |
| `refactor` | Refactors | Internal |
| `chore` | Chores | Internal |

### 8.4 Current repo nuances

- Signed tags let GitHub show verified tag provenance when signing is configured
  correctly.
- Release images are signed separately from Git tags. Docker Release uses
  keyless Sigstore/Cosign with GitHub Actions OIDC, verifies GHCR and Docker
  Hub signatures by digest before the workflow succeeds, and uploads that digest
  for the GitHub Release notes.
- If `git tag -s` fails, stop and configure a verified GPG or SSH signing key
  before pushing the tag.
- Docker metadata rules may publish semver-derived aliases during release-tag
  builds. Review GHCR and Docker Hub tags after release and delete unwanted
  aliases deliberately.
- GHCR may show extra `unknown/unknown` entries for SBOM/provenance attestation
  manifests. Keep them; they are supply-chain metadata, not runnable Pullbox
  images.
- A pre-release tag can be used to test the release pipeline:

```bash
git tag -s vX.Y.Z-rc1 -m "Release candidate X.Y.Z-rc1"
git tag -v vX.Y.Z-rc1
git push origin vX.Y.Z-rc1
```

### 8.5 Audit checks

- [ ] `develop` is green before release prep.
- [ ] Release version is committed before the release PR.
- [ ] `CHANGELOG.md` has a curated section for the release.
- [ ] `make release-changelog-check VERSION=X.Y.Z` passes.
- [ ] Release PR targets `main`.
- [ ] Release tag is signed and verified locally.
- [ ] Release workflows complete successfully.
- [ ] GHCR and Docker Hub image signatures verify with Cosign by digest.
- [ ] GHCR and Docker Hub tags are reviewed after publish.

## 9. Post-Release Sync

### 9.1 Current Pullbox implementation

- After a release, `develop` should move back to a `-dev` version.
- Hotfixes made around a release must be synchronized so `main` and `develop`
  do not drift unexpectedly.

### 9.2 Required standard

After the release tag is pushed and release automation is healthy, open a
post-release sync PR back to `develop`:

```bash
git switch main
git pull --ff-only origin main
git switch -c feature/sync-develop-X.Y.Z

# Edit src/pullbox/__init__.py from X.Y.Z to the next patch dev version,
# for example 0.9.10 -> 0.9.11-dev.
git add src/pullbox/__init__.py
git commit -m "chore: sync develop after vX.Y.Z release"
git push -u origin feature/sync-develop-X.Y.Z
gh pr create --base develop --head feature/sync-develop-X.Y.Z \
  --title "Sync develop after vX.Y.Z release" \
  --body "Syncs main back to develop after vX.Y.Z and reopens the dev version."
```

The required CI, Security, Workflow Hygiene, and Docker Validate workflows have
a narrow fast path for this exact PR shape:

- base branch is `develop`;
- head branch starts with `feature/sync-develop-`;
- PR is same-repository, not a fork or Dependabot PR;
- `origin/main` contains `origin/develop`;
- PR head contains `origin/main`;
- the only change after `origin/main` is `src/pullbox/__init__.py`;
- the version changes from the released `X.Y.Z` to the next patch
  `X.Y.(Z+1)-dev`.

Any other sync, hotfix, workflow, or source-code change falls back to the normal
full required checks.

If a release fix lands on `main` outside the normal release PR, bring it back
into `develop` before starting more feature work through a normal PR:

```bash
git switch develop
git pull --ff-only origin develop
git switch -c feature/sync-develop-hotfix
git merge origin/main
git push -u origin feature/sync-develop-hotfix
gh pr create --base develop --head feature/sync-develop-hotfix \
  --title "Sync develop after release hotfix"
```

### 9.3 Current repo nuances

- Keeping `main` and `develop` synchronized after release prevents the next
  cycle from rediscovering a release-only fix.
- Version bumps should be boring and isolated from unrelated changes.

### 9.4 Audit checks

- [ ] `develop` is bumped back to a `-dev` version after release.
- [ ] Release-only fixes are merged back into `develop`.
- [ ] Version bump commits contain only version files.
- [ ] Local and remote branch state is clean before the next branch starts.

## 10. Contributor PRs

### 10.1 Current Pullbox implementation

- `CONTRIBUTING.md` is the entrypoint for general contributor setup.
- Development standards live under `docs/development`.
- Branch protection and CI are expected to gate external changes.

### 10.2 Required standard

- External PRs should target `develop` unless they are documentation-only or
  explicitly release-related.
- Review the diff locally when behavior, security, dependencies, workflows, or
  migrations change.
- Run the relevant validation gate before merge.
- Do not allow contributors to bypass protected-branch checks.

### 10.3 Current repo nuances

- Forked PRs should be treated as untrusted code until CI and review prove
  otherwise.
- Avoid `pull_request_target` for untrusted PR execution.
- Fork and Dependabot PRs must run ordinary checks on GitHub-hosted runners,
  not on home self-hosted runners.
- Public-readiness work must scan the current tree, full Git history, release
  notes, PR/issue metadata, and refs before changing repository visibility.

### 10.4 Audit checks

- [ ] Contributor PR targets the right base branch.
- [ ] Required aggregate checks are green.
- [ ] Risky changes get local review where practical.
- [ ] Protected branch rules are not bypassed.

## 11. Common Recovery Commands

### 11.1 Current Pullbox implementation

These commands are safe, common tools for local recovery and inspection.

### 11.2 Required standard

Check branch and worktree state:

```bash
git status --short --branch
git branch --show-current
git log --oneline --decorate -10
```

Bring a local branch up to date:

```bash
git pull --ff-only origin develop
```

Stash incomplete work:

```bash
git stash push -m "describe the incomplete work"
git stash list
git stash pop
```

Move the last commit to a new branch when it was made on the wrong branch:

```bash
git switch -c fix/move-accidental-commit
git switch develop
git revert <commit-sha>
```

Inspect a remote PR locally:

```bash
git fetch origin pull/<PR-NUMBER>/head:pr-<PR-NUMBER>
git switch pr-<PR-NUMBER>
```

Clean up merged local branches:

```bash
git branch --merged develop
git branch -d branch-name
git remote prune origin
```

### 11.3 Current repo nuances

- Avoid destructive commands unless the intent is explicit and the worktree is
  understood.
- Prefer `git revert` for published history.
- Prefer `--force-with-lease` over plain force push when a topic branch rewrite
  is intentional.

### 11.4 Audit checks

- [ ] Worktree state is checked before recovery commands.
- [ ] Published history is reverted rather than rewritten.
- [ ] Destructive commands are avoided unless intentionally approved.
- [ ] Remote tracking refs are pruned after branch cleanup.

## 12. Workflow Audit Checklist

Use this checklist before merging branch or release work:

- [ ] Branch started from the correct base.
- [ ] Branch name is clear and tool-neutral.
- [ ] Commits are focused and use conventional prefixes.
- [ ] Commit messages explain contract or test expectation changes.
- [ ] Focused tests passed during development.
- [ ] `make validate` passed for ordinary code changes.
- [ ] Broader gates ran for risky changes.
- [ ] PR title and body explain summary and validation.
- [ ] Required CI checks are green.
- [ ] Release PRs target `main`.
- [ ] Release tags are signed and verified locally.
- [ ] Post-release `develop` bump is isolated and pushed.
- [ ] Merged topic branches are cleaned up locally and remotely.
