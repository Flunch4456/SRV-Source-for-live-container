import hmac
import os
import plistlib
import re
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from flask import (Flask, abort, jsonify, redirect, render_template_string,
                   request, send_from_directory)
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.utils import secure_filename

DATA = Path(os.environ.get("DATA_DIR", "/data"))
IPAS = DATA / "ipas"
IPAS.mkdir(parents=True, exist_ok=True)

TOKEN = os.environ.get("UPLOAD_TOKEN", "")
# Nom de domaine public (ex: ipa.exemple.fr), sans https:// ni slash. Optionnel.
DOMAIN = (os.environ.get("DOMAIN", "").strip().lower()
          .removeprefix("https://").removeprefix("http://").strip("/"))
SOURCE_NAME = os.environ.get("SOURCE_NAME", "Ma source IPA")
DEV_NAME = os.environ.get("DEVELOPER_NAME", "Moi")
MAX_MB = int(os.environ.get("MAX_UPLOAD_MB", "2048"))

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = MAX_MB * 1024 * 1024
# Derriere un reverse proxy (Caddy / Cloudflare Tunnel) : on fait confiance aux en-tetes X-Forwarded-*
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

_cache = {}  # {nom_fichier: (mtime, infos)}
PLIST_RE = re.compile(r"^Payload/[^/]+\.app/Info\.plist$")


def base_url():
    """Adresse de base utilisee dans les liens de source.json.

    - Visite par le domaine (DOMAIN) -> https://DOMAIN
    - Visite en local (http://192.168.x.x:8000, etc.) -> on reprend l'adresse utilisee,
      donc les liens marchent aussi sur le reseau local.
    """
    host = request.host.split(":")[0].lower()
    if DOMAIN and host == DOMAIN:
        return f"https://{DOMAIN}"
    return request.url_root.rstrip("/")


def read_ipa(path: Path):
    """Lit l'Info.plist d'une IPA (une IPA est juste un zip). Resultat mis en cache."""
    mtime = path.stat().st_mtime
    cached = _cache.get(path.name)
    if cached and cached[0] == mtime:
        return cached[1]
    info = None
    try:
        with zipfile.ZipFile(path) as z:
            name = next((n for n in z.namelist() if PLIST_RE.match(n)), None)
            if name:
                p = plistlib.loads(z.read(name))
                info = {
                    "name": p.get("CFBundleDisplayName") or p.get("CFBundleName") or path.stem,
                    "bundle": p.get("CFBundleIdentifier", "unknown.bundle"),
                    "version": str(p.get("CFBundleShortVersionString") or p.get("CFBundleVersion") or "1.0"),
                    "min_os": p.get("MinimumOSVersion", "14.0"),
                }
    except (zipfile.BadZipFile, plistlib.InvalidFileException, KeyError):
        info = None
    _cache[path.name] = (mtime, info)
    return info


def list_ipas():
    out = []
    for f in sorted(IPAS.glob("*.ipa")):
        info = read_ipa(f)
        if info:
            out.append((f, info))
    return out


@app.get("/source.json")
def source():
    root = base_url()
    apps = {}
    for f, info in sorted(list_ipas(), key=lambda t: t[0].stat().st_mtime, reverse=True):
        st = f.stat()
        icon = IPAS / (f.stem + ".png")
        entry = {
            "version": info["version"],
            "date": datetime.fromtimestamp(st.st_mtime, timezone.utc).strftime("%Y-%m-%d"),
            "size": st.st_size,
            "downloadURL": f"{root}/ipa/{f.name}",
            "minOSVersion": info["min_os"],
        }
        a = apps.setdefault(info["bundle"], {
            "name": info["name"],
            "bundleIdentifier": info["bundle"],
            "developerName": DEV_NAME,
            "subtitle": "",
            "localizedDescription": f"{info['name']} (IPA perso)",
            "iconURL": f"{root}/ipa/{icon.name}" if icon.exists() else "",
            "tintColor": "3b82f6",
            "screenshotURLs": [],
            "versions": [],
        })
        a["versions"].append(entry)
    result = []
    for a in apps.values():
        latest = a["versions"][0]  # le plus recent (tri par date decroissante)
        # champs "legacy" gardes pour compatibilite avec les anciens clients
        a.update({
            "version": latest["version"],
            "versionDate": latest["date"],
            "size": latest["size"],
            "downloadURL": latest["downloadURL"],
        })
        result.append(a)
    return jsonify({
        "name": SOURCE_NAME,
        "identifier": "self.hosted.ipa.source",
        "sourceURL": f"{root}/source.json",
        "apps": result,
        "news": [],
    })


@app.get("/ipa/<path:name>")
def download(name):
    if not name.lower().endswith((".ipa", ".png")):
        abort(404)
    return send_from_directory(IPAS, name, as_attachment=name.lower().endswith(".ipa"))


def check_token():
    given = request.headers.get("X-Token") or request.form.get("token", "")
    if not TOKEN or not hmac.compare_digest(given, TOKEN):
        abort(403)


@app.post("/upload")
def upload():
    check_token()
    files = request.files.getlist("file")
    if not files:
        abort(400)
    for f in files:
        name = secure_filename(f.filename or "")
        if not name.lower().endswith((".ipa", ".png")):
            abort(400, "Seuls .ipa et .png sont acceptes")
        f.save(IPAS / name)
    if request.headers.get("X-Token"):
        return jsonify(ok=True)
    return redirect("/")


@app.post("/delete/<name>")
def delete(name):
    check_token()
    target = IPAS / secure_filename(name)
    if target.exists():
        target.unlink()
        _cache.pop(target.name, None)
    return redirect("/") if not request.headers.get("X-Token") else jsonify(ok=True)


PAGE = """<!doctype html><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>{{ name }}</title>
<style>body{font:16px system-ui;max-width:720px;margin:2rem auto;padding:0 1rem}
code{background:#8883;padding:2px 6px;border-radius:4px;word-break:break-all}
li{margin:.4rem 0}input,button{font:inherit;padding:.4rem}</style>
<h1>{{ name }}</h1>
<p>URL de la source a ajouter dans LiveContainer :</p>
<p><code>{{ root }}/source.json</code></p>
{% if domain_url and domain_url != root %}<p>Via ton domaine : <code>{{ domain_url }}/source.json</code></p>{% endif %}
<h2>Apps ({{ apps|length }})</h2>
<ul>{% for f, i in apps %}<li><b>{{ i.name }}</b> {{ i.version }} <small>({{ i.bundle }}, {{ (f.stat().st_size/1048576)|round(1) }} Mo)</small>
 — <a href="/ipa/{{ f.name }}">telecharger</a></li>{% else %}<li>Aucune IPA pour l'instant.</li>{% endfor %}</ul>
<h2>Envoyer une IPA</h2>
<form method=post action=/upload enctype=multipart/form-data>
<p><input type=password name=token placeholder="Token d'upload" required></p>
<p><input type=file name=file accept=".ipa,.png" multiple required></p>
<p><button>Envoyer</button></p></form>
<p><small>Astuce : une image <code>MonApp.png</code> a cote de <code>MonApp.ipa</code> devient son icone.</small></p>"""


@app.get("/")
def index():
    return render_template_string(PAGE, name=SOURCE_NAME, root=base_url(), apps=list_ipas(),
                                  domain_url=f"https://{DOMAIN}" if DOMAIN else "")


@app.get("/health")
def health():
    return "ok"
