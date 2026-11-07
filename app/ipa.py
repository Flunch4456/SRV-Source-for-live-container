"""Lecture des métadonnées d'une IPA.

Une IPA est un simple zip qui contient Payload/<App>.app/Info.plist.
On ne lit que ce fichier : l'IPA n'est jamais décompressée en entier.
"""
import hashlib
import plistlib
import re
import zipfile

PLIST_RE = re.compile(r"^Payload/[^/]+\.app/Info\.plist$")
BUNDLE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,199}$")
MAX_PLIST = 2 * 1024 * 1024


class IpaError(ValueError):
    """IPA invalide (le message est affiché tel quel à l'utilisateur)."""


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def read_info(path):
    try:
        with zipfile.ZipFile(path) as z:
            names = [n for n in z.namelist() if PLIST_RE.match(n)]
            if len(names) != 1:
                raise IpaError("Pas une IPA valide : Payload/<App>.app/Info.plist introuvable")
            entry = z.getinfo(names[0])
            if entry.file_size > MAX_PLIST:
                raise IpaError("Info.plist anormalement gros")
            data = z.read(entry)
    except IpaError:
        raise
    except zipfile.BadZipFile:
        raise IpaError("Fichier corrompu : ce n'est pas un zip") from None
    except Exception as e:  # lecture impossible (disque, zip tronqué...)
        raise IpaError(f"Lecture impossible : {e}") from None

    try:
        plist = plistlib.loads(data)
    except Exception:
        raise IpaError("Info.plist illisible") from None
    if not isinstance(plist, dict):
        raise IpaError("Info.plist illisible")

    bundle = str(plist.get("CFBundleIdentifier", "")).strip()
    if not BUNDLE_RE.match(bundle):
        raise IpaError("Identifiant de l'app (bundle ID) manquant ou invalide")

    short = str(plist.get("CFBundleShortVersionString") or "").strip()
    build = str(plist.get("CFBundleVersion") or "").strip()
    version = short or build or "1.0"
    if build == version:
        build = ""  # évite d'afficher « 1.0 (1.0) »
    name = str(plist.get("CFBundleDisplayName") or plist.get("CFBundleName") or bundle).strip()
    return {
        "bundle": bundle,
        "name": name[:100] or bundle,
        "version": version[:64],
        "build": build[:64],
        "min_os": str(plist.get("MinimumOSVersion") or "14.0")[:16],
    }
