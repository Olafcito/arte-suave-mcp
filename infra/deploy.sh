#!/usr/bin/env bash
# One-command deploy for arte-suave-mcp.
#
#   infra/deploy.sh
#
# Env (override as needed):
#   AWS_PROFILE   (optional; else default credential chain)   AWS_REGION (default: eu-north-1)
#   STACK         (default: artesuave-mcp)
# Requires: aws cli, uv. No Docker, no SAM CLI. Builds Linux wheels for the
# binary deps (selectolax, pydantic-core) so it works from any dev OS.
#
# Credentials + server secret must already be in SSM (see infra/put-secrets.sh).
set -euo pipefail
export MSYS_NO_PATHCONV=1

# Native Windows aws/python need Windows-style paths; on Git Bash convert with
# cygpath, elsewhere use the path as-is.
winpath() { command -v cygpath >/dev/null 2>&1 && cygpath -m "$1" || printf '%s' "$1"; }

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# Auth: pass --profile only when AWS_PROFILE is set; otherwise the CLI's default
# credential chain applies (CI gets AWS_ACCESS_KEY_ID from the OIDC step).
PROFILE="${AWS_PROFILE:-}"
REGION="${AWS_REGION:-eu-north-1}"
STACK="${STACK:-artesuave-mcp}"
LWA="${LWA_LAYER_ARN:-arn:aws:lambda:eu-north-1:753240598075:layer:LambdaAdapterLayerX86:30}"
UV="${UV:-uv}"
AWS=(aws --region "$REGION")
[ -n "$PROFILE" ] && AWS+=(--profile "$PROFILE")

BUILD="$ROOT/.build"
DIST="$BUILD/pkg"
rm -rf "$BUILD"; mkdir -p "$DIST/deps"
DIST_W="$(winpath "$DIST")"

echo ">> installing Linux deps"
$UV pip install --python-platform x86_64-manylinux2014 --python-version 3.12 \
  --target "$DIST_W/deps" --only-binary=:all: \
  selectolax pydantic fastmcp uvicorn httpx >/dev/null

echo ">> assembling package"
cp -r "$ROOT/arte_suave_mcp" "$DIST/arte_suave_mcp"
cp "$ROOT/infra/run.sh" "$DIST/run.sh"
sed -i 's/\r$//' "$DIST/run.sh"; chmod +x "$DIST/run.sh"
find "$DIST" -name '__pycache__' -type d -prune -exec rm -rf {} +

echo ">> zipping"
ZIP="$BUILD/function.zip"; ZIP_W="$(winpath "$ZIP")"
"$UV" run python - "$DIST_W" "$ZIP_W" <<'PY'
import os, sys, zipfile
dist, zpath = sys.argv[1], sys.argv[2]
with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as z:
    for root, _, files in os.walk(dist):
        for f in files:
            p = os.path.join(root, f)
            rel = os.path.relpath(p, dist).replace("\\", "/")
            zi = zipfile.ZipInfo(rel)
            zi.external_attr = (0o755 if f == "run.sh" else 0o644) << 16
            zi.compress_type = zipfile.ZIP_DEFLATED
            with open(p, "rb") as fh:
                z.writestr(zi, fh.read())
names = zipfile.ZipFile(zpath).namelist()
assert any(n.startswith("deps/uvicorn/") for n in names), "build broken: uvicorn missing from zip"
assert "run.sh" in names, "build broken: run.sh missing"
print(f"   zip ok: {len(names)} entries")
PY
echo "   $(du -h "$ZIP" | cut -f1)"

# S3 bucket for code (created once, this project's own)
ACCOUNT=$("${AWS[@]}" sts get-caller-identity --query Account --output text)
BUCKET="artesuave-mcp-deploy-${ACCOUNT}-${REGION}"
if ! "${AWS[@]}" s3api head-bucket --bucket "$BUCKET" 2>/dev/null; then
  echo ">> creating $BUCKET"
  "${AWS[@]}" s3api create-bucket --bucket "$BUCKET" \
    --create-bucket-configuration LocationConstraint="$REGION" >/dev/null
  "${AWS[@]}" s3api put-bucket-tagging --bucket "$BUCKET" \
    --tagging 'TagSet=[{Key=project,Value=artesuave-mcp}]'
fi
KEY="function-$(date +%s).zip"
echo ">> uploading s3://$BUCKET/$KEY"
# native aws.exe needs the Windows path, not the MSYS path, or it silently
# uploads the wrong/short file (deps never make it to S3 -> Lambda can't import).
"${AWS[@]}" s3 cp "$ZIP_W" "s3://$BUCKET/$KEY" >/dev/null
# fail loudly if the uploaded object isn't the full package
UP_SIZE=$("${AWS[@]}" s3api head-object --bucket "$BUCKET" --key "$KEY" --query ContentLength --output text)
if [ "$UP_SIZE" -lt 1000000 ]; then
  echo "ERROR: uploaded object is only ${UP_SIZE} bytes — deps missing from zip" >&2
  exit 1
fi
echo "   uploaded ${UP_SIZE} bytes"

echo ">> deploying stack $STACK"
"${AWS[@]}" cloudformation deploy \
  --stack-name "$STACK" \
  --template-file "$(winpath "$ROOT/infra/template.yaml")" \
  --capabilities CAPABILITY_NAMED_IAM \
  --parameter-overrides LwaLayerArn="$LWA" CodeS3Bucket="$BUCKET" CodeS3Key="$KEY" \
  --tags project=artesuave-mcp

URL=$("${AWS[@]}" cloudformation describe-stacks --stack-name "$STACK" \
  --query "Stacks[0].Outputs[?OutputKey=='McpEndpoint'].OutputValue" --output text)
echo ">> deployed. MCP endpoint: ${URL}"
