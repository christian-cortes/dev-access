# cortexdev-access

Capa de acceso de la infraestructura `*.cortexdev.win`: portal/índice y dashboards de
infraestructura (Pi-hole, Proxmox, PBS, Netdata y Grafana). Vive en **ubuntu-services**
(192.168.0.49), que está siempre encendida, para que reiniciar `ubuntu-docker` (192.168.0.87,
donde corren las apps) no deje sin portal ni DNS.

> `*.cortexdev.lan` fue **retirado** (Fase 3): ni la capa de acceso ni el DNS lo sirven ya.

## Contenido

```
docker-compose.yml                 # nginx:stable + portal-api, network_mode host
nginx/conf.d/
  index.conf                       # index.cortexdev.win  → portal/ + /api/
  infra.conf                       # pihole, proxmox, backups/pbs, grafana, netdata-*
  snippets/                        # cuerpos compartidos y par TLS (tls-win.conf)
portal/                            # portal (shells HTML + render.js, favicons, iconos)
backend/                           # portal-api: FastAPI + SQLite (catalogo y estado)
backend/catalog.yml                # catalogo de servicios (fuente de verdad, se edita aqui)
monitoring/prometheus/             # prometheus.yml + reglas de alertas (Netdata)
monitoring/grafana/                # provisioning (datasource + dashboards) y el dashboard
.env.example                       # plantilla de credenciales de Grafana (.env gitignored)
certs/                             # gitignored; wildcard *.cortexdev.win
systemd/access-ingress.service.template
systemd/acme-renew.service.template
systemd/acme-renew.timer
scripts/install-win-cert.sh        # cert *.cortexdev.win: provisional / emision Let's Encrypt
scripts/add-app-win-vhost.sh       # en ubuntu-docker: añade un vhost proxy *.cortexdev.win
scripts/apps-verify-win.sh         # en ubuntu-docker: verifica los vhosts .win de nginx_web
scripts/apps-audit-lan.sh          # en ubuntu-docker: audita restos de dominio antiguo en apps
scripts/apps-fix-acme-reload.sh    # en ubuntu-docker: asegura reload de nginx_web al renovar
scripts/apps-retire-lan.sh         # en ubuntu-docker: desactiva el legado .lan del nginx de apps
scripts/diagnose-host.sh           # diagnostica puertos/servicios de un host interno
scripts/acme-renew.sh              # renovacion periodica (acme.sh --cron) para systemd
scripts/dns-overrides.sh           # overrides DNS de la capa de acceso en Pi-hole (idempotente)
scripts/tailscale-dns.sh           # verifica split DNS Tailscale + Pi-hole
scripts/deploy.sh
scripts/check.sh
scripts/setup-services.sh
backupcsr/                         # copias de seguridad (cron) ejecutadas en esta maquina
backupcsr/web/                     # portal de administracion (copias.cortexdev.win) + API
INSTALACION-cortexdev-win.md       # runbook paso a paso: Cloudflare, emision, DNS y Tailscale
```

## Requisitos en ubuntu-services

- Docker + Docker Compose v2, con el servicio habilitado al arranque (`systemctl is-enabled docker`).
- Pi-hole (nativo o Docker) debe soltar 80/443 para dejárselos a este nginx:
  - **nativo (v6)**: `sudo pihole-FTL --config webserver.port '8080o,8443s'` y `sudo systemctl restart pihole-FTL`.
  - **Docker**: publicar `8080:80` y `8443:443` en su compose y recrear con `docker compose up -d`
    (no `restart`), con `restart: unless-stopped`.
  `infra.conf` proxifica `pihole.cortexdev.win` a `127.0.0.1:8443`.
- Para acceso remoto por Tailscale, Pi-hole debe aceptar consultas del tailnet:
  `sudo pihole-FTL --config dns.listeningMode ALL` y `sudo systemctl restart pihole-FTL`
  (ver "Acceso remoto con Tailscale").
- El certificado `*.cortexdev.win` (no está en git).

## Puesta en marcha

```bash
# 1. Clonar
git clone <REMOTE_URL> ~/cortexdev-access
cd ~/cortexdev-access

# 2. Levantar nginx de acceso (requiere 80/443 libres).
#    Si aun no hay cert real, se genera un provisional autofirmado para arrancar.
docker compose up -d

# 3. Autostart (opcional, recomendado)
INSTALL_SYSTEMD=1 scripts/deploy.sh
```

El certificado real se emite con `scripts/install-win-cert.sh --issue` (ver
"Dominio interno con certificado público").

## Actualizar

```bash
scripts/deploy.sh                 # git pull + docker compose up -d --build
```

## Validar

En `ubuntu-services`:

```bash
scripts/check.sh
```

Comprueba el certificado, que `access_nginx` esté corriendo, respuestas HTTP de acceso y apps
(sin `-k`), los overrides DNS `.win` en Pi-hole y los puertos 80/443. Equivalente manual:

```bash
curl -sSI https://index.cortexdev.win/            # 200 sin -k
dig +short index.cortexdev.win @127.0.0.1         # 192.168.0.49
dig +short app-admin.cortexdev.win @127.0.0.1     # 192.168.0.87
openssl s_client -connect 192.168.0.49:443 -servername index.cortexdev.win  # issuer Let's Encrypt
dig +short index.cortexdev.lan @127.0.0.1         # vacio (retirado)
# No debe existir DNS publico del dominio:
dig +short index.cortexdev.win @1.1.1.1           # vacio
```

Si `index` devuelve `403`, Pi-hole sigue ocupando el 443: remapea su UI a `8080/8443`.

### Puesta en marcha todo-en-uno

Si prefieres un solo paso (idempotente), en `ubuntu-services`:

```bash
scripts/setup-services.sh
```

Detecta Pi-hole y libera 80/443, asegura el cert `.win`, levanta el compose, aplica los overrides
DNS con `scripts/dns-overrides.sh` y valida con `scripts/check.sh`.

## DNS en Pi-hole (192.168.0.49)

En `misc.dnsmasq_lines` el wildcard manda las apps a ubuntu-docker; estos overrides mandan la
capa de acceso a ubuntu-services (dnsmasq usa la coincidencia más específica).
`scripts/dns-overrides.sh` los aplica automáticamente (backup de `pihole.toml`, escritura vía
`pihole-FTL --config`, `reloaddns` y verificación con `dig`; idempotente). Además **purga**
cualquier línea `.lan`:

```
address=/cortexdev.win/192.168.0.87
address=/index.cortexdev.win/192.168.0.49
address=/pihole.cortexdev.win/192.168.0.49
address=/proxmox.cortexdev.win/192.168.0.49
address=/backups.cortexdev.win/192.168.0.49
address=/pbs.cortexdev.win/192.168.0.49
address=/grafana.cortexdev.win/192.168.0.49
address=/netdata-services.cortexdev.win/192.168.0.49
address=/netdata-backups.cortexdev.win/192.168.0.49
address=/netdata-proxmox.cortexdev.win/192.168.0.49
address=/netdata-docker.cortexdev.win/192.168.0.49
```

## Acceso remoto con Tailscale

Los equipos en la VPN Tailscale usan MagicDNS (`100.100.100.100`). Para que
`https://<host>.cortexdev.win` resuelva fuera de la LAN, el tailnet necesita un **split DNS**
(restricted nameserver) hacia Pi-hole. La subred `192.168.0.0/24` ya la anuncia
`ubuntu-services`, así que solo falta el DNS.

### 1. Split DNS en Tailscale (una vez, en la consola)

En <https://console.tailscale.com/admin/dns>:

- **Add nameserver** → **Custom** → `192.168.0.49`, restringido al dominio `cortexdev.win`.
- **No** actives "Override DNS servers" (solo ese dominio va a Pi-hole).
- Opcional: añade `cortexdev.win` a **Search domains** para escribir `index` a secas.
- Si algún equipo usa **exit node**: activa "Use with exit node" en ese nameserver.

En cada cliente: "Use Tailscale DNS settings" activado (por defecto) y rutas de subred aceptadas
(Linux `sudo tailscale set --accept-routes`; Windows/Android/iOS: "Use Tailscale subnets").

### 2. Pi-hole

Debe aceptar consultas con origen del tailnet (`100.64.0.0/10`), que Pi-hole en modo `LOCAL`
rechaza:

```bash
sudo pihole-FTL --config dns.listeningMode ALL
sudo systemctl restart pihole-FTL
```

`ufw` está deshabilitado (`ENABLED=no`) y no bloquea. Si se habilita, permite 53 desde
`192.168.0.0/24` y `100.64.0.0/10`.

### 3. Validar

En `ubuntu-services`: `scripts/tailscale-dns.sh` (también se ejecuta dentro de `scripts/check.sh`).

Desde un cliente remoto conectado solo por Tailscale:

```bash
dig +short index.cortexdev.win @100.100.100.100       # 192.168.0.49
dig +short app-admin.cortexdev.win @100.100.100.100   # 192.168.0.87 (wildcard apps)
curl -vI https://index.cortexdev.win                  # 200
```

Windows: `Resolve-DnsName index.cortexdev.win` (no `nslookup`, no respeta el split DNS).

## Dominio interno con certificado público (`*.cortexdev.win`)

Todo se sirve como `*.cortexdev.win` con un certificado comodín de **Let's Encrypt**, de modo que
**no aparece el aviso de certificado** aunque el equipo no tenga instalada ninguna CA. El dominio
sigue siendo interno: no se publica ningún A/AAAA ni se abren puertos, y el DNS lo resuelve
Pi-hole dentro de la LAN o el split DNS de Tailscale.

> Runbook completo con todos los comandos (Cloudflare, emisión, renovación, DNS y Tailscale):
> [`INSTALACION-cortexdev-win.md`](INSTALACION-cortexdev-win.md).

- Emisión por **DNS-01 con Cloudflare** (obligatorio: es comodín y no se exponen 80/443).
- La zona de Cloudflare no debe tener registros A/AAAA de servicio: solo NS/SOA y el TXT
  temporal `_acme-challenge` que acme.sh crea y borra.
- `.lan` y la CA mkcert fueron retirados; no hace falta instalar nada en los equipos.

### Emisión y credencial (una vez)

El token de Cloudflare necesita el permiso `Zone:DNS:Edit` sobre `cortexdev.win`:

```bash
sudo install -d -m 700 /etc/cortexdev
printf 'CF_Token=<token>\n' | sudo tee /etc/cortexdev/acme.env >/dev/null
sudo chmod 600 /etc/cortexdev/acme.env
```

Luego, en `ubuntu-services` (root):

```bash
# 1. Ensayo en staging (evita el rate limit de produccion; no instala nada)
sudo scripts/install-win-cert.sh --staging

# 2. Emision real e instalacion en certs/cortexdev.win/ + recarga de nginx
sudo scripts/install-win-cert.sh --issue

# 3. Estado del certificado
scripts/install-win-cert.sh --status
```

Mientras no se emita el real, `scripts/install-win-cert.sh` (y `deploy.sh`/`setup-services.sh`)
generan un **provisional autofirmado** para que nginx arranque; los navegadores avisarán en ese
periodo. El par (`certs/cortexdev.win/{fullchain.pem,key.pem}`) está **gitignored** y la key se
fuerza a `600`: el comodín permite suplantar cualquier host de un nivel bajo el dominio.

### Renovación

`systemd/acme-renew.{service,timer}` ejecutan `scripts/acme-renew.sh` (root) a diario. El
`--reloadcmd` guardado por acme.sh recarga `access_nginx` tras renovar. Instalar con:

```bash
INSTALL_SYSTEMD=1 scripts/deploy.sh      # instala access-ingress + acme-renew.timer
systemctl list-timers | grep acme
```

### nginx

Un `server` block por host con `include snippets/tls-win.conf` y un `*-body.conf` con las
directivas compartidas. nginx no elige entre dos certs del mismo tipo en un bloque, por eso no se
mezclan varios dominios en el mismo `server`.

### Apps de `ubuntu-docker`

Las apps se sirven también como `*.cortexdev.win`: `nginx_web` (192.168.0.87) tiene un `server`
block `.win` por app y usa un comodín Let's Encrypt en `certs/cortexdev.win/` de ese host,
**emitido y renovado allí** con `~/.acme.sh` (crontab 0/6/12/18). Es independiente de la capa de
acceso.

- Pi-hole resuelve el wildcard `address=/cortexdev.win/192.168.0.87` (apps) y los overrides de la
  capa de acceso a `192.168.0.49`; Tailscale tiene el split DNS `cortexdev.win`.
- Para añadir un vhost `.win` nuevo (proxy), en `ubuntu-docker`:
  `scripts/add-app-win-vhost.sh <host>.cortexdev.win <proxy_pass>` (atajo sin args:
  pma → `http://phpmyadmin:80`). Escribe el vhost con el cert `.win`, valida, recarga y verifica.
- Legado `.lan`: el `cortexdev-lan.conf` del nginx de apps quedó inerte al retirar el DNS.
  Para desactivarlo por completo: `scripts/apps-retire-lan.sh` (reporte) y luego
  `--apply` (`[--fix-env] [--purge-certs]`) en ubuntu-docker.

## Portal dinámico (catálogo + estado + estadísticas)

`portal-api` (contenedor `portal_api`, FastAPI + SQLite) comprueba el catálogo y calcula
agregados. Las páginas se generan en build desde `backend/catalog.yml`; no hay listas
hardcodeadas en HTML.

- Catálogo: `backend/catalog.yml` es la fuente de verdad. Al arrancar (y en cada
  `scripts/deploy.sh`) se sincroniza por `slug` con `/data/portal.db`; los servicios que se
  quitan del YAML se desactivan (no se borra el histórico).
- Build: `scripts/build-portal.py` genera `portal/dist/` desde `backend/catalog.yml` y
  `portal/templates/`. `scripts/deploy.sh` construye antes de recrear nginx; si el build falla,
  no se despliega. `portal/dist/` está gitignored y no se edita a mano.
- Sondeos: cada `POLL_INTERVAL` (30s) `portal_api` hace GET contra la URL pública de cada
  servicio (`probe_url` opcional), sin seguir redirecciones, con timeout de `PROBE_TIMEOUT`.
  Un código en `accept` es online (2xx) o login (3xx/401/403); timeout, error o 5xx = offline.
  Los Netdata usan `/api/v1/info` para no cargar el dashboard.
- Estado: `state` es el estado comprometido con histéresis (3 fallos para caer, 2 éxitos para
  recuperar) y es el que dispara los avisos. `last_state` es el último sondeo crudo.
- API (bajo `index.cortexdev.win/api/`, bind local `127.0.0.1:8088`):
  - `GET /api/health` → BD, `last_run`, `last_error` y versión de esquema.
  - `GET /api/catalog` → catálogo saneado con ETag (sin estado).
  - `GET /api/status` → estados, `summary`, `refresh_ms`, `next_check_at` y serie horaria.
  - `GET /api/stats?window=24h|7d|30d` → disponibilidad, cobertura, latencia, incidentes y serie.
  - `GET /api/incidents?window=7d&limit=50` → histórico de incidentes.
  - `POST /api/refresh` → fuerza una tanda; los `GET` no lanzan sondeos.
  - `GET /api/portal` → composición temporal, deprecada.
- Histórico: `checks` con retención de 7 días (`RETENTION_DAYS`); `checks_hourly` con 90 días;
  `incidents` guarda caídas y MTTR. La ventana de 24 h se muestra como `uptime_24h_pct` (0–100).
  `availability_pct` se pondera por checks, `coverage_pct` mide servicios con datos, `p95` es una
  muestra reciente (no la ventana completa) y el MTTR empieza cuando la caída se compromete
  (3 fallos), no en el primer fallo.
- Frontend: `portal/render.js` consume `/api/status`; en modo `static` no pide catálogo, actualiza
  pills/contadores y pinta los KPI. Si `portal_api` cae, conserva el HTML y muestra un aviso.
- Tema: Bootstrap 5.3.3 **vendoreado** en `portal/vendor/bootstrap/` (sin CDN ni npm; el portal
  es autocontenido para la LAN/Tailscale). `portal/theme.js` fija `data-theme` y `data-bs-theme`
  en `<html>` a partir de `localStorage['cortexdev-theme']` (por defecto oscuro), y `app.js`
  inyecta el botón de tema que alterna claro/oscuro y lo persiste.
- Añadir/editar servicios: editar `backend/catalog.yml` y ejecutar `scripts/deploy.sh`
  (idempotente). No hay que tocar los HTML.

Variables de `portal_api` (compose): `DB_PATH`, `CATALOG_PATH`, `POLL_INTERVAL`, `PROBE_TIMEOUT`,
`RETENTION_DAYS`, `PORTAL_DISABLE_POLL`. La verificación TLS usa las CAs del sistema
(Let's Encrypt). El breakpoint móvil real de `portal/style.css` es 820 px.

## Copias de seguridad (`backupcsr/`)

Las copias se ejecutan en **esta máquina** (`ubuntu-services`), no en `ubuntu-docker`: cada job
espeja un origen remoto (FTP/SFTP) directo a `/mnt/nas` (NAS `//192.168.0.179/Backups`).

```bash
sudo backupcsr/install.sh       # instala /opt/backupcsr, /etc/backupcsr, cron, logrotate y el NAS
sudo validar-copias             # 0=OK, 1=advertencias, 2=errores
```

`scripts/setup-services.sh` puede instalarlo con `INSTALL_BACKUPCSR=1`. Detalle (jobs, horarios,
secretos y corte desde `ubuntu-docker`) en [`backupcsr/README.md`](backupcsr/README.md).
`backups.cortexdev.win` sigue siendo el Proxmox Backup Server; son cosas distintas.

### Portal de administración (`copias.cortexdev.win`)

`backupcsr/web/` añade un portal web (Bootstrap 5, app-shell con barra lateral) para ver el estado,
el **tamaño (GB) almacenado por tarea** con su tendencia, ejecutar/reintentar jobs, consultar el
historial (con export CSV/JSON), navegar los archivos de cada copia, y —con la gestión activada—
editar el horario con presets y previsualización. Incluye usuarios (`admin`/`viewer`), auditoría y
alertas (jobs vencidos/fallidos, NAS lleno). El panel usa **una sola barra de acciones** con
selección de fila (estilo DataTables *Buttons*) e iconos **Font Awesome 6.5.2** vendoreados; los
gráficos son SVG propios embebidos y el tema es claro por defecto con toggle oscuro (sin CDN ni
build). La API corre nativa por systemd en `127.0.0.1:8089` (necesita root para el NAS/logs/jobs) y
nginx la publica:

```bash
sudo backupcsr/web/install.sh     # venv, systemd, esquema y admin
docker compose up -d --force-recreate ingress && scripts/dns-overrides.sh
```

Detalle en [`backupcsr/web/README.md`](backupcsr/web/README.md).
## Grafana (métricas de los 4 servidores)

Grafana central en ubuntu-services muestra CPU/RAM/disco/red/carga/uptime y temperatura de CPU,
reutilizando el **Netdata que ya corre en cada host** (no se instalan agentes nuevos):

```
navegador → https://grafana.cortexdev.win (nginx .49:443)
          → Grafana 127.0.0.1:3000 → Prometheus 127.0.0.1:9090
          → scrape http://<host>:19999/api/v1/allmetrics (Netdata)
```

- Hosts: `ubuntu-services` .49, `ubuntu-docker` .87, `proxmox-ve` .224, `proxmox-backups` .166.
- Prometheus y Grafana usan `network_mode: host` y bindean solo a `127.0.0.1` (3000/9090); se
  acceden por HTTPS a través de `grafana.cortexdev.win`.
- Credenciales: `.env` (gitignored). `scripts/deploy.sh` y `scripts/setup-services.sh` lo crean a
  partir de `.env.example` con una clave aleatoria si no existe (usuario por defecto `admin`).
- Retención de Prometheus: 30d. Volúmenes nombrados `prometheus_data` y `grafana_data`.
- Temperatura de CPU (panel "Temperatura CPU"): sale de los sensores `hwmon` de Netdata
  (`netdata_system_hw_sensor_temperature_input_*`, drivers `k10temp`/`coretemp`/`zenpower`...).
  Solo la publican los hosts con sensores accesibles: hoy únicamente `proxmox-ve` (`k10temp`,
  `Tctl`); las VM invitadas no exponen sensor de CPU.
- Uso real de CPU/RAM: los paneles "CPU usada %" y "RAM real usada %" calculan el uso por host, y el
  stat "RAM real usada (MB)" muestra el valor absoluto en MB (unidad `mbytes`, 1024-based, estilo
  Windows). La RAM real usada es `total − disponible`, usando `netdata_mem_available_MiB_average`
  (`MemAvailable`): descuenta la caché reclamable y el ARC de ZFS, por eso difiere del `used` de
  "RAM por estado (desglose)".
- Aparece en el portal como `Grafana` (`backend/catalog.yml`, health check a `/api/health`).
- Si añades/cambias `server` blocks (p. ej. `grafana.cortexdev.win`), `docker compose up -d` no
  recarga nginx: ejecuta `docker exec access_nginx nginx -s reload` o usa `scripts/deploy.sh`.

Personalizar:

- Añadir/quitar servidores: editar `monitoring/prometheus/prometheus.yml` (`static_configs`) y
  `docker compose up -d prometheus` (o recargar con `curl -X POST http://127.0.0.1:9090/-/reload`).
- Dashboard: `monitoring/grafana/dashboards/cortexdev-servers.json` (provisionado, se recarga solo).
- Alertas: `monitoring/prometheus/rules/nodes.yml` (CPU y raíz), visibles en `/alerts`. No hay
  Alertmanager.

Verificar:

```bash
curl -fsS http://127.0.0.1:9090/-/ready
curl -fsS http://127.0.0.1:3000/api/health
curl -fsS http://127.0.0.1:9090/api/v1/targets | jq -r '.data.activeTargets[] | "\(.labels.host) \(.health)"'
```

Si algún target sale `down`, el Netdata de ese host no expone `:19999` desde .49
(`scripts/diagnose-host.sh <ip> 19999`).

## Seguridad

- `certs/` está en `.gitignore`: **nunca** subir `.pem`/llaves privadas, ni siquiera en un repo
  privado.
- La llave del comodín público `certs/cortexdev.win/key.pem` (permisos `600`) permite suplantar
  cualquier host de un nivel bajo `cortexdev.win`. No versionarla y, si se filtra, revocar y
  reemitir con `scripts/install-win-cert.sh --issue`.
- El repo debe ser **privado**.
