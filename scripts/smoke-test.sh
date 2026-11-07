#!/usr/bin/env bash
# Test de bout en bout : lance l'image Docker, puis verifie l'API, le catalogue public,
# le panel admin (connexion + import) et /source.json.
# Usage : scripts/smoke-test.sh [nom-de-l-image]   (defaut : ipa-server:test)
set -euo pipefail

IMAGE="${1:-ipa-server:test}"
NAME="ipa-smoke-$$"
PORT=18000
URL="http://127.0.0.1:$PORT"
TMP="$(mktemp -d)"
trap 'docker rm -f "$NAME" >/dev/null 2>&1 || true; rm -rf "$TMP"' EXIT

make_ipa() {  # $1 = fichier, $2 = version
python3 - "$1" "$2" <<'EOF'
import plistlib, sys, zipfile
plist = {"CFBundleDisplayName": "SmokeApp", "CFBundleIdentifier": "com.smoke.app",
         "CFBundleShortVersionString": sys.argv[2], "CFBundleVersion": "1", "MinimumOSVersion": "15.0"}
with zipfile.ZipFile(sys.argv[1], "w") as z:
    z.writestr("Payload/Smoke.app/Info.plist", plistlib.dumps(plist))
EOF
}
make_ipa "$TMP/v1.ipa" 1.0
make_ipa "$TMP/v2.ipa" 2.0

docker run -d --name "$NAME" -p "127.0.0.1:$PORT:8000" \
  -e UPLOAD_TOKEN=smoke-token -e ADMIN_PASSWORD=smoke-pass -e DOMAIN=ipa.test "$IMAGE" >/dev/null

echo "Attente du serveur..."
for _ in $(seq 1 30); do
  curl -fs "$URL/health" >/dev/null 2>&1 && break
  sleep 1
done
curl -fs "$URL/health" | grep -q ok

echo "Catalogue public ouvert sans connexion"
curl -fs "$URL/" | grep -q "source.json"

echo "Panel admin ferme sans connexion (redirection vers la page de login)"
code=$(curl -s -o /dev/null -w "%{http_code}" "$URL/admin")
[ "$code" = "302" ]

echo "Envoi par l'API : refuse sans token, accepte avec"
code=$(curl -s -o /dev/null -w "%{http_code}" -F file=@"$TMP/v1.ipa" "$URL/api/upload")
[ "$code" = "403" ]
curl -fs -H "X-Token: smoke-token" -F file=@"$TMP/v1.ipa" "$URL/api/upload" >/dev/null

echo "Connexion au panel (mauvais puis bon mot de passe)"
JAR="$TMP/cookies"
csrf() { curl -fs -c "$JAR" -b "$JAR" "$URL/admin/login" | sed -n 's/.*name="csrf" value="\([^"]*\)".*/\1/p' | head -1; }
T="$(csrf)"
code=$(curl -s -o /dev/null -w "%{http_code}" -c "$JAR" -b "$JAR" -d "csrf=$T" -d "password=faux" "$URL/admin/login")
[ "$code" = "401" ]
T="$(csrf)"
code=$(curl -s -o /dev/null -w "%{http_code}" -c "$JAR" -b "$JAR" -d "csrf=$T" -d "password=smoke-pass" "$URL/admin/login")
[ "$code" = "302" ]
curl -fs -b "$JAR" "$URL/admin" | grep -q "Importer des IPA"

echo "Import d'une 2e version depuis le panel"
T="$(curl -fs -c "$JAR" -b "$JAR" "$URL/admin" | sed -n 's/.*name="csrf" value="\([^"]*\)".*/\1/p' | head -1)"
curl -fs -o /dev/null -b "$JAR" -F "csrf=$T" -F "file=@$TMP/v2.ipa" "$URL/admin/upload"

echo "Verification de source.json (local puis via le domaine)"
curl -fs "$URL/source.json" | python3 -c '
import json, sys
app = json.load(sys.stdin)["apps"][0]
assert app["bundleIdentifier"] == "com.smoke.app", app
assert [v["version"] for v in app["versions"]] == ["2.0", "1.0"], app["versions"]
assert app["downloadURL"].startswith("http://127.0.0.1:18000/dl/"), app["downloadURL"]
print("OK local :", app["name"], [v["version"] for v in app["versions"]])
'
curl -fs -H "Host: ipa.test" "$URL/source.json" | python3 -c '
import json, sys
url = json.load(sys.stdin)["apps"][0]["downloadURL"]
assert url.startswith("https://ipa.test/dl/"), url
print("OK domaine :", url)
'

echo "Telechargement public de l'IPA"
DL="$(curl -fs "$URL/source.json" | python3 -c 'import json,sys; print(json.load(sys.stdin)["apps"][0]["downloadURL"])')"
curl -fs -o "$TMP/dl.ipa" "$DL"
cmp "$TMP/dl.ipa" "$TMP/v2.ipa"

echo "Les donnees survivent a un redemarrage du conteneur"
docker restart "$NAME" >/dev/null
for _ in $(seq 1 30); do curl -fs "$URL/health" >/dev/null 2>&1 && break; sleep 1; done
curl -fs "$URL/source.json" | grep -q com.smoke.app

echo "Smoke test reussi"
