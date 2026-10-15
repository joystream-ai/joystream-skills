#!/usr/bin/env bash
# Upload every non-baselined skill to the Skills API and delete it again (the body of the
# skills-api workflow, kept in a file so it can be run locally against a fake server:
#   SKILLS_API_BASE_URL=http://127.0.0.1:8765 ANTHROPIC_SKILLS_API_KEY=x bash .github/scripts/skills_api_dry_run.sh
# Exit 1 when any POST or DELETE answers outside 2xx. Never prints the key.
set -euo pipefail

: "${ANTHROPIC_SKILLS_API_KEY:?ANTHROPIC_SKILLS_API_KEY is required}"
base_url="${SKILLS_API_BASE_URL:-https://api.anthropic.com}"
version="${ANTHROPIC_VERSION:-2023-06-01}"
baseline="${VALIDATE_BASELINE:-validate-baseline.json}"

# Skills still listed in validate-baseline.json are known not to conform yet
# (CONTRIBUTING.md § 9 "Known exceptions"); every other skill must be accepted.
baselined=$(python3 -c 'import json,sys;print(" ".join(json.load(open(sys.argv[1]))))' "$baseline")

status=0
proven=0
for dir in skills/*/ plugins/*/skills/*/; do
  [ -d "$dir" ] || continue
  name=$(basename "$dir")
  if [[ " $baselined " == *" $name "* ]]; then
    echo "skip (baselined): $name"
    continue
  fi

  # One files[] part per regular file, with the path relative to the repository's
  # skills/ parent so SKILL.md sits at the top of one enclosing folder, as the API
  # requires. Symlinks are not followed (find -type f excludes them).
  parts=()
  while IFS= read -r -d '' file; do
    rel="${file#"$dir"}"
    parts+=(-F "files[]=@${file};filename=${name}/${rel}")
  done < <(find "$dir" -type f -print0 | sort -z)
  if [ "${#parts[@]}" -eq 0 ]; then
    echo "::error::$name has no files to upload"
    status=1
    continue
  fi

  response=$(mktemp)
  code=$(curl -sS -o "$response" -w '%{http_code}' -X POST "${base_url}/v1/skills" \
    -H "x-api-key: ${ANTHROPIC_SKILLS_API_KEY}" \
    -H "anthropic-version: ${version}" \
    "${parts[@]}")
  if [[ "$code" != 2* ]]; then
    message=$(jq -r '.error.message // empty' "$response" 2>/dev/null | head -c 500 || true)
    echo "::error::POST /v1/skills for $name returned $code${message:+: $message}"
    status=1
    rm -f "$response"
    continue
  fi
  id=$(jq -r '.id // empty' "$response")
  if [ -z "$id" ]; then
    echo "::error::POST /v1/skills for $name returned $code without an id"
    status=1
    rm -f "$response"
    continue
  fi
  echo "uploaded $name as $id"

  code=$(curl -sS -o "$response" -w '%{http_code}' -X DELETE "${base_url}/v1/skills/${id}" \
    -H "x-api-key: ${ANTHROPIC_SKILLS_API_KEY}" \
    -H "anthropic-version: ${version}")
  if [[ "$code" != 2* ]]; then
    message=$(jq -r '.error.message // empty' "$response" 2>/dev/null | head -c 500 || true)
    echo "::error::DELETE /v1/skills/$id ($name) returned $code${message:+: $message}; delete it from the scratch org by hand"
    status=1
    rm -f "$response"
    continue
  fi
  rm -f "$response"
  echo "deleted $name ($id)"
  proven=$((proven + 1))
done

echo "skills accepted by the Skills API and deleted again: $proven"
exit $status
