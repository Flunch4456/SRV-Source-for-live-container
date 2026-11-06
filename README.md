# Serveur de source IPA pour LiveContainer

Petit serveur qui héberge tes IPA et génère automatiquement une **source au format AltStore** (`/source.json`), que LiveContainer sait lire.

## Fonctionnement

- Tu déposes un `.ipa` (via la page web, `curl`, ou directement dans `data/ipas/`).
- Le serveur ouvre l'IPA (c'est un zip), lit son `Info.plist` et récupère nom, bundle ID, version.
- `/source.json` est régénéré à chaque requête : pas de base de données, pas de fichier à éditer.
- Une image `MonApp.png` à côté de `MonApp.ipa` devient son icône.

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
nano .env            # change UPLOAD_TOKEN et DOMAIN

# 3. Lancer (construit l'image en local, ne dépend de rien d'autre)
docker compose up -d --build
```

Test : `curl http://127.0.0.1:8000/health` doit répondre `ok`.

### Accès en local et domaine

- **Réseau local** : `http://IP-du-serveur:8000`. Les liens de `source.json` reprennent l'adresse utilisée, donc tout marche en local sans domaine.
- **Domaine** : mets-le dans `.env` (`DOMAIN=ipa.exemple.fr`, sans `https://`). Quand on passe par ce domaine, les liens deviennent `https://ipa.exemple.fr/...`.
- **Changer de domaine** : modifie `DOMAIN` dans `.env`, puis `docker compose up -d`. Rien d'autre à toucher (Caddy lit aussi cette variable).

### Mettre à jour

```bash
git pull && docker compose up -d --build
```

Tes IPA sont dans `data/` (hors de l'image), elles ne sont jamais perdues lors d'une mise à jour.

### Variante : image prête à l'emploi

Chaque release publie une image sur GitHub (`ghcr.io/flunch4456/srv-source-for-live-container`, en amd64 et arm64). Au lieu de `--build` :

```bash
docker compose pull && docker compose up -d
```

> Si le `pull` est refusé, ouvre le paquet sur GitHub (profil → Packages) → Package settings → passe sa visibilité en **Public**. C'est à faire une seule fois.

## Faire une nouvelle release

Les GitHub Actions s'en chargent. À chaque push, `ci.yml` construit l'image et lance `scripts/smoke-test.sh`. Pour publier :

```bash
git tag v1.0.1 && git push origin v1.0.1
```

Le workflow `release.yml` rejoue le test, publie l'image Docker et crée la release GitHub avec une archive du projet.

## Le rendre accessible depuis n'importe où

LiveContainer télécharge depuis ton iPhone, donc l'URL doit être joignable depuis Internet, et en HTTPS de préférence (iOS bloque souvent le HTTP simple). Trois options :

### Option A, Cloudflare Tunnel (la plus simple, aucun port à ouvrir)

Pas besoin de toucher à ta box. Sur la VM :

```bash
cloudflared tunnel --url http://127.0.0.1:8000     # test rapide, URL temporaire
```

Pour une URL fixe, crée un tunnel nommé avec ton domaine dans Cloudflare Zero Trust (service cible : `http://localhost:8000`), puis mets ce domaine dans `DOMAIN` (fichier `.env`).

### Option B, Caddy + nom de domaine (HTTPS automatique)

1. Un nom de domaine (ou DuckDNS gratuit) qui pointe vers l'IP publique de ta box.
2. Sur la box : rediriger les ports **80 et 443** vers la VM.
3. Mets ton domaine dans `DOMAIN` (fichier `.env`) : le `Caddyfile` le lit, rien à y modifier.
4. `docker compose --profile https up -d`

### Option C, Tailscale (privé, pas vraiment « public »)

Installe Tailscale sur la VM et l'iPhone : tu y accèdes de partout sans rien exposer. Idéal si tu es seul à l'utiliser.

## Ajouter la source dans LiveContainer

Dans LiveContainer, onglet **Sources** → **+** → colle `https://ton-domaine/source.json`.

## Envoyer une IPA

- **Page web** : `https://ton-domaine/` → champ token + fichier.
- **curl** (pratique depuis un PC) :

```bash
curl -H "X-Token: TON_TOKEN" -F "file=@MonApp.ipa" -F "file=@MonApp.png" https://ton-domaine/upload
```

- **Directement** : copie le fichier dans `data/ipas/` (aucun redémarrage nécessaire).

## Sécurité

- Seul l'**upload et la suppression** demandent le token. Quiconque connaît l'URL peut **télécharger** les IPA : n'y mets rien de privé ou de payant.
- Garde un `UPLOAD_TOKEN` long (`openssl rand -hex 24`).
- N'héberge que des apps dont tu as le droit de distribuer/utiliser le code.

## Dépannage

| Problème | Cause probable |
|---|---|
| Source vide dans LiveContainer | IPA invalide (pas de `Payload/X.app/Info.plist`) |
| Liens de téléchargement faux | `DOMAIN` mal renseigné dans `.env` (sans `https://`), ou le proxy ne transmet pas le nom d'hôte |
| Téléchargement impossible sur iPhone | Source ajoutée en `http://` : utilise l'adresse `https://` du domaine |
| Upload refusé (413) | Fichier plus gros que `MAX_UPLOAD_MB` |
| Upload refusé (403) | Mauvais token |
