#!/usr/bin/env bash
# Onboard a user (yourself or a friend) with their OWN Arte Suave account.
#
#   infra/add-user.sh <userid> [login-email]
#
# Stores the user's gym login + a generated token as SSM SecureStrings under
# /artesuave-mcp/users/<userid>/, and prints the bearer token they put in their
# Claude connector. The gym password is read interactively and never echoed,
# logged, or written to a file. Run this in a REAL terminal (it prompts).
#
# Env: AWS_PROFILE (optional; else default credential chain),
#      AWS_REGION (default eu-north-1).
set -euo pipefail
export MSYS_NO_PATHCONV=1

PROFILE="${AWS_PROFILE:-}"
REGION="${AWS_REGION:-eu-north-1}"
AWS=(aws --region "$REGION")
[ -n "$PROFILE" ] && AWS+=(--profile "$PROFILE")

USER_ID="${1:-}"
LOGIN="${2:-}"
if [ -z "$USER_ID" ]; then
  echo "usage: infra/add-user.sh <userid> [login-email]" >&2
  echo "  <userid>: lowercase, starts alphanumeric, [a-z0-9_-], <=32 chars" >&2
  exit 1
fi
if ! printf '%s' "$USER_ID" | grep -Eq '^[a-z0-9][a-z0-9_-]{0,31}$'; then
  echo "invalid userid '$USER_ID' (allowed: ^[a-z0-9][a-z0-9_-]{0,31}\$)" >&2
  exit 1
fi

if [ -z "$LOGIN" ]; then
  read -r -p "Arte Suave login (email) for '$USER_ID': " LOGIN
fi
[ -n "$LOGIN" ] || { echo "login is required" >&2; exit 1; }
read -r -s -p "Arte Suave password for '$USER_ID' (hidden): " PW; echo
[ -n "$PW" ] || { echo "password is required" >&2; exit 1; }

# random per-user secret; bearer the user configures is "<userid>.<secret>"
SECRET="$(openssl rand -hex 24)"
BASE="/artesuave-mcp/users/${USER_ID}"

put() {  # put a SecureString without the value appearing in the process list
  "${AWS[@]}" ssm put-parameter --name "$1" --type SecureString \
    --overwrite --cli-input-json "$(printf '{"Value":%s}' "$(printf '%s' "$2" | python -c 'import json,sys;print(json.dumps(sys.stdin.read()))')")" >/dev/null
  "${AWS[@]}" ssm add-tags-to-resource --resource-type Parameter \
    --resource-id "$1" --tags Key=project,Value=artesuave-mcp >/dev/null 2>&1 || true
}

echo ">> storing SSM params under ${BASE}/ ..."
put "${BASE}/login" "$LOGIN"
put "${BASE}/password" "$PW"
put "${BASE}/token" "$SECRET"

echo
echo "User '${USER_ID}' provisioned. Give them this BEARER TOKEN (treat as a password):"
echo
echo "    ${USER_ID}.${SECRET}"
echo
echo "They set it in their Claude connector as:  Authorization: Bearer ${USER_ID}.${SECRET}"
echo "Endpoint stays the same for everyone."
