#!/usr/bin/env bash
set -euo pipefail

tag=${1:-}
if [[ ! "$tag" =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
  echo "Usage: $0 vX.Y.Z" >&2
  exit 2
fi

repo=$(gh repo view --json nameWithOwner --jq .nameWithOwner)
if [[ "$repo" != "mate-vasarhelyi/blindchicken-project" ]]; then
  echo "Run this helper in the blindchicken-project fork, got $repo" >&2
  exit 1
fi
repo_owner=${repo%%/*}
if [[ $(git branch --show-current) != main || -n $(git status --porcelain) ]]; then
  echo "Run this helper from a clean main branch" >&2
  exit 1
fi

release=$(gh release view "$tag" --repo opf/openproject --json tagName,isDraft,isPrerelease)
if [[ $(jq -r .tagName <<<"$release") != "$tag" ||
      $(jq -r .isDraft <<<"$release") != false ||
      $(jq -r .isPrerelease <<<"$release") != false ]]; then
  echo "No published stable OpenProject release exists for $tag" >&2
  exit 1
fi

ref=$(gh api "repos/opf/openproject/git/ref/tags/$tag")
official_sha=$(jq -r .object.sha <<<"$ref")
if [[ $(jq -r .object.type <<<"$ref") == tag ]]; then
  official_sha=$(gh api "repos/opf/openproject/git/tags/$official_sha" --jq .object.sha)
fi

git fetch --no-tags https://github.com/opf/openproject.git \
  "refs/tags/$tag:refs/bcp-upstream-tags/$tag"
tag_sha=$(git rev-parse "refs/bcp-upstream-tags/$tag^{commit}")
if [[ "$tag_sha" != "$official_sha" ]]; then
  echo "Fetched tag $tag does not match the official GitHub release ref" >&2
  exit 1
fi

git fetch --quiet origin main
branch="upstream-$tag"
if git show-ref --verify --quiet "refs/heads/$branch"; then
  echo "Branch $branch already exists" >&2
  exit 1
fi

git switch --create "$branch" origin/main
if ! git merge --no-edit --no-ff "$tag_sha"; then
  echo "Resolve conflicts on $branch, commit the merge, then push and open the PR manually." >&2
  exit 1
fi

git push --set-upstream origin "$branch"
body=$(mktemp)
trap 'rm -f "$body"' EXIT
cat >"$body" <<EOF
Merge the official OpenProject $tag release into the Blind Chicken fork.

Upstream release: https://github.com/opf/openproject/releases/tag/$tag
Upstream commit: $tag_sha

Review the merge, run the BCP image workflow on this branch, then deploy through the documented backup and acceptance steps.
EOF
gh pr create --repo "$repo" --base main --head "$repo_owner:$branch" \
  --title "Update upstream to $tag" --body-file "$body"
