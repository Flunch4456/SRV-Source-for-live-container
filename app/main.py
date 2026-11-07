"""Serveur de source IPA pour LiveContainer.

- Public  : catalogue (/), page d'une app, téléchargements, /source.json
- Privé   : panel admin (/admin) protégé par mot de passe, et API d'envoi par token
- Données : base SQLite + fichiers dans DATA_DIR (volume Docker)
"""
import hmac
import os
import re
import secrets
import shutil
import sqlite3
import time
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path

from flask import (Flask, abort, current_app, flash, g, jsonify, redirect,
                   render_template, request, send_file, send_from_directory,
                   session, url_for)
from werkzeug.exceptions import RequestEntityTooLarge
from werkzeug.middleware.proxy_fix import ProxyFix

from .ipa import BUNDLE_RE, IpaError, read_info, sha256_file

PLACEHOLDERS = {"", "change-moi", "changeme"}
SCHEMA = """
CREATE TABLE IF NOT EXISTS apps(
  bundle      TEXT PRIMARY KEY,
  name        TEXT NOT NULL,
  subtitle    TEXT NOT NULL DEFAULT '',
  description TEXT NOT NULL DEFAULT '',
  developer   TEXT NOT NULL DEFAULT '',
  icon        TEXT,
  created_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS versions(
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  bundle      TEXT NOT NULL REFERENCES apps(bundle) ON DELETE CASCADE,
  version     TEXT NOT NULL,
  build       TEXT NOT NULL DEFAULT '',
  min_os      TEXT NOT NULL DEFAULT '14.0',
  size        INTEGER NOT NULL,
  sha256      TEXT NOT NULL,
  filename    TEXT NOT NULL,
  notes       TEXT NOT NULL DEFAULT '',
  uploaded_at TEXT NOT NULL,
  UNIQUE(bundle, version, build)
);
"""
ICON_MAGIC = {b"\x89PNG\r\n\x1a\n": "png", b"\xff\xd8\xff": "jpg"}
ICON_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+\.(png|jpg)$")
STORED_IPA_RE = re.compile(r"^[0-9a-f]{24}\.ipa$")


class Conflict(ValueError):
    """Cette version existe déjà."""


def load_config(env=None):
    env = os.environ if env is None else env
    domain = (env.get("DOMAIN", "").strip().lower()
              .removeprefix("https://").removeprefix("http://").strip("/"))
    token = env.get("UPLOAD_TOKEN", "").strip()
    # Le mot de passe du panel = ADMIN_PASSWORD, ou à défaut l'ancien UPLOAD_TOKEN
    password = env.get("ADMIN_PASSWORD", "").strip() or token
    return {
        "DATA_DIR": Path(env.get("DATA_DIR", "/data")),
        "DOMAIN": domain,
        "API_TOKEN": "" if token in PLACEHOLDERS else token,
        "ADMIN_PASSWORD": "" if password in PLACEHOLDERS else password,
        "SOURCE_NAME": env.get("SOURCE_NAME", "Ma source IPA"),
        "DEVELOPER_NAME": env.get("DEVELOPER_NAME", ""),
        "MAX_UPLOAD_MB": int(env.get("MAX_UPLOAD_MB", "2048")),
        "SECRET_KEY": env.get("SECRET_KEY", ""),
    }


# ---------------------------------------------------------------- utilitaires

def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def db():
    if "db" not in g:
        con = sqlite3.connect(current_app.config["DB_PATH"], timeout=15)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA foreign_keys=ON")
        g.db = con
    return g.db


def vkey(v):
    """Clé de tri : 1.10 > 1.9, puis le build, puis la date d'envoi."""
    nums = lambda s: tuple(int(x) for x in re.findall(r"\d+", s)[:6])  # noqa: E731
    return nums(v["version"]), nums(v["build"]), v["uploaded_at"]


def load_catalog(only_published=True):
    con = db()
    versions = {}
    for v in con.execute("SELECT * FROM versions"):
        versions.setdefault(v["bundle"], []).append(dict(v))
    items = []
    for a in con.execute("SELECT * FROM apps ORDER BY name COLLATE NOCASE"):
        vs = sorted(versions.get(a["bundle"], []), key=vkey, reverse=True)
        if only_published and not vs:
            continue
        items.append({"app": dict(a), "versions": vs, "latest": vs[0] if vs else None})
    return items


def get_app_or_404(bundle):
    if not BUNDLE_RE.match(bundle):
        abort(404)
    a = db().execute("SELECT * FROM apps WHERE bundle=?", (bundle,)).fetchone()
    if not a:
        abort(404)
    return dict(a)


def slug(text):
    return re.sub(r"[^A-Za-z0-9._-]+", "-", text).strip("-") or "app"


def dl_name(app_name, version):
    return slug(f"{app_name}-{version}") + ".ipa"


def base_url():
    """Base des liens de téléchargement.

    Via le domaine (DOMAIN) -> https://DOMAIN ; en local -> l'adresse utilisée (http://IP:8000).
    """
    domain = current_app.config["CFG"]["DOMAIN"]
    host = request.host.split(":")[0].lower()
    if domain and host == domain:
        return f"https://{domain}"
    return request.url_root.rstrip("/")


def dl_url(v, app_name, absolute=False):
    path = url_for("download", vid=v["id"], name=dl_name(app_name, v["version"]))
    return base_url() + path if absolute else path


def paths():
    d = current_app.config["CFG"]["DATA_DIR"]
    return d / "ipas", d / "icons", d / "tmp"


def remove_file(path):
    try:
        Path(path).unlink(missing_ok=True)
    except OSError:
        pass


# ------------------------------------------------------------------- import

def ingest(path, notes="", expect_bundle=None):
    """Range une IPA (déjà sur le disque) dans la base. Déplace le fichier."""
    ipas, _, _ = paths()
    info = read_info(path)
    if expect_bundle and info["bundle"] != expect_bundle:
        raise IpaError(f"Cette IPA est « {info['bundle']} », pas « {expect_bundle} »")
    build_txt = f" ({info['build']})" if info["build"] else ""
    exists = Conflict(f"{info['name']} {info['version']}{build_txt} existe déjà : "
                      "supprime-la d'abord si tu veux la remplacer")
    con = db()
    if con.execute("SELECT 1 FROM versions WHERE bundle=? AND version=? AND build=?",
                   (info["bundle"], info["version"], info["build"])).fetchone():
        raise exists

    fname = secrets.token_hex(12) + ".ipa"
    size, sha = Path(path).stat().st_size, sha256_file(path)
    shutil.move(str(path), ipas / fname)
    try:
        with con:
            con.execute("INSERT OR IGNORE INTO apps(bundle,name,developer,created_at) VALUES(?,?,?,?)",
                        (info["bundle"], info["name"], current_app.config["CFG"]["DEVELOPER_NAME"], now_iso()))
            cur = con.execute(
                "INSERT INTO versions(bundle,version,build,min_os,size,sha256,filename,notes,uploaded_at)"
                " VALUES(?,?,?,?,?,?,?,?,?)",
                (info["bundle"], info["version"], info["build"], info["min_os"],
                 size, sha, fname, notes, now_iso()))
    except sqlite3.IntegrityError:
        remove_file(ipas / fname)
        raise exists from None
    except sqlite3.Error:
        remove_file(ipas / fname)
        raise
    return {**info, "id": cur.lastrowid}


def save_upload(file_storage, notes="", expect_bundle=None):
    tmp = paths()[2] / (secrets.token_hex(8) + ".part")
    file_storage.save(tmp)
    try:
        return ingest(tmp, notes, expect_bundle)
    finally:
        remove_file(tmp)


def import_legacy():
    """Reprend les IPA déposées à plat dans data/ipas/ (ancienne version du serveur)."""
    ipas, icons, _ = paths()
    for f in sorted(ipas.glob("*.ipa")):
        if STORED_IPA_RE.match(f.name):
            continue
        try:
            v = ingest(f)
        except (IpaError, Conflict) as e:
            print(f"[import] {f.name} ignorée : {e}", flush=True)
            continue
        icon_src = f.with_suffix(".png")
        if icon_src.exists():
            name = f"{v['bundle']}-{secrets.token_hex(4)}.png"
            shutil.move(str(icon_src), icons / name)
            with db() as con:
                con.execute("UPDATE apps SET icon=? WHERE bundle=? AND icon IS NULL", (name, v["bundle"]))
        print(f"[import] {f.name} -> {v['name']} {v['version']}", flush=True)


# ------------------------------------------------------------------ fabrique

def create_app(env=None):
    cfg = load_config(env)
    data = cfg["DATA_DIR"]
    for sub in ("ipas", "icons", "tmp"):
        (data / sub).mkdir(parents=True, exist_ok=True)
    for stale in (data / "tmp").glob("*"):  # restes d'envois interrompus
        remove_file(stale)

    app = Flask(__name__)
    app.config.update(
        CFG=cfg,
        DB_PATH=str(data / "db.sqlite"),
        MAX_CONTENT_LENGTH=cfg["MAX_UPLOAD_MB"] * 1024 * 1024,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        PERMANENT_SESSION_LIFETIME=12 * 3600,
    )
    key_file = data / "secret.key"
    if not cfg["SECRET_KEY"] and not key_file.exists():
        key_file.write_text(secrets.token_hex(32))
        key_file.chmod(0o600)
    app.secret_key = cfg["SECRET_KEY"] or key_file.read_text().strip()
    login_fails = {}  # ip -> horodatages des échecs de connexion récents
    # Derrière Zoraxy / Cloudflare : on fait confiance aux en-têtes X-Forwarded-*
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

    con = sqlite3.connect(app.config["DB_PATH"], timeout=15)
    con.execute("PRAGMA journal_mode=WAL")
    con.executescript(SCHEMA)
    con.close()
    with app.app_context():
        import_legacy()

    # ------------------------------------------------------------ gabarits
    def csrf_token():
        if "csrf" not in session:
            session["csrf"] = secrets.token_hex(16)
        return session["csrf"]

    def filesize(n):
        n = float(n)
        for unit in ("o", "Ko", "Mo", "Go"):
            if n < 1024 or unit == "Go":
                return f"{n:.0f} {unit}" if unit == "o" else f"{n:.1f} {unit}"
            n /= 1024

    def fdate(s):
        return f"{s[8:10]}/{s[5:7]}/{s[0:4]}" if s else ""

    app.jinja_env.globals.update(csrf_token=csrf_token, dl_url=dl_url)
    app.jinja_env.filters.update(filesize=filesize, fdate=fdate)

    @app.context_processor
    def common():
        return {"source_name": cfg["SOURCE_NAME"]}

    @app.teardown_appcontext
    def close_db(_exc):
        con = g.pop("db", None)
        if con is not None:
            con.close()

    @app.after_request
    def security_headers(r):
        r.headers.setdefault("X-Content-Type-Options", "nosniff")
        r.headers.setdefault("X-Frame-Options", "DENY")
        r.headers.setdefault("Referrer-Policy", "same-origin")
        r.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'")
        if request.path.startswith("/admin"):
            r.headers["Cache-Control"] = "no-store"
        return r

    @app.errorhandler(RequestEntityTooLarge)
    def too_large(_e):
        return f"Fichier trop gros (maximum {cfg['MAX_UPLOAD_MB']} Mo).", 413

    @app.before_request
    def csrf_protect():
        """Toutes les actions POST du panel exigent le jeton CSRF de la session."""
        if request.method == "POST" and request.path.startswith("/admin"):
            sent = request.form.get("csrf", "")
            if not sent or not hmac.compare_digest(sent, session.get("csrf", "")):
                abort(400, "Session expirée : recharge la page et réessaie.")

    def admin_required(f):
        @wraps(f)
        def wrapper(*a, **kw):
            if not session.get("admin"):
                if request.method == "GET":
                    return redirect(url_for("login"))
                abort(403)
            return f(*a, **kw)
        return wrapper

    # -------------------------------------------------------------- public
    @app.get("/")
    def index():
        return render_template("index.html", items=load_catalog(), root=base_url())

    @app.get("/app/<bundle>")
    def app_page(bundle):
        a = get_app_or_404(bundle)
        item = next((i for i in load_catalog() if i["app"]["bundle"] == a["bundle"]), None)
        if not item:
            abort(404)
        return render_template("app.html", item=item, root=base_url())

    @app.get("/source.json")
    def source():
        root = base_url()
        apps = []
        for item in load_catalog():
            a, versions = item["app"], item["versions"]
            entries = []
            for v in versions:
                e = {"version": v["version"], "date": v["uploaded_at"][:10], "size": v["size"],
                     "downloadURL": dl_url(v, a["name"], absolute=True),
                     "minOSVersion": v["min_os"], "sha256": v["sha256"]}
                if v["build"]:
                    e["buildVersion"] = v["build"]
                if v["notes"]:
                    e["localizedDescription"] = v["notes"]
                entries.append(e)
            latest = entries[0]
            apps.append({
                "name": a["name"], "bundleIdentifier": a["bundle"],
                "developerName": a["developer"] or cfg["DEVELOPER_NAME"],
                "subtitle": a["subtitle"],
                "localizedDescription": a["description"] or a["name"],
                "iconURL": f"{root}/icon/{a['icon']}" if a["icon"] else "",
                "tintColor": "3b82f6", "screenshotURLs": [],
                "versions": entries,
                # champs « anciens » pour les clients qui ne lisent pas versions[]
                "version": latest["version"], "versionDate": latest["date"],
                "size": latest["size"], "downloadURL": latest["downloadURL"],
            })
        resp = jsonify({"name": cfg["SOURCE_NAME"], "identifier": "self.hosted.ipa.source",
                        "sourceURL": f"{root}/source.json", "apps": apps, "news": []})
        resp.headers["Cache-Control"] = "no-cache"
        return resp

    @app.get("/dl/<int:vid>/<name>")
    def download(vid, name):
        v = db().execute("SELECT v.filename, v.version, a.name AS app_name FROM versions v "
                         "JOIN apps a USING(bundle) WHERE v.id=?", (vid,)).fetchone()
        path = paths()[0] / v["filename"] if v else None
        if not v or not path.is_file():
            abort(404)
        return send_file(path, mimetype="application/octet-stream", as_attachment=True,
                         download_name=dl_name(v["app_name"], v["version"]),
                         conditional=True, max_age=3600)

    @app.get("/icon/<name>")
    def icon(name):
        if not ICON_NAME_RE.match(name):
            abort(404)
        return send_from_directory(paths()[1], name, max_age=86400)

    @app.get("/health")
    def health():
        return "ok"

    # --------------------------------------------------------------- login
    @app.route("/admin/login", methods=["GET", "POST"])
    def login():
        error = None
        if request.method == "POST":
            ip, now = request.remote_addr or "?", time.time()
            recent = [t for t in login_fails.get(ip, []) if now - t < 600]
            login_fails[ip] = recent
            given = request.form.get("password", "")
            if not cfg["ADMIN_PASSWORD"]:
                error = "Panel désactivé : définis ADMIN_PASSWORD dans le fichier .env."
            elif len(recent) >= 5:
                return render_template("login.html", error="Trop d'essais : réessaie dans 10 minutes."), 429
            elif hmac.compare_digest(given.encode(), cfg["ADMIN_PASSWORD"].encode()):
                login_fails.pop(ip, None)
                session.clear()
                session["admin"] = True
                session.permanent = True
                return redirect(url_for("dashboard"))
            else:
                recent.append(now)
                time.sleep(0.5)
                error = "Mot de passe incorrect."
        return render_template("login.html", error=error), (401 if error else 200)

    @app.post("/admin/logout")
    def logout():
        session.clear()
        return redirect(url_for("index"))

    # --------------------------------------------------------------- admin
    @app.get("/admin")
    @admin_required
    def dashboard():
        items = load_catalog(only_published=False)
        stats = {
            "apps": len(items),
            "versions": sum(len(i["versions"]) for i in items),
            "size": sum(v["size"] for i in items for v in i["versions"]),
            "free": shutil.disk_usage(cfg["DATA_DIR"]).free,
        }
        via_domain = bool(cfg["DOMAIN"]) and request.host.split(":")[0].lower() == cfg["DOMAIN"]
        return render_template("admin.html", items=items, stats=stats, via_domain=via_domain,
                               max_mb=cfg["MAX_UPLOAD_MB"])

    @app.post("/admin/upload")
    @admin_required
    def admin_upload():
        files = [f for f in request.files.getlist("file") if f.filename]
        bundle = request.form.get("bundle", "")
        back = url_for("admin_app", bundle=bundle) if bundle and BUNDLE_RE.match(bundle) else url_for("dashboard")
        if not files:
            flash("Aucun fichier choisi.", "err")
        notes = request.form.get("notes", "").strip()[:2000]
        for f in files:
            try:
                v = save_upload(f, notes, expect_bundle=bundle or None)
                flash(f"{v['name']} {v['version']} ajoutée.", "ok")
            except (IpaError, Conflict) as e:
                flash(f"{f.filename} : {e}", "err")
        if request.headers.get("X-Requested-With") == "XMLHttpRequest":
            return "", 204  # le JS recharge la page, les messages restent dans la session
        return redirect(back)

    @app.get("/admin/app/<bundle>")
    @admin_required
    def admin_app(bundle):
        a = get_app_or_404(bundle)
        item = next(i for i in load_catalog(only_published=False) if i["app"]["bundle"] == a["bundle"])
        return render_template("admin_app.html", item=item, root=base_url(), max_mb=cfg["MAX_UPLOAD_MB"])

    @app.post("/admin/app/<bundle>")
    @admin_required
    def admin_app_update(bundle):
        a = get_app_or_404(bundle)
        f = request.form
        name = f.get("name", "").strip()[:100]
        if not name:
            flash("Le nom ne peut pas être vide.", "err")
        else:
            with db() as con:
                con.execute("UPDATE apps SET name=?, subtitle=?, description=?, developer=? WHERE bundle=?",
                            (name, f.get("subtitle", "").strip()[:120], f.get("description", "").strip()[:2000],
                             f.get("developer", "").strip()[:100], a["bundle"]))
            flash("Informations enregistrées.", "ok")
        return redirect(url_for("admin_app", bundle=bundle))

    @app.post("/admin/app/<bundle>/icon")
    @admin_required
    def admin_icon(bundle):
        a = get_app_or_404(bundle)
        f = request.files.get("icon")
        data = f.read(2 * 1024 * 1024 + 1) if f else b""
        ext = next((e for magic, e in ICON_MAGIC.items() if data.startswith(magic)), None)
        if not data or len(data) > 2 * 1024 * 1024:
            flash("Choisis une image de 2 Mo maximum.", "err")
        elif not ext:
            flash("L'icône doit être un PNG ou un JPEG classique.", "err")
        else:
            name = f"{a['bundle']}-{secrets.token_hex(4)}.{ext}"
            (paths()[1] / name).write_bytes(data)
            if a["icon"]:
                remove_file(paths()[1] / a["icon"])
            with db() as con:
                con.execute("UPDATE apps SET icon=? WHERE bundle=?", (name, a["bundle"]))
            flash("Icône mise à jour.", "ok")
        return redirect(url_for("admin_app", bundle=bundle))

    @app.post("/admin/app/<bundle>/icon/delete")
    @admin_required
    def admin_icon_delete(bundle):
        a = get_app_or_404(bundle)
        if a["icon"]:
            remove_file(paths()[1] / a["icon"])
            with db() as con:
                con.execute("UPDATE apps SET icon=NULL WHERE bundle=?", (a["bundle"],))
        return redirect(url_for("admin_app", bundle=bundle))

    @app.post("/admin/app/<bundle>/delete")
    @admin_required
    def admin_app_delete(bundle):
        a = get_app_or_404(bundle)
        con = db()
        files = [r["filename"] for r in con.execute("SELECT filename FROM versions WHERE bundle=?", (a["bundle"],))]
        with con:
            con.execute("DELETE FROM apps WHERE bundle=?", (a["bundle"],))
        for fn in files:
            remove_file(paths()[0] / fn)
        if a["icon"]:
            remove_file(paths()[1] / a["icon"])
        flash(f"{a['name']} supprimée avec toutes ses versions.", "ok")
        return redirect(url_for("dashboard"))

    def get_version_or_404(vid):
        v = db().execute("SELECT * FROM versions WHERE id=?", (vid,)).fetchone()
        if not v:
            abort(404)
        return dict(v)

    @app.post("/admin/version/<int:vid>/notes")
    @admin_required
    def admin_version_notes(vid):
        v = get_version_or_404(vid)
        with db() as con:
            con.execute("UPDATE versions SET notes=? WHERE id=?", (request.form.get("notes", "").strip()[:2000], vid))
        flash("Notes enregistrées.", "ok")
        return redirect(url_for("admin_app", bundle=v["bundle"]))

    @app.post("/admin/version/<int:vid>/delete")
    @admin_required
    def admin_version_delete(vid):
        v = get_version_or_404(vid)
        with db() as con:
            con.execute("DELETE FROM versions WHERE id=?", (vid,))
        remove_file(paths()[0] / v["filename"])
        flash(f"Version {v['version']} supprimée.", "ok")
        return redirect(url_for("admin_app", bundle=v["bundle"]))

    # ------------------------------------------------- API d'envoi (curl)
    @app.post("/api/upload")
    @app.post("/upload")
    def api_upload():
        token = request.headers.get("X-Token", "")
        if not cfg["API_TOKEN"] or not hmac.compare_digest(token.encode(), cfg["API_TOKEN"].encode()):
            abort(403)
        files = [f for f in request.files.getlist("file") if f.filename]
        if not files:
            abort(400, "Aucun fichier (champ « file »)")
        results = []
        for f in files:
            try:
                v = save_upload(f, request.form.get("notes", "").strip()[:2000])
                results.append({"file": f.filename, "ok": True, "bundle": v["bundle"], "version": v["version"]})
            except (IpaError, Conflict) as e:
                results.append({"file": f.filename, "ok": False, "error": str(e)})
        ok = all(r["ok"] for r in results)
        return jsonify(ok=ok, results=results), (200 if ok else 422)

    return app


app = create_app() if os.environ.get("DATA_DIR") or os.path.isdir("/data") else None
