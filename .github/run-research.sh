#!/usr/bin/env bash
# Commit a durable reservation before giving the Responses API a chance to bill.
set -euo pipefail

mode=$1
output=$2
if [[ -z "${OPENAI_API_KEY:-}" ]]; then
  echo 'OpenAI research skipped: OPENAI_API_KEY is unavailable.'
  exit 1
fi

branch=monitor-research-budget
budget_dir="$RUNNER_TEMP/oem-research-budget"
ledger="$budget_dir/reservations.json"
git config user.name 'github-actions[bot]'
git config user.email '41898282+github-actions[bot]@users.noreply.github.com'
remote_ref=$(git ls-remote origin "refs/heads/$branch")
if [[ -z "$remote_ref" ]]; then
  empty_tree=$(git mktree </dev/null)
  initial=$(git commit-tree "$empty_tree" -m 'Initialize OpenAI research reservation ledger')
  git push origin "$initial:refs/heads/$branch"
fi
git fetch --depth=1 origin "$branch"
git worktree add --detach "$budget_dir" FETCH_HEAD
status=0
python research.py reserve "$ledger" --mode "$mode" --run-id "$GITHUB_RUN_ID" || status=$?
if [[ "$status" == 3 ]]; then
  if [[ "$mode" == preview ]]; then
    echo 'Requested API preview was skipped due to the reservation limit.'
    exit 1
  fi
  exit 0
elif [[ "$status" != 0 ]]; then
  exit "$status"
fi
git -C "$budget_dir" add reservations.json
git -C "$budget_dir" commit -m "Reserve OpenAI research for run $GITHUB_RUN_ID"
git -C "$budget_dir" push origin "HEAD:refs/heads/$branch"
python research.py run "$output"
