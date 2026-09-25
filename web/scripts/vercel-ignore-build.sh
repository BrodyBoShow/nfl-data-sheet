#!/bin/sh
# Vercel "Ignored Build Step" (docs/phases/P6.md step 9). Exit 0 skips the build; any
# other exit builds.
#
# Skips only when nothing that feeds the web app changed since the last successful
# deploy: web/ itself, plus the three repo files the prebuild step reads
# (scripts/sync-content.mjs). Diffing against VERCEL_GIT_PREVIOUS_SHA, not HEAD^, so a
# push of several commits is checked in full, not just its last commit.
#
# Every doubt builds: no previous SHA (first deploy), or a previous commit that isn't in
# the clone, exits 1.
set -u
[ -n "${VERCEL_GIT_PREVIOUS_SHA:-}" ] || exit 1
cd "$(git rev-parse --show-toplevel)" || exit 1
git cat-file -e "${VERCEL_GIT_PREVIOUS_SHA}^{commit}" 2>/dev/null || exit 1
git diff --quiet "$VERCEL_GIT_PREVIOUS_SHA" HEAD -- \
  web \
  docs/backtest_report.md \
  pipeline/synthesis/model_coefficients.json \
  pipeline/core/team_aliases.py
