#!/usr/bin/env bash
# Test de bout en bout : lance l'image Docker, envoie une fausse IPA, verifie /source.json
# Usage : scripts/smoke-test.sh [nom-de-l-image]   (defaut : ipa-server:test)
set -euo pipefail

IMAGE="${1:-ipa-server:test}"
NAME="ipa-smoke-$$"
PORT=18000
TMP="$(mktemp -d)"
trap 'docker rm -f "$NAME" >/dev/null 2>&1 || true; rm -rf "$TMP"' EXIT

# Fausse IPA : un zip avec Payload/X.app/Info.plist
python3 - "$TMP/Test.ipa" <<'EOF'
import plistlib, sys, zipfile
plist = {
    "CFBundleDisplayName": "SmokeApp",
    "CFBundleIdentifier": "com.smoke.app",
    "CFBundleShortVersionString": "9.9.9",
    "MinimumOSVersion": "15.0",
}
with zipfile.ZipFile(sys.argv[1], "w") as z:
    z.writestr("Payload/Smoke.app/Info.plist", plistlib.dumps(plist))
EOF

docker run -d --name "$NAME" -p "127.0.0.1:$PORT:8000" \
  -e UPLOAD_TOKEN=smoke -e PUBLIC_URL=https://ipa.test "$IMAGE" >/dev/null

echo "Attente du serveur..."
for _ in $(seq 1 30); do
  curl -fs "http://127.0.0.1:$PORT/health" >/dev/null 2>&1 && break
  sleep 1
done
curl -fs "http://127.0.0.1:$PORT/health" | grep -q ok

echo "Upload sans token doit etre refuse (403)"
code=$(curl -s -o /dev/null -w "%{http_code}" -F file=@"$TMP/Test.ipa" "http://127.0.0.1:$PORT/upload")
[ "$code" = "403" ]

echo "Upload avec token"
curl -fs -H "X-Token: smoke" -F file=@"$TMP/Test.ipa" "http://127.0.0.1:$PORT/upload" >/dev/null

echo "Verification de source.json"
curl -fs "http://127.0.0.1:$PORT/source.json" | python3 -c '
import json, sys
d = json.load(sys.stdin)
app = d["apps"][0]
assert app["bundleIdentifier"] == "com.smoke.app", app
assert app["version"] == "9.9.9", app
assert app["downloadURL"] == "https://ipa.test/ipa/Test.ipa", app
print("OK :", app["name"], app["version"])
'

echo "Telechargement de l'IPA"
curl -fs -o /dev/null "http://127.0.0.1:$PORT/ipa/Test.ipa"

echo "Smoke test reussi"
