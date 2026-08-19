# Déploiement VPS — V1 (Market Observation + Paper Trading)

Objectif : faire tourner les collectors Binance/OKX/Bybit en conditions réelles
pour la première fois (jamais testé jusqu'ici — la sandbox de dev bloque ces
domaines). **Toujours zéro capital réel, zéro ordre réel** — voir la note
sécurité en bas.

## 1. Choisir le VPS

- **OS** : Ubuntu 22.04/24.04 LTS (le plus simple pour Docker).
- **Taille** : 2 vCPU / 4 Go RAM suffit largement pour 3 WebSockets + Postgres + Redis + FastAPI. Pas besoin de plus pour la V1.
- **Région** : proche des serveurs des exchanges si tu veux des latences propres pour le Latency Monitoring (section 18) — eu-west (Europe) ou us-east sont les choix classiques pour Binance/OKX/Bybit.
- **Fournisseur** : peu importe (Hetzner, OVH, DigitalOcean, Contabo...) tant que le firewall sortant n'est pas restrictif — voir étape 3.

## 2. Prérequis sur le VPS

```bash
# Docker + Docker Compose plugin
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker $USER   # puis se reconnecter (logout/login)

# Python 3.12
sudo apt update
sudo apt install -y python3.12 python3.12-venv git
```

## 3. Vérifier que les exchanges sont bien joignables

C'est le test qui a bloqué en local — à faire en premier sur le VPS avant tout le reste :

```bash
curl -s -o /dev/null -w "%{http_code}\n" https://api.binance.com/api/v3/ping   # attendu: 200
curl -s -o /dev/null -w "%{http_code}\n" https://www.okx.com                    # attendu: 200
curl -s -o /dev/null -w "%{http_code}\n" https://api.bybit.com                  # attendu: 200
```

Si l'un des trois time-out, c'est un firewall sortant du provider (rare) — sinon c'est bon, passe à l'étape suivante.

## 4. Déployer le code

```bash
git clone <url-du-repo> robotcripto   # ou scp/rsync depuis ta machine si pas encore sur un remote
cd robotcripto
cp .env.example .env
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

docker compose up -d      # Postgres (TimescaleDB) + Redis
```

## 5. Premier run — ce qu'il faut surveiller

```bash
python main.py
```

Dans les logs, tu dois voir dans les premières secondes :

```
binance collector connected (N symbols)
okx collector connected (N symbols)
bybit collector connected (N symbols)
```

Si un collector boucle en "crashed, reconnecting in Xs" au lieu de "connected",
c'est un vrai bug de payload à corriger (le code n'a jamais tourné contre les
vraies données — voir `README.md` section "Known limitations"). Copie-moi les
logs d'erreur, je debug depuis là.

Laisse tourner **au moins quelques minutes** avant de juger : les premières
opportunités doivent apparaître via :

```bash
curl localhost:8000/opportunities | jq
```

## 6. Faire tourner ça en continu (24/7 comme demandé section 40)

Une fois que le run manuel est stable, passe en service systemd pour survivre
aux reboots/déconnexions SSH :

```ini
# /etc/systemd/system/arbitrage-engine.service
[Unit]
Description=Multi-Market Arbitrage Engine
After=docker.service network-online.target

[Service]
Type=simple
User=<ton-user>
WorkingDirectory=/home/<ton-user>/robotcripto
ExecStart=/home/<ton-user>/robotcripto/.venv/bin/python main.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now arbitrage-engine
sudo journalctl -u arbitrage-engine -f   # logs en continu
```

Pour le dashboard, un simple screen/tmux suffit pour la V1 :

```bash
tmux new -s dashboard
streamlit run dashboard/app.py --server.port 8501 --server.address 0.0.0.0
```

## 7. Sécurité minimale

- **Firewall (ufw)** : n'ouvre que ce qui doit être public.
  ```bash
  sudo ufw allow OpenSSH
  sudo ufw allow 8501/tcp   # dashboard, ou tunnel SSH plutôt que d'exposer
  sudo ufw enable
  ```
- **Ne pas exposer Postgres (5432) ni Redis (6379) sur Internet** — `docker-compose.yml` les publie sur `0.0.0.0` par défaut ; si le VPS a une IP publique, change le mapping en `127.0.0.1:5432:5432` / `127.0.0.1:6379:6379` avant de démarrer.
- **Aucune clé API d'exchange n'est nécessaire pour la V1** — tout est en lecture publique (WebSocket ticker + REST funding). Ne mets aucune clé avec permission "Trade" ou "Withdraw" dans `.env` tant qu'on n'est pas en Phase 2 du cahier des charges (section 36) — ce qui n'est pas encore construit (pas d'Execution Engine, pas de gestion de clés signées).
- Si tu préfères ne pas exposer le dashboard du tout, garde-le en local et accède-y via un tunnel SSH : `ssh -L 8501:localhost:8501 <user>@<vps-ip>`.

## 8. Après quelques jours

Le cahier des charges (section 40) demande 7 jours d'observation continue
avant toute décision GO/MODIFY/NO-GO. Pas d'action de trading réelle à
prévoir avant ce jalon, quelle que soit la qualité des chiffres qui sortent
plus tôt.
