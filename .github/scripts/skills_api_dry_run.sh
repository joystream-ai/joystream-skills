#!/usr/bin/env bash
# Upload every non-baselined skill to the Skills API and delete it again (the body of the
# skills-api workflow, kept in a file so it can be run locally against a fake server:
#   SKILLS_API_BASE_URL=http://127.0.0.1:8765 ANTHROPIC_SKILLS_API_KEY=x bash .github/scripts/skills_api_dry_run.sh
# Exit 1 when any POST or DELETE answers outside 2xx, when curl cannot reach the API, or
# when NO skill was proven at all (a skip is never a pass). The key never appears on a
# command line: curl reads it from a mode-600 config file.
set -euo pipefail

: "${ANTHROPIC_SKILLS_API_KEY:?ANTHROPIC_SKILLS_API_KEY is required}"
base_url="${SKILLS_API_BASE_URL:-https://api.anthropic.com}"
version="${ANTHROPIC_VERSION:-2023-06-01}"
baseline="${VALIDATE_BASELINE:-validate-baseline.json}"

# Headers via a curl config file (-K), so the key is never an argv entry visible in `ps`
# or in a shell trace. The file is private to this process and removed on exit.
curl_config=$(mktemp)
response=$(mktemp)
trap 'rm -f "$curl_config" "$response"' EXIT
chmod 600 "$curl_config"
printf 'header = "x-api-key: %s"\nheader = "anthropic-version: %s"\n' \
  "$ANTHROPIC_SKILLS_API_KEY" "$version" > "$curl_config"

# Skills still listed in validate-baseline.json are known not to conform yet
# (CONTRIBUTING.md § 9 "Known exceptions"); every other skill must be accepted. A
# baseline key is a skill NAME (validate.py keys on the frontmatter name, which equals
# the directory name) and addresses skills/<name>; a plugin skill is baselined only by
# its full relative path plugins/<plugin>/skills/<name>. Matching is on the relative
# directory, never the basename, so a plugin skill never inherits a top-level skill's
# exception.
baselined_dirs=$(python3 - "$baseline" <<'PY'
import json, sys
keys = json.load(open(sys.argv[1]))
print(" ".join(k if "/" in k else f"skills/{k}" for k in keys))
PY
)

# METHOD URL [curl args...] -> prints the HTTP code; returns curl's exit code. A transport
# failure (DNS, refused, TLS) exits curl non-zero with no HTTP code and must be reported,
# not silently abort the script through set -e.
request() {
  local method=$1 url=$2
  shift 2
  curl -sS -o "$response" -w '%{http_code}' -K "$curl_config" -X "$method" "$url" "$@"
}

# The API's error message when it sent one, else the head of the body on one line.
api_error_text() {
  local message
  message=$(jq -r '.error.message // empty' "$response" 2>/dev/null || true)
  if [ -n "$message" ]; then
    printf '%s' "$message" | head -c 500
  else
    head -c 500 "$response" | tr '\n' ' '
  fi
}

status=0
proven=0
for dir in skills/*/ plugins/*/skills/*/; do
  [ -d "$dir" ] || continue
  rel_dir="${dir%/}"
  name=$(basename "$rel_dir")
  if [[ " $baselined_dirs " == *" $rel_dir "* ]]; then
    echo "skip (baselined): $rel_dir"
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
    echo "::error::$rel_dir has no files to upload"
    status=1
    continue
  fi

  # `cmd || rc=$?` keeps curl's own exit code (an `if !` branch would see the negated test).
  rc=0
  code=$(request POST "${base_url}/v1/skills" "${parts[@]}") || rc=$?
  if [ "$rc" -ne 0 ]; then
    echo "::error::curl could not complete POST /v1/skills for $rel_dir (curl exit $rc)"
    status=1
    continue
  fi
  if [[ "$code" != 2* ]]; then
    message=$(api_error_text)
    echo "::error::POST /v1/skills for $rel_dir returned $code${message:+: $message}"
    status=1
    continue
  fi
  id=$(jq -r '.id // empty' "$response")
  if [ -z "$id" ]; then
    echo "::error::POST /v1/skills for $rel_dir returned $code without an id"
    status=1
    continue
  fi
  echo "uploaded $rel_dir as $id"

  rc=0
  code=$(request DELETE "${base_url}/v1/skills/${id}") || rc=$?
  if [ "$rc" -ne 0 ]; then
    echo "::error::curl could not complete DELETE /v1/skills/$id for $rel_dir (curl exit $rc); delete it from the scratch org by hand"
    status=1
    continue
  fi
  if [[ "$code" != 2* ]]; then
    message=$(api_error_text)
    echo "::error::DELETE /v1/skills/$id ($rel_dir) returned $code${message:+: $message}; delete it from the scratch org by hand"
    status=1
    continue
  fi
  echo "deleted $rel_dir ($id)"
  proven=$((proven + 1))
done

echo "skills accepted by the Skills API and deleted again: $proven"
if [ "$proven" -eq 0 ]; then
  echo "::error::no skill was proven: every skill is baselined or failed, so this run verifies nothing"
  status=1
fi
exit $status
