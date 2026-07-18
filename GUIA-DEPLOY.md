# Guía de Deploy — Unified Jackpot Tracker en `jackpot.lililimon.vip`

Esta guía asume una VPS Ubuntu 24.04 fresca con acceso SSH y el dominio `jackpot.lililimon.vip` apuntando a la IP de la VPS. Está escrita para ser ejecutada paso a paso, con comandos copy-pasteables.

**Tiempo estimado:** 30-50 minutos desde VPS vacía hasta HTTPS funcionando.

---

## 0. Pre-requisitos

Confirmar antes de empezar:

```bash
lsb_release -a                  # Ubuntu 24.04
free -h                         # >= 4 GB RAM
df -h /                         # >= 20 GB libres
nproc                           # >= 2 vCPU
ip -br a                        # tu IP pública (IPv4)
```

Acceso a la VPS:

```bash
ssh usuario@40.160.231.65       # OVH VPS Ubuntu 24.04 (usuario inicial: ubuntu)
```

El dominio `jackpot.lililimon.vip` debe tener un registro A apuntando a `40.160.231.65` (IPv4). Verificar:

```bash
dig +short jackpot.lililimon.vip A
# debe devolver: 40.160.231.65
```

---

## 1. Sistema base

```bash
sudo apt update && sudo apt upgrade -y
sudo apt install -y software-properties-common curl git ufw fail2ban python3-pip python3-venv nginx certbot python3-certbot-nginx sqlite3
sudo systemctl enable --now fail2ban
sudo timedatectl set-timezone UTC    # o America/Lima si prefieres hora local
```

---

## 2. Crear usuario `deploy`

No correr la app como root. Crear un usuario dedicado con sudo sin password (igual que la imagen OVH Ubuntu para `ubuntu`):

```bash
sudo adduser --disabled-password --gecos "" deploy
echo "deploy ALL=(ALL) NOPASSWD:ALL" | sudo tee /etc/sudoers.d/deploy-deploy
sudo chmod 440 /etc/sudoers.d/deploy-deploy
```

Desde Windows ahora puedes hacer `ssh deploy@40.160.231.65`.

---

## 3. Clonar el repo

```bash
sudo mkdir -p /opt/jackpot-tracker
sudo chown deploy:deploy /opt/jackpot-tracker
sudo -u deploy git clone https://github.com/<tu-usuario>/unified-jackpot-tracker.git /opt/jackpot-tracker
cd /opt/jackpot-tracker
```

> Si el repo es privado, primero añade tu clave SSH al servidor (en GitHub → Settings → SSH and GPG keys → New SSH key), luego clona con `git@github.com:...` en lugar de `https://`.

---

## 4. Configurar credenciales CarnivalCiti

El repo NO incluye `accessos.txt` (es `.gitignore`). Crearlo manualmente en el server:

```bash
sudo -u deploy tee /opt/jackpot-tracker/accessos.txt > /dev/null <<'EOF'
# CarnivalCiti credentials
#email
tu@email.com
#password
tu_password
EOF
sudo chmod 600 /opt/jackpot-tracker/accessos.txt
```

Si quieres trackear juegos de Zitobox, edita `links_zitobox.txt` (formato: `Display Name | Jackpot Label | URL`). Si no, el scraper Zitobox simplemente devolverá 0 juegos.

---

## 5. Crear venv e instalar dependencias

```bash
sudo -u deploy python3 -m venv .venv
sudo -u deploy ./bin/venv/bin/pip install --upgrade pip
sudo -u deploy ./.venv/bin/pip install -r requirements.txt
sudo -u deploy ./.venv/bin/playwright install --with-deps chromium
```

`--with-deps` instala las libs del sistema que Playwright necesita (libnss3, libatk1.0, etc.). Si te da error de permisos, ejecutar la primera parte como root y luego cambiar ownership.

---

## 6. Servicio systemd

Crear el unit file. La app escucha en `127.0.0.1:5050` (sólo loopback; Nginx hace el proxy público).

```bash
sudo tee /etc/systemd/system/jackpot-tracker.service > /dev/null <<'EOF'
[Unit]
Description=Unified Jackpot Tracker (Flask + Playwright)
After=network.target

[Service]
Type=simple
User=deploy
WorkingDirectory=/opt/jackpot-tracker
ExecStart=/opt/jackpot-tracker/.venv/bin/python app.py
Restart=always
RestartSec=10
StandardOutput=append:/opt/jackpot-tracker/data/tracker_stdout.log
StandardError=append:/opt/jackpot-tracker/data/tracker_stdout.log

# Hardening
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=full
ProtectHome=true

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now jackpot-tracker
sudo systemctl status jackpot-tracker   # debe estar "active (running)"
```

Verificación rápida:

```bash
curl -s http://127.0.0.1:5050/api/status | head -5
```

Si ves JSON, la app está sirviendo localmente. Si no, `journalctl -u jackpot-tracker -n 50`.

---

## 7. Firewall

```bash
sudo ufw allow OpenSSH
sudo ufw allow "Nginx Full"   # 80 + 443
sudo ufw enable
sudo ufw status verbose
```

NO abrir el puerto 5050 — Nginx lo expone via 443 y el puerto real queda en loopback.

---

## 8. Nginx como reverse proxy

```bash
sudo tee /etc/nginx/sites-available/jackpot.lililimon.vip > /dev/null <<'EOF'
server {
    listen 80;
    server_name jackpot.lililimon.vip;

    location / {
        proxy_pass http://127.0.0.1:5050;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        # Long timeouts so the CarnivalCiti retry logic (up to 270s per game)
        # doesn't get cut off mid-request.
        proxy_read_timeout 600s;
        proxy_send_timeout 600s;
    }
}
EOF

sudo ln -sf /etc/nginx/sites-available/jackpot.lililimon.vip /etc/nginx/sites-enabled/
sudo nginx -t
sudo systemctl reload nginx
```

Verificar HTTP plano antes de meter HTTPS:

```bash
curl -I http://jackpot.lililimon.vip/
# debe devolver 200 OK desde Nginx
```

---

## 9. HTTPS con Let's Encrypt

```bash
sudo certbot --nginx -d jackpot.lililimon.vip --non-interactive --agree-tos -m tu@email.com
```

Certbot modifica el bloque de Nginx para añadir la sección `listen 443 ssl` y la redirección 80→443. Verificar:

```bash
curl -I https://jackpot.lililimon.vip/
# debe devolver 200 OK con headers de seguridad
```

El cert se renueva automáticamente vía `certbot.timer` (ya habilitado por defecto).

---

## 10. Backups automáticos de SQLite

La DB vive en `/opt/jackpot-tracker/data/tracker.db`. Backup diario con 14 días de retención:

```bash
sudo tee /etc/cron.daily/jackpot-backup > /dev/null <<'EOF'
#!/bin/bash
set -e
BACKUP_DIR=/opt/jackpot-tracker/data/backups
mkdir -p "$BACKUP_DIR"
TS=$(date +%Y%m%d_%H%M%S)
sqlite3 /opt/jackpot-tracker/data/tracker.db ".backup '$BACKUP_DIR/tracker_$TS.db'"
# Rotate: keep last 14 daily backups
find "$BACKUP_DIR" -name 'tracker_*.db' -mtime +14 -delete
EOF
sudo chmod +x /etc/cron.daily/jackpot-backup
```

`sqlite3 .backup` es atómico — copia la DB sin riesgo de inconsistencia.

---

## 11. Hardening final

```bash
# fail2ban ya está activo (reglas SSH por defecto)
sudo fail2ban-client status sshd

# Permisos estrictos en accessos.txt
sudo -u deploy chmod 600 /opt/jackpot-tracker/accessos.txt

# Backup de Carnival storage (session cookies, sensible)
sudo -u deploy chmod 600 /opt/jackpot-tracker/data/carnival_storage.json 2>/dev/null || true
```

---

## 12. Verificación final

```bash
# 1. App responde por HTTPS
curl -sI https://jackpot.lililimon.vip/ | head -3

# 2. API funcional
curl -s https://jackpot.lililimon.vip/api/sources | python3 -m json.tool | head -10

# 3. Diagnóstico
curl -s https://jackpot.lililimon.vip/api/diagnostics | python3 -m json.tool | head -30

# 4. Logs
sudo journalctl -u jackpot-tracker --since "10 minutes ago" | tail -20

# 5. El scraper Zitobox debería haber cosechado tokens y guardado jackpot al menos una vez por hora
curl -s 'https://jackpot.lililimon.vip/api/history?source=zitobox&code=<game_code>' \
  | python3 -m json.tool | head -20
```

Abre `https://jackpot.lililimon.vip/` en el navegador. Deberías ver el dashboard con los tabs por source.

---

## Re-deploy (actualizaciones del código)

```bash
# Desde Windows
cd unified-jackpot-tracker
git add .
git commit -m "..."
git push origin main

# Desde la VPS
ssh deploy@40.160.231.65
cd /opt/jackpot-tracker
git pull origin main
.venv/bin/pip install -r requirements.txt    # solo si requirements.txt cambió
sudo systemctl restart jackpot-tracker
curl -I https://jackpot.lililimon.vip/
```

---

## Troubleshooting

| Síntoma | Causa probable | Fix |
|---------|----------------|-----|
| `systemctl status jackpot-tracker` muestra "failed" | Error en `accessos.txt`, Playwright no instalado, o falta módulo | `journalctl -u jackpot-tracker -n 100` para el traceback completo |
| `502 Bad Gateway` desde Nginx | App no está escuchando en 5050 | `ss -tlnp | grep 5050` para confirmar; reiniciar el servicio |
| Zitobox: tokens expirados / scraper lento | Cached token expiró, ahora re-harvest | Normal en la primera falla. El 2do intento usa el token fresco. |
| CarnivalCiti: `login_failed` | Credenciales mal en `accessos.txt` | Editar archivo, reiniciar servicio, esperar próximo ciclo |
| CarnivalCiti: `no_cc_jackpot` repetido | El SPA tarda más de 270s en cargar | Esperar siguiente ciclo. Si persiste, aumentar `MAX_ATTEMPTS` y `PER_ATTEMPT_DEADLINE` en `scrapers.py` |
| `certbot renew` falla | Rate limit de Let's Encrypt o DNS no resuelve | `sudo certbot renew --dry-run` para diagnóstico |
| Disco lleno | `data/` creció mucho sin backups | `du -sh /opt/jackpot-tracker/data/*` para encontrar al culpable (típicamente `tracker.log`) |

---

## Hard reset (empezar de cero, perder datos)

```bash
sudo systemctl stop jackpot-tracker
sudo -u deploy rm -rf /opt/jackpot-tracker/data/tracker.db
sudo -u deploy rm -rf /opt/jackpot-tracker/data/backups/*
sudo -u deploy rm -f /opt/jackpot-tracker/data/carnival_storage.json
sudo systemctl start jackpot-tracker
```

---

## Estructura final

```
/opt/jackpot-tracker/
├── .venv/                    Python venv (no versionar)
├── data/                     Runtime state
│   ├── tracker.db            SQLite (jackpot readings + games + hits)
│   ├── tracker.log           Append-only app log
│   ├── tracker_stdout.log    systemd stdout/stderr capture
│   ├── carnival_storage.json Browser session cookies (CC scraper)
│   └── backups/              Cron-managed SQLite snapshots (14d retention)
├── accessos.txt              CarnivalCiti creds (chmod 600)
├── links_zitobox.txt         Zitobox game URLs (una por línea)
├── links_carnivalciti.txt    CarnivalCiti game URLs (incluido en repo)
├── app.py                    Flask app
├── scrapers.py               Zitobox + CarnivalCiti
├── db.py                     SQLite + export
├── templates/index.html      Dashboard
├── test_regression.py        13 tests, <5s
└── requirements.txt          flask + playwright + httpx
```