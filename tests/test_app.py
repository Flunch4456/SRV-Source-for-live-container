import io
import plistlib
import re
import zipfile

import pytest

from app.main import create_app

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 64
PASSWORD = "mot-de-passe-test"
TOKEN = "token-api-test"


def make_ipa(bundle="com.test.app", name="TestApp", version="1.0", build="1", min_os="15.0"):
    plist = {"CFBundleIdentifier": bundle, "CFBundleDisplayName": name,
             "CFBundleShortVersionString": version, "CFBundleVersion": build, "MinimumOSVersion": min_os}
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("Payload/Test.app/Info.plist", plistlib.dumps(plist))
        z.writestr("Payload/Test.app/bin", b"x" * 1000)
    return buf.getvalue()


@pytest.fixture
def env(tmp_path):
    return {"DATA_DIR": str(tmp_path), "UPLOAD_TOKEN": TOKEN, "ADMIN_PASSWORD": PASSWORD,
            "DOMAIN": "ipa.test", "SOURCE_NAME": "Test Source", "DEVELOPER_NAME": "Dev"}


@pytest.fixture
def app(env):
    return create_app(env)


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def admin(app):
    c = app.test_client()
    with c.session_transaction() as s:
        s["admin"] = True
        s["csrf"] = "tok"
    return c


def upload(c, data, filename="a.ipa", **extra):
    return c.post("/admin/upload", data={"csrf": "tok", "file": (io.BytesIO(data), filename), **extra},
                  content_type="multipart/form-data", follow_redirects=True)


# ---------------------------------------------------------------- public

def test_public_pages_open_without_login(client):
    assert client.get("/").status_code == 200
    assert client.get("/health").data == b"ok"
    assert client.get("/source.json").json["apps"] == []


def test_admin_requires_login(client):
    for path in ("/admin", "/admin/app/com.test.app"):
        r = client.get(path)
        assert r.status_code == 302 and "/admin/login" in r.headers["Location"]
    # un POST sans session admin est refusé (le jeton CSRF manque d'abord)
    assert client.post("/admin/upload").status_code in (400, 403)


def test_catalog_lists_and_downloads(admin, client):
    upload(admin, make_ipa())
    page = client.get("/").get_data(as_text=True)
    assert "TestApp" in page and "Télécharger" in page
    detail = client.get("/app/com.test.app").get_data(as_text=True)
    assert "1.0" in detail and "com.test.app" in detail
    src = client.get("/source.json").json["apps"][0]
    url = src["downloadURL"].replace("http://localhost", "")
    r = client.get(url)
    assert r.status_code == 200 and r.data == make_ipa() and "attachment" in r.headers["Content-Disposition"]
    # reprise de téléchargement (iOS)
    part = client.get(url, headers={"Range": "bytes=0-9"})
    assert part.status_code == 206 and len(part.data) == 10


def test_unknown_things_are_404(client):
    assert client.get("/app/com.nope").status_code == 404
    assert client.get("/dl/999/x.ipa").status_code == 404
    assert client.get("/icon/..%2F..%2Fsecret.key").status_code == 404
    assert client.get("/icon/secret.key").status_code == 404


# ----------------------------------------------------------------- login

def test_login_wrong_and_right_password(client):
    client.get("/admin/login")
    with client.session_transaction() as s:
        s["csrf"] = "tok"
    r = client.post("/admin/login", data={"csrf": "tok", "password": "faux"})
    assert r.status_code == 401 and client.get("/admin").status_code == 302
    r = client.post("/admin/login", data={"csrf": "tok", "password": PASSWORD})
    assert r.status_code == 302 and client.get("/admin").status_code == 200


def test_login_rate_limit(client):
    with client.session_transaction() as s:
        s["csrf"] = "tok"
    codes = [client.post("/admin/login", data={"csrf": "tok", "password": "x"}).status_code for _ in range(6)]
    assert codes[:5] == [401] * 5 and codes[5] == 429
    # même le bon mot de passe est refusé pendant le blocage
    assert client.post("/admin/login", data={"csrf": "tok", "password": PASSWORD}).status_code == 429


def test_login_disabled_without_password(tmp_path):
    c = create_app({"DATA_DIR": str(tmp_path)}).test_client()
    with c.session_transaction() as s:
        s["csrf"] = "tok"
    r = c.post("/admin/login", data={"csrf": "tok", "password": ""})
    assert r.status_code == 401 and "ADMIN_PASSWORD" in r.get_data(as_text=True)


def test_placeholder_password_is_refused(tmp_path):
    c = create_app({"DATA_DIR": str(tmp_path), "UPLOAD_TOKEN": "change-moi"}).test_client()
    with c.session_transaction() as s:
        s["csrf"] = "tok"
    assert c.post("/admin/login", data={"csrf": "tok", "password": "change-moi"}).status_code == 401


def test_password_falls_back_to_upload_token(tmp_path):
    c = create_app({"DATA_DIR": str(tmp_path), "UPLOAD_TOKEN": "ancien-token"}).test_client()
    with c.session_transaction() as s:
        s["csrf"] = "tok"
    assert c.post("/admin/login", data={"csrf": "tok", "password": "ancien-token"}).status_code == 302


def test_csrf_is_required(admin):
    r = admin.post("/admin/upload", data={"file": (io.BytesIO(make_ipa()), "a.ipa")},
                   content_type="multipart/form-data")
    assert r.status_code == 400
    assert admin.get("/source.json").json["apps"] == []


# ---------------------------------------------------------------- import

def test_upload_creates_app_and_version(admin):
    r = upload(admin, make_ipa(), notes="Première version")
    assert "TestApp 1.0 ajoutée" in r.get_data(as_text=True)
    app = admin.get("/source.json").json["apps"][0]
    assert app["name"] == "TestApp" and app["bundleIdentifier"] == "com.test.app"
    v = app["versions"][0]
    assert v["version"] == "1.0" and v["buildVersion"] == "1" and v["localizedDescription"] == "Première version"
    assert v["minOSVersion"] == "15.0" and len(v["sha256"]) == 64


def test_versions_are_sorted_newest_first(admin):
    for ver in ("1.9", "1.10", "1.2"):
        upload(admin, make_ipa(version=ver, build=ver))
    versions = [v["version"] for v in admin.get("/source.json").json["apps"][0]["versions"]]
    assert versions == ["1.10", "1.9", "1.2"]  # 1.10 > 1.9 (tri numérique, pas alphabétique)
    assert admin.get("/source.json").json["apps"][0]["version"] == "1.10"


def test_duplicate_version_is_rejected(admin):
    upload(admin, make_ipa())
    r = upload(admin, make_ipa())
    assert "existe déjà" in r.get_data(as_text=True)
    assert len(admin.get("/source.json").json["apps"][0]["versions"]) == 1


def test_invalid_files_are_rejected(admin):
    assert "pas un zip" in upload(admin, b"contenu quelconque").get_data(as_text=True)
    assert "ajoutée" not in upload(admin, b"").get_data(as_text=True)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("hello.txt", "x")
    assert "Info.plist introuvable" in upload(admin, buf.getvalue()).get_data(as_text=True)
    assert admin.get("/source.json").json["apps"] == []


def test_bad_bundle_id_is_rejected(admin):
    r = upload(admin, make_ipa(bundle="../../etc/passwd"))
    assert "bundle ID" in r.get_data(as_text=True)


def test_upload_from_app_page_checks_bundle(admin):
    upload(admin, make_ipa())
    r = upload(admin, make_ipa(bundle="com.autre", name="Autre"), bundle="com.test.app")
    assert "pas « com.test.app »" in r.get_data(as_text=True)
    r = upload(admin, make_ipa(version="2.0"), bundle="com.test.app")
    assert "TestApp 2.0 ajoutée" in r.get_data(as_text=True)


def test_ajax_upload_returns_204(admin):
    r = admin.post("/admin/upload", data={"csrf": "tok", "file": (io.BytesIO(make_ipa()), "a.ipa")},
                   content_type="multipart/form-data", headers={"X-Requested-With": "XMLHttpRequest"})
    assert r.status_code == 204
    assert "TestApp 1.0 ajoutée" in admin.get("/admin").get_data(as_text=True)  # message conservé


def test_api_upload_with_token(client):
    assert client.post("/api/upload", data={"file": (io.BytesIO(make_ipa()), "a.ipa")},
                       content_type="multipart/form-data").status_code == 403
    r = client.post("/api/upload", headers={"X-Token": "faux"}, data={"file": (io.BytesIO(make_ipa()), "a.ipa")},
                    content_type="multipart/form-data")
    assert r.status_code == 403
    r = client.post("/upload", headers={"X-Token": TOKEN}, data={"file": (io.BytesIO(make_ipa()), "a.ipa")},
                    content_type="multipart/form-data")
    assert r.status_code == 200 and r.json["ok"] is True
    r = client.post("/api/upload", headers={"X-Token": TOKEN}, data={"file": (io.BytesIO(make_ipa()), "a.ipa")},
                    content_type="multipart/form-data")
    assert r.status_code == 422 and "existe déjà" in r.json["results"][0]["error"]


def test_api_disabled_without_token(tmp_path):
    c = create_app({"DATA_DIR": str(tmp_path)}).test_client()
    assert c.post("/api/upload", headers={"X-Token": ""}, data={"file": (io.BytesIO(make_ipa()), "a.ipa")},
                  content_type="multipart/form-data").status_code == 403


# ------------------------------------------------------------ gestion

def test_edit_app_details_and_icon(admin, client):
    upload(admin, make_ipa())
    admin.post("/admin/app/com.test.app", data={"csrf": "tok", "name": "Nouveau nom", "subtitle": "Sous-titre",
                                               "description": "Une <b>description</b>", "developer": "Moi"})
    r = admin.post("/admin/app/com.test.app/icon", data={"csrf": "tok", "icon": (io.BytesIO(PNG), "i.png")},
                   content_type="multipart/form-data", follow_redirects=True)
    assert "Icône mise à jour" in r.get_data(as_text=True)
    app = client.get("/source.json").json["apps"][0]
    assert app["name"] == "Nouveau nom" and app["subtitle"] == "Sous-titre" and app["developerName"] == "Moi"
    assert app["iconURL"].startswith("http://localhost/icon/")
    assert client.get(app["iconURL"].replace("http://localhost", "")).data == PNG
    page = client.get("/app/com.test.app").get_data(as_text=True)
    assert "&lt;b&gt;description&lt;/b&gt;" in page and "<b>description</b>" not in page  # échappé


def test_icon_must_be_an_image(admin):
    upload(admin, make_ipa())
    r = admin.post("/admin/app/com.test.app/icon", data={"csrf": "tok", "icon": (io.BytesIO(b"<svg onload=x>"), "i.svg")},
                   content_type="multipart/form-data", follow_redirects=True)
    assert "PNG ou un JPEG" in r.get_data(as_text=True)
    assert admin.get("/source.json").json["apps"][0]["iconURL"] == ""


def test_edit_notes_and_delete_version(admin, env):
    upload(admin, make_ipa(version="1.0"))
    upload(admin, make_ipa(version="2.0"))
    vs = admin.get("/source.json").json["apps"][0]["versions"]
    old_id = int(re.search(r"/dl/(\d+)/", next(v for v in vs if v["version"] == "1.0")["downloadURL"]).group(1))
    admin.post(f"/admin/version/{old_id}/notes", data={"csrf": "tok", "notes": "Correctifs"})
    vs = admin.get("/source.json").json["apps"][0]["versions"]
    assert next(v for v in vs if v["version"] == "1.0")["localizedDescription"] == "Correctifs"

    from pathlib import Path
    before = len(list((Path(env["DATA_DIR"]) / "ipas").glob("*.ipa")))
    admin.post(f"/admin/version/{old_id}/delete", data={"csrf": "tok"})
    assert [v["version"] for v in admin.get("/source.json").json["apps"][0]["versions"]] == ["2.0"]
    assert len(list((Path(env["DATA_DIR"]) / "ipas").glob("*.ipa"))) == before - 1  # fichier effacé
    assert admin.get(f"/dl/{old_id}/x.ipa").status_code == 404


def test_app_without_versions_is_hidden_from_public(admin, client):
    upload(admin, make_ipa())
    vid = int(re.search(r"/dl/(\d+)/", admin.get("/source.json").json["apps"][0]["downloadURL"]).group(1))
    admin.post(f"/admin/version/{vid}/delete", data={"csrf": "tok"})
    assert client.get("/source.json").json["apps"] == []
    assert client.get("/app/com.test.app").status_code == 404
    assert admin.get("/admin/app/com.test.app").status_code == 200  # toujours gérable en admin


def test_delete_app_removes_everything(admin, env):
    from pathlib import Path
    upload(admin, make_ipa(version="1.0"))
    upload(admin, make_ipa(version="2.0"))
    admin.post("/admin/app/com.test.app/icon", data={"csrf": "tok", "icon": (io.BytesIO(PNG), "i.png")},
               content_type="multipart/form-data")
    admin.post("/admin/app/com.test.app/delete", data={"csrf": "tok"})
    assert admin.get("/source.json").json["apps"] == []
    assert not list((Path(env["DATA_DIR"]) / "ipas").glob("*"))
    assert not list((Path(env["DATA_DIR"]) / "icons").glob("*"))


# ------------------------------------------------ liens et migration

def test_links_use_domain_or_local_address(admin):
    upload(admin, make_ipa())
    local = admin.get("/source.json").json["apps"][0]["downloadURL"]
    assert local.startswith("http://localhost/dl/")
    via = admin.get("/source.json", headers={"Host": "ipa.test"}).json["apps"][0]["downloadURL"]
    assert via.startswith("https://ipa.test/dl/")
    proxied = admin.get("/source.json", headers={"X-Forwarded-Host": "ipa.test", "X-Forwarded-Proto": "https"})
    assert proxied.json["apps"][0]["downloadURL"].startswith("https://ipa.test/dl/")
    other = admin.get("/source.json", headers={"Host": "192.168.1.5:8000"}).json["apps"][0]["downloadURL"]
    assert other.startswith("http://192.168.1.5:8000/dl/")


def test_legacy_flat_ipas_are_imported(env, tmp_path):
    from pathlib import Path
    ipas = Path(env["DATA_DIR"]) / "ipas"
    ipas.mkdir(parents=True)
    (ipas / "MonApp.ipa").write_bytes(make_ipa(name="MonApp", bundle="com.mon.app"))
    (ipas / "MonApp.png").write_bytes(PNG)
    (ipas / "casse.ipa").write_bytes(b"corrompu")
    c = create_app(env).test_client()
    app = c.get("/source.json").json["apps"][0]
    assert app["name"] == "MonApp" and app["iconURL"]
    assert not (ipas / "MonApp.ipa").exists()  # déplacée dans le nouveau rangement
    create_app(env)  # un second démarrage ne duplique rien
    assert len(c.get("/source.json").json["apps"][0]["versions"]) == 1


def test_security_headers(client):
    r = client.get("/")
    assert r.headers["X-Content-Type-Options"] == "nosniff" and r.headers["X-Frame-Options"] == "DENY"
    assert "default-src 'self'" in r.headers["Content-Security-Policy"]
    assert client.get("/admin/login").headers["Cache-Control"] == "no-store"
