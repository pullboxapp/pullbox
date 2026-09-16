#!/usr/bin/env bash

set -euo pipefail

if [ "$#" -lt 1 ] || [ "$#" -gt 2 ]; then
  echo "Usage: $0 <gitleaks-path> [base-ref=origin/develop]" >&2
  exit 1
fi

scanner="$1"
base_ref="${2:-origin/develop}"
# Resolve before scanning; an unavailable PR base must not silently skip history.
base_sha="$(git rev-parse --verify --end-of-options "${base_ref}^{commit}")"

"${scanner}" dir . --no-banner --redact --timeout=300
"${scanner}" git . --log-opts="--no-merges ${base_sha}..HEAD" --no-banner --redact --timeout=300
