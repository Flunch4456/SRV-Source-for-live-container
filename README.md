# Serveur de source IPA pour LiveContainer

Un petit serveur auto-hébergé : tu importes tes IPA dans un **panel admin privé**, et elles sont rangées par app et par version dans une base de données. Le **catalogue public** liste les apps et permet de les installer à distance, via une **source au format AltStore** (`/source.json`) que LiveContainer sait lire.

| | Accès |
|---|---|
| Catalogue (`/`), page d'une app, téléchargements, `/source.json` | **Public** |
| Panel admin (`/admin`) : import, versions, infos, icônes, suppression | **Privé** (mot de passe) |
| Envoi en ligne de commande (`/api/upload`) | **Privé** (token) |

## Fonctionnement

- Tu envoies un `.ipa` dans le panel. Le serveur l'ouvre (c'est un zip), lit son `Info.plist` et en tire le nom, le bundle ID et la version.
- Les apps sont regroupées par bundle ID, avec toutes leurs versions (la plus récente en premier, `1.10` après `1.9`).
- Tout vit dans `data/` : `db.sqlite` (la base), `ipas/` (les fichiers), `icons/`. **Sauvegarde ce dossier** pour ne rien perdre.
- Une version déjà présente est refusée (supprime-la d'abord pour la remplacer).

## Installation sur une VM ou un LXC Debian 13

> Dans un **LXC Proxmox**, active d'abord Options → Features → **Nesting** (et **keyctl** si le conteneur est unprivileged), puis redémarre-le. Sans ça, Docker ne démarre pas correctement.

```bash
# 1. Docker (dépôt officiel : docker-compose-v2 n'existe pas dans Debian 13)
apt update && apt install -y ca-certificates curl git
install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/debian/gpg -o /etc/apt/keyrings/docker.asc
chmod a+r /etc/apt/keyrings/docker.asc
tee /etc/apt/sources.list.d/docker.sources <<EOF
Types: deb
URIs: https://download.docker.com/linux/debian
Suites: $(. /etc/os-release && echo "$VERSION_CODENAME")
Components: stable
Signed-By: /etc/apt/keyrings/docker.asc
EOF
apt update
apt install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin

# 2. Le projet
git clone https://github.com/Flunch4456/SRV-Source-for-live-container.git
cd SRV-Source-for-live-container
cp .env.example .env
nano .env            # change ADMIN_PASSWORD et DOMAIN

# 3. Lancer (construit l'image en local)
docker compose up -d --build
```

Test : `curl http://127.0.0.1:8000/health` doit répondre `ok`.

### Configuration (`.env`)

| Variable | Rôle |
|---|---|
| `ADMIN_PASSWORD` | Mot de passe du panel `/admin`. À défaut, `UPLOAD_TOKEN` sert de mot de passe. |
| `UPLOAD_TOKEN` | Token pour l'envoi par `curl`. Vide = API d'envoi désactivée. |
| `DOMAIN` | Ton domaine, sans `https://` (ex. `ipa.exemple.fr`). Les liens de `source.json` en dépendent. |
| `BIND_ADDRESS`, `HTTP_PORT` | Port exposé sur le réseau local (par défaut `0.0.0.0:8000`). |
| `SOURCE_NAME`, `DEVELOPER_NAME` | Nom affiché de la source, développeur par défaut des apps. |
| `MAX_UPLOAD_MB` | Taille maximale d'un envoi (2048 par défaut). |

Une valeur laissée à `change-moi` est refusée : le panel reste fermé tant que tu n'as pas mis un vrai mot de passe.

### Accès en local et domaine

- **Réseau local** : `http://IP-du-serveur:8000`. Les liens reprennent l'adresse utilisée.
- **Domaine** : quand on passe par `DOMAIN`, les liens deviennent `https://ton-domaine/...`.
- **Changer de domaine** : modifie `DOMAIN` dans `.env`, puis `docker compose up -d`.

### Mettre à jour

```bash
git pull && docker compose up -d --build
```

Tes données (`data/`) ne sont pas touchées. Les IPA déposées à plat dans `data/ipas/` par une ancienne version du serveur sont reprises automatiquement au démarrage.

## Utiliser le panel admin

1. Ouvre `/admin` et connecte-toi avec `ADMIN_PASSWORD`.
2. **Importer** : choisis un ou plusieurs `.ipa`, ajoute des notes de version si tu veux, envoie.
3. Clique sur une app pour **ajouter une version**, modifier son nom, sous-titre, description, développeur, **changer l'icône** (PNG/JPEG), éditer les notes de chaque version ou **supprimer** une version ou l'app entière.

Une app sans aucune version n'apparaît plus dans le catalogue public.

**Limite Cloudflare :** derrière le proxy Cloudflare (nuage orange), les envois sont plafonnés à **100 Mo** sur l'offre gratuite. Pour importer une grosse IPA, ouvre le panel par l'adresse locale (`http://IP-du-serveur:8000/admin`). Les téléchargements depuis l'iPhone, eux, passent normalement par le domaine.

### Envoi en ligne de commande

```bash
curl -H "X-Token: TON_TOKEN" -F "file=@MonApp.ipa" -F "notes=Correctifs" https://ton-domaine/api/upload
```

## Ajouter la source dans LiveContainer

Dans LiveContainer, ajoute la source `https://ton-domaine/source.json` (l'adresse est aussi affichée, avec un bouton « Copier », en haut du catalogue). La page de chaque app propose aussi un lien « Ouvrir dans LiveContainer ».

## Le rendre accessible depuis n'importe où

LiveContainer télécharge depuis ton iPhone : l'URL doit être joignable depuis Internet, en HTTPS de préférence.

- **Zoraxy / Nginx / autre reverse proxy** (cas actuel) : cible `http://IP-du-serveur:8000`, HTTPS géré par le proxy. Mets ton domaine dans `DOMAIN`.
- **Cloudflare Tunnel** : service cible `http://localhost:8000`, domaine dans `DOMAIN`.
- **Caddy intégré** (HTTPS automatique, ports 80/443 à rediriger) : `docker compose --profile https up -d`.
- **Tailscale** : accès privé de partout, sans rien exposer.

## Sécurité

- Le **catalogue et les IPA sont publics** : n'héberge que des apps que tu as le droit de distribuer. Rien de privé ni de payant.
- Le panel est protégé par mot de passe (comparaison à temps constant, 5 essais ratés = blocage 10 min, protection CSRF, cookie de session `HttpOnly`).
- Sur le réseau local en `http://`, le mot de passe circule sans chiffrement : pour te connecter hors de chez toi, utilise toujours l'adresse `https://`.
- Les IPA et icônes sont stockées sous des noms aléatoires : aucun nom de fichier envoyé n'est utilisé sur le disque.

## Développement et releases

```bash
pip install -r requirements-dev.txt && pytest
```

À chaque push, GitHub Actions lance les tests, construit l'image et joue `scripts/smoke-test.sh` (catalogue, panel, import, `source.json`, redémarrage). Pour publier une version :

```bash
git tag v1.1.0 && git push origin v1.1.0
```

Le workflow `release.yml` rejoue les tests, publie l'image Docker sur `ghcr.io` (amd64 et arm64) et crée la release GitHub.

## Dépannage

| Problème | Cause probable |
|---|---|
| Impossible de se connecter au panel | `ADMIN_PASSWORD` vide ou resté à `change-moi` ; ou 5 essais ratés (attends 10 min) |
| Source vide dans LiveContainer | Aucune version importée, ou IPA invalide (pas de `Payload/X.app/Info.plist`) |
| Liens de téléchargement faux | `DOMAIN` mal renseigné (sans `https://`), ou le proxy ne transmet pas le nom d'hôte |
| Téléchargement impossible sur iPhone | Source ajoutée en `http://` : utilise l'adresse `https://` du domaine |
| Envoi refusé (413 ou coupure) | Fichier plus gros que `MAX_UPLOAD_MB`, ou limite de 100 Mo de Cloudflare |
| « existe déjà » à l'import | Cette version est déjà dans le catalogue : supprime-la d'abord |
