#!/usr/bin/env bash
# Exchange a GitHub App Manifest `code` for the App's credentials and write a
# ready-to-paste dokploy.env. Run locally — the private key never leaves your
# machine or this file. The code is single-use and expires ~1h after you clicked
# "Create GitHub App".
#
# Usage:  bash finish-registration.sh <code> [output.env]
set -euo pipefail

CODE="${1:?usage: finish-registration.sh <code> [output.env]}"
OUT="${2:-./dokploy.env}"

command -v curl >/dev/null || { echo "need curl"; exit 1; }
command -v python3 >/dev/null || { echo "need python3"; exit 1; }
command -v base64 >/dev/null || { echo "need base64"; exit 1; }

resp="$(curl -fsS -X POST -H 'Accept: application/vnd.github+json' \
  "https://api.github.com/app-manifests/${CODE}/conversions")" \
  || { echo "conversion request failed (code expired or already used?)"; exit 1; }

# Parse with python (robust) and base64-encode the PEM to a single line.
eval "$(python3 - "$resp" <<'PY'
import sys, json, base64, shlex
d = json.loads(sys.argv[1])
if 'id' not in d:
    print("echo 'ERROR: %s'; exit 1" % d.get('message', d))
    raise SystemExit
b64 = base64.b64encode((d['pem']).encode()).decode()
print("APP_ID=%s"   % shlex.quote(str(d['id'])))
print("SECRET=%s"   % shlex.quote(d['webhook_secret']))
print("KEYB64=%s"   % shlex.quote(b64))
print("SLUG=%s"     % shlex.quote(d.get('slug','')))
print("HTMLURL=%s"  % shlex.quote(d.get('html_url','')))
PY
)"

umask 077
cat > "$OUT" <<EOF
# Paste these into Dokploy → your service → Environment. Add OPENAI_KEY yourself.
OPENAI_KEY=REPLACE_WITH_YOUR_ZAI_GLM5_KEY
GITHUB_APP_ID=$APP_ID
GITHUB_WEBHOOK_SECRET=$SECRET
GITHUB_PRIVATE_KEY_B64=$KEYB64
EOF

echo "OK — GitHub App created."
echo "  slug   : $SLUG"
echo "  App ID : $APP_ID"
echo "  settings: $HTMLURL"
echo "  install : ${HTMLURL}/installations/new    <- install it on your repo"
echo "  wrote  : $OUT  (contains webhook secret + private key — keep local)"
echo
echo "Next: fill OPENAI_KEY in $OUT, paste all 4 vars into Dokploy env, Deploy."
