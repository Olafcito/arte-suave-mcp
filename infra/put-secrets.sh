#!/usr/bin/env bash
# Put credentials + a generated MCP server secret into SSM SecureString.
# Reads LOGIN/PASSWORD from the repo .env (never echoed). Run once before deploy;
# re-run to rotate. Prints only the parameter names and the MCP secret to store
# in your connector — never the gym credentials.
set -euo pipefail
export MSYS_NO_PATHCONV=1

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PROFILE="${AWS_PROFILE:-nettoday-admin}"
REGION="${AWS_REGION:-eu-north-1}"
AWS=(aws --profile "$PROFILE" --region "$REGION")

# shellcheck disable=SC1091
set -a; source "$ROOT/.env"; set +a
: "${LOGIN:?LOGIN missing in .env}"; : "${PASSWORD:?PASSWORD missing in .env}"

put() { "${AWS[@]}" ssm put-parameter --name "$1" --value "$2" --type SecureString \
  --overwrite --tags Key=project,Value=artesuave-mcp >/dev/null 2>&1 \
  || "${AWS[@]}" ssm put-parameter --name "$1" --value "$2" --type SecureString --overwrite >/dev/null; }

SECRET="${ARTESUAVE_MCP_SECRET:-$(openssl rand -hex 32)}"

put /artesuave-mcp/login "$LOGIN"
put /artesuave-mcp/password "$PASSWORD"
put /artesuave-mcp/mcp-secret "$SECRET"

echo "stored: /artesuave-mcp/login /artesuave-mcp/password /artesuave-mcp/mcp-secret"
echo "MCP_SECRET=$SECRET"
echo "(use this secret as the Bearer token in your Claude connector; store it safely)"
