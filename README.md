# Serveur de source IPA pour LiveContainer

Petit serveur qui héberge tes IPA et génère automatiquement une **source au format AltStore** (`/source.json`), que LiveContainer sait lire.

## Fonctionnement

- Tu déposes un `.ipa` (via la page web, `curl`, ou directement dans `data/ipas/`).
- Le serveur ouvre l'IPA (c'est un zip), lit son `Info.plist` et récupère nom, bundle ID, version.
- `/source.json` est régénéré à chaque requête : pas de base de données, pas de fichier à éditer.
- Une image `MonApp.png` à côté de `MonApp.ipa` devient son icône.

## Installation sur la VM Debian 13

```bash
# 1. Docker
sudo apt update && sudo apt install -y docker.io docker-compose-v2 git
sudo systemctl enable --now docker

# 2. Le projet
git clone https://github.com/Flunch4456/srv-source-for-live-container.git
cd srv-source-for-live-container
cp .env.example .env
nano .env            # change UPLOAD_TOKEN et PUBLIC_URL

# 3. Lancer (construit l'image en local, ne dépend de rien d'autre)
docker compose up -d --build
```

Test en local : `curl http://127.0.0.1:8000/health` doit répondre `ok`.

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

LiveContainer télécharge via ton iPhone, donc l'URL doit être joignable depuis Internet **et en HTTPS** (iOS refuse le HTTP simple pour les téléchargements). Trois options :

### Option A, Cloudflare Tunnel (la plus simple, aucun port à ouvrir)

Pas besoin de toucher à ta box. Sur la VM :

```bash
cloudflared tunnel --url http://127.0.0.1:8000     # test rapide, URL temporaire
```

Pour une URL fixe, crée un tunnel nommé avec un domaine dans Cloudflare Zero Trust, puis mets cette URL dans `PUBLIC_URL`.

### Option B, Caddy + nom de domaine (HTTPS automatique)

1. Un nom de domaine (ou DuckDNS gratuit) qui pointe vers l'IP publique de ta box.
2. Sur la box : rediriger les ports **80 et 443** vers la VM.
3. Dans `Caddyfile`, remplace `ipa.exemple.fr` par ton domaine.
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
| Téléchargement impossible | `PUBLIC_URL` faux ou en `http://` au lieu de `https://` |
| Upload refusé (413) | Fichier plus gros que `MAX_UPLOAD_MB` |
| Upload refusé (403) | Mauvais token |
