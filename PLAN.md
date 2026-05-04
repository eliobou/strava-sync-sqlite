# Plan : Transformer strava-sync-sqlite en application web multi-utilisateurs

## Contexte

Le projet actuel est un outil CLI mono-utilisateur : des scripts Python lancés par cron qui synchronisent les activités Strava d'une personne vers GeoVelo, via une base SQLite locale. L'objectif est de le transformer en une application web hébergée où n'importe qui peut créer un compte, connecter ses propres comptes Strava et GeoVelo, et bénéficier de la synchronisation automatique — avec la possibilité de la mettre en pause.

---

## Décisions de stack

| Composant | Choix | Raison |
|---|---|---|
| **Backend web** | Flask | Minimal, s'adapte au code existant. FastAPI = overkill pour des vues HTML. Django = trop lourd pour un side project |
| **Base de données** | SQLite (WAL, restructurée) | Déjà en place, WAL gère la concurrence lecture/écriture. PostgreSQL = complexité inutile à < 500 users |
| **Scheduler** | APScheduler (in-process) | Un job par utilisateur, sans Redis ni worker externe. Celery = overkill |
| **Frontend** | Jinja2 + HTMX | Templates server-rendered, polling HTMX pour le statut live. Pas de React |
| **Chiffrement** | Fernet (bibliothèque `cryptography`) | Pour les credentials GeoVelo stockés en base |
| **Hébergement** | VPS + Docker Compose | `docker compose up -d`, volume pour SQLite, Caddy pour HTTPS automatique |

Nouvelles dépendances : `flask`, `flask-login`, `apscheduler`, `cryptography`

---

## Changements de schéma DB

### Nouvelle table : `users`
```sql
CREATE TABLE users (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    email           TEXT UNIQUE NOT NULL,
    password_hash   TEXT NOT NULL,
    created_at      TEXT DEFAULT CURRENT_TIMESTAMP,
    sync_enabled    INTEGER DEFAULT 1,       -- 0=pausé, 1=actif
    strava_connected  INTEGER DEFAULT 0,
    geovelo_connected INTEGER DEFAULT 0
);
```

### Nouvelle table : `user_strava_tokens` (remplace `config`)
```sql
CREATE TABLE user_strava_tokens (
    user_id             INTEGER PRIMARY KEY,
    strava_athlete_id   INTEGER,
    access_token        TEXT NOT NULL,
    refresh_token       TEXT NOT NULL,
    token_expires_at    INTEGER NOT NULL,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);
```

### Nouvelle table : `user_geovelo_credentials`
```sql
CREATE TABLE user_geovelo_credentials (
    user_id             INTEGER PRIMARY KEY,
    encrypted_email     BLOB NOT NULL,     -- Fernet encrypted
    encrypted_password  BLOB NOT NULL,     -- Fernet encrypted
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);
```

### Tables existantes modifiées
- `activities` : renommer `id` → `strava_id`, ajouter `id` AUTOINCREMENT comme PK interne, ajouter `user_id`, contrainte UNIQUE sur `(user_id, strava_id)`
- `activity_tracks` : cascade de la FK vers le nouveau `activities.id`
- `sync_log` : ajouter `user_id` + colonne `sync_type` ('strava' | 'geovelo')
- `geovelo_uploads` : ajouter `user_id`
- `config` : **supprimée** (remplacée par `user_strava_tokens`)

---

## Structure de l'application

Structure plate, pas de sous-modules.

```
app.py               # Toutes les routes Flask (~300 lignes)
db.py                # get_db(), init_db(), schéma SQL
strava_sync.py       # sync_user(user_id) — adapté de sync.py
geovelo_sync.py      # sync_user_geovelo(user_id) — adapté de geovelo_sync.py
scheduler.py         # APScheduler : register/pause/resume par user_id
templates/
├── base.html
├── index.html       # Dashboard
├── login.html
└── register.html
Dockerfile
docker-compose.yml
Caddyfile            # Config reverse proxy HTTPS
.env.example
requirements.txt
```

---

## Nouveaux composants clés

### 1. Auth utilisateur
- Inscription/connexion avec `werkzeug` pour le hash des mots de passe (PBKDF2-SHA256)
- Sessions via `flask-login` (cookie signé avec `SECRET_KEY`)
- `@login_required` sur toutes les routes dashboard/OAuth

### 2. Flow Strava OAuth (web)
Remplace `auth.py` (CLI manuel) par une redirection web :
1. `GET /strava/connect` → génère un `state` token, redirige vers `strava.com/oauth/authorize`
2. `GET /strava/callback?code=...&state=...` → vérifie le state, échange le code contre des tokens, les stocke dans `user_strava_tokens`, enregistre le job APScheduler

**Gotcha proxy HTTPS** : Flask derrière Caddy voit les requêtes en HTTP et génère des URLs `http://` par défaut. Sans correction, `url_for('strava_callback', _external=True)` produit `http://votredomaine.com/strava/callback` → Strava rejette le callback (URI ne correspond pas).

Correction — ajouter dans `app.py` :
```python
from werkzeug.middleware.proxy_fix import ProxyFix
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)
```
Caddy envoie `X-Forwarded-Proto: https`, `ProxyFix` le lit, et `url_for(..., _external=True)` génère correctement `https://`.

- Le `redirect_uri` `https://votredomaine.com/strava/callback` doit être enregistré sur strava.com/settings/api
- En local (dev) : utiliser `http://localhost:5000/strava/callback` — Strava autorise `localhost` sans HTTPS

### 3. Credentials GeoVelo
- Formulaire simple email/mot de passe
- Validation immédiate en appelant l'API GeoVelo
- Chiffrement Fernet avant stockage (clé dans `.env` : `APP_FERNET_KEY`)
- Déchiffrement uniquement au moment du sync, jamais retourné au navigateur

### 4. Scheduler par utilisateur
**Un seul job par utilisateur** : la sync Strava détecte les nouvelles activités et déclenche immédiatement l'upload GeoVelo dans la foulée. Pas deux processus séparés.

```python
def run_sync(user_id):
    new_activities = sync_strava(user_id)   # fetch + upsert
    if new_activities:
        sync_geovelo(user_id)               # upload immédiat

scheduler.add_job(
    func=run_sync, trigger="interval", minutes=15,
    id=f"sync_{user_id}", args=[user_id]
)
# Pause :
scheduler.get_job(f"sync_{user_id}").pause()
# Reprise :
scheduler.get_job(f"sync_{user_id}").resume()
```
- APScheduler utilise un `SQLAlchemyJobStore` sur la même DB → jobs persistants entre redémarrages
- `jitter=60` pour étaler les lancements et éviter les pics de rate limit Strava
- **Contrainte Strava** : 100 req/15min **par application** (partagé entre tous les users), pas par user. `client_id`/`client_secret` = partagés. `access_token`/`refresh_token` = par user dans `user_strava_tokens`.

### 5. Dashboard
- Statut de connexion Strava / GeoVelo
- Bouton pause/reprise (POST `/sync/toggle` → HTMX swap)
- Dernier sync (statut + timestamp)
- Fragment HTMX `/status` : polling toutes les 30 secondes pour mise à jour live

---

## Infrastructure

### Docker Compose (local et production)

**`Dockerfile`** — image Python slim, Gunicorn avec 1 worker + 4 threads (obligatoire pour APScheduler in-process) :
```dockerfile
FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
CMD ["gunicorn", "app:app", "--workers", "1", "--threads", "4", "--bind", "0.0.0.0:8000"]
```

**`docker-compose.yml`** — app + Caddy (reverse proxy HTTPS automatique) :
```yaml
services:
  web:
    build: .
    expose:
      - "8000"
    volumes:
      - ./data:/app/data   # DB SQLite persistante
    env_file: .env
    restart: unless-stopped

  caddy:
    image: caddy:2-alpine
    ports:
      - "80:80"
      - "443:443"
    volumes:
      - ./Caddyfile:/etc/caddy/Caddyfile
      - caddy_data:/data
    depends_on: [web]
    restart: unless-stopped

volumes:
  caddy_data:
```

**`Caddyfile`** — HTTPS Let's Encrypt automatique, zéro config :
```
votredomaine.com {
    reverse_proxy web:8000
}
```

### Hébergement : VPS
- **Hetzner CX22** : ~4€/mois, 2 vCPU, 4 GB RAM, amplement suffisant
- Prérequis : Docker + Docker Compose installés, port 80/443 ouverts
- Déploiement : `git pull && docker compose up -d --build`
- La DB SQLite est dans `./data/strava.db` sur le host (montée en volume)

### Variables d'environnement (`.env`)
```
SECRET_KEY=<random 32 bytes hex>
APP_FERNET_KEY=<Fernet.generate_key() output>
STRAVA_CLIENT_ID=
STRAVA_CLIENT_SECRET=
DB_PATH=/app/data/strava.db
```

---

## Ce qui est réutilisé sans changement

| Code actuel | Destination |
|---|---|
| `StravaClient._get()`, `get_all_activities()` | `strava_sync.py` — zéro changement |
| `activity_to_row()`, `decode_polyline()`, `upsert_activity()` | `strava_sync.py` — ajouter `user_id` aux WHERE |
| `get_access_token()` | `strava_sync.py` — lire/écrire dans `user_strava_tokens` au lieu de `config` |
| `build_gpx()` | `geovelo_sync.py` — zéro changement |
| `geovelo_authenticate()`, `upload_gpx()` | `geovelo_sync.py` — zéro changement |

C'est une adaptation, pas une réécriture complète.

---

## Séquence d'implémentation

1. Flask app factory + SQLite + login/register
2. Nouveau schéma DB (script de migration)
3. Flow Strava OAuth web
4. Port de `sync.py` → `app/strava/sync.py` avec `user_id`
5. APScheduler + job Strava
6. Dashboard minimal
7. Formulaire credentials GeoVelo + chiffrement Fernet
8. Port de `geovelo_sync.py` → `app/geovelo/sync.py` avec `user_id`
9. Job GeoVelo + bouton pause/reprise HTMX
10. Déploiement Docker sur VPS (`docker compose up -d`)

---

## Sécurité

| Risque | Mitigation |
|---|---|
| Mots de passe GeoVelo stockés | Chiffrement Fernet avec clé en env |
| Tokens Strava volés | Volume DB en accès restreint (chmod 600) |
| CSRF sur les formulaires | Flask-WTF ou token CSRF manuel dans les sessions |
| CSRF sur le flow OAuth | State token aléatoire par flow, vérifié au callback |
| Sessions forgées | `SECRET_KEY` fort aléatoire, jamais hardcodé |

---

## Limitations connues

- **Suppression Strava → GeoVelo non propagée** : si une activité est supprimée sur Strava, elle est marquée `is_deleted = 1` en base mais la trace reste sur GeoVelo. Nécessiterait un endpoint DELETE GeoVelo (existence non vérifiée). Hors scope V1 — décision produit à prendre.

---

## Vérification

- Tester le flow complet en local avec `http://localhost` comme redirect_uri Strava
- Vérifier que le chiffrement/déchiffrement GeoVelo fonctionne dans un test unitaire
- Vérifier que les jobs APScheduler redémarrent correctement après un `kill -SIGTERM` du processus
- Tester la pause/reprise depuis le dashboard
- Déployer sur VPS (`docker compose up -d`) et vérifier le flow OAuth avec HTTPS via Caddy
