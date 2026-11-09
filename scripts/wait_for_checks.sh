#!/usr/bin/env bash
# Waits for the Validate workflow to finish on a commit and exits 0 only if
# it passed. release.yml runs this first, so a merge whose checks fail or
# never finish does not publish a release HACS would offer.
#
#   REPO=owner/name SHA=<commit> scripts/wait_for_checks.sh
#
# WORKFLOW (default validate.yml), WAIT_LIMIT_S (default 2700) and
# WAIT_STEP_S (default 30) can be set; the tests shorten the last two.
set -euo pipefail

: "${REPO:?set REPO to owner/name}" "${SHA:?set SHA to the commit}"
WORKFLOW="${WORKFLOW:-validate.yml}"
LIMIT="${WAIT_LIMIT_S:-2700}"
STEP="${WAIT_STEP_S:-30}"

waited=0
while :; do
  state="$(gh run list --repo "$REPO" --workflow "$WORKFLOW" --commit "$SHA" \
    --json status,conclusion \
    --jq 'if length == 0 then "none" else "\(.[0].status) \(.[0].conclusion)" end' \
    2>/dev/null || echo "unreadable")"
  case "$state" in
    "completed success")
      echo "Validate passed on $SHA."
      exit 0 ;;
    "completed "*)
      echo "Validate finished as '${state#completed }' on $SHA, so no release is published." >&2
      exit 1 ;;
  esac
  if [ "$waited" -ge "$LIMIT" ]; then
    echo "Validate did not finish on $SHA within ${LIMIT}s (last seen: $state), so no release is published." >&2
    exit 1
  fi
  echo "Validate on $SHA: $state. Waiting..."
  sleep "$STEP"
  waited=$((waited + STEP))
done
