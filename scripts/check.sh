#!/usr/bin/env bash
#
# Valida la capa de acceso en ubuntu-services (192.168.0.49).
# Se ejecuta EN ubuntu-services: scripts/check.sh
#
set -uo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
FAIL=0

cd "$REPO_DIR"

ok()   { printf '  \033[1;32mOK\033[0m   %s\n' "$*"; }
bad()  { printf '  \033[1;31mFAIL\033[0m %s\n' "$*"; FAIL=1; }
info() { printf '\n\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '  \033[1;33mWARN\033[0m %s\n' "$*"; }

if ! hostname -I 2>/dev/null | grep -q '192\.168\.0\.49'; then
  warn "esta maquina no tiene la IP 192.168.0.49: check.sh valida ubuntu-services, no ubuntu-docker (.87)"
fi

info "Certificados"
for f in fullchain.pem key.pem; do
  if [ -f "$REPO_DIR/certs/cortexdev.win/$f" ]; then
    ok "cortexdev.win/$f"
  else
    bad "falta certs/cortexdev.win/$f (corre scripts/install-win-cert.sh)"
  fi
done
win_cert="$REPO_DIR/certs/cortexdev.win/fullchain.pem"
if [ -f "$win_cert" ]; then
  win_iss="$(openssl x509 -in "$win_cert" -noout -issuer 2>/dev/null | sed 's/^issuer=//')"
  win_sub="$(openssl x509 -in "$win_cert" -noout -subject 2>/dev/null | sed 's/^subject=//')"
  if [ -n "$win_iss" ] && [ "$win_iss" = "$win_sub" ]; then
    warn "cortexdev.win/fullchain.pem es provisional autofirmado; emite con scripts/install-win-cert.sh --issue"
  else
    ok "cortexdev.win/fullchain.pem emitido por CA ($win_iss)"
  fi
fi

info "Contenedores"
for c in access_nginx portal_api prometheus grafana; do
  status="$(docker inspect --format '{{.State.Status}}' "$c" 2>/dev/null || true)"
  if [ "$status" = "running" ]; then
    ok "$c running"
  else
    bad "$c no esta corriendo (docker compose up -d --build)"
  fi
done

# backupcsr-web corre nativo (systemd) porque necesita root para el NAS y los jobs.
if systemctl is-active --quiet backupcsr-web 2>/dev/null; then
  ok "backupcsr-web active (systemd)"
else
  warn "backupcsr-web no está activo (backupcsr/web/install.sh; es opcional)"
fi

# HTTP contra nginx local por loopback, sin depender del DNS y sin -k (valida el cert).
code() {
  curl -sS -o /dev/null -w '%{http_code}' --resolve "$1:443:127.0.0.1" "https://$1$2" 2>/dev/null || true
}

info "HTTP acceso (.win, cert publico sin -k)"
c="$(code index.cortexdev.win /)"
if [ "$c" = "200" ] || [ "$c" = "302" ]; then
  ok "index.cortexdev.win / -> $c (SSO login unico)"
else
  bad "index.cortexdev.win / -> ${c:-000}"
  echo "       Si es 403/404, Pi-hole sigue ocupando el 443:"
  echo "         - compose con puertos: usa 'docker compose up -d' (recrear, no 'restart')"
  echo "         - Pi-hole con network_mode: host: cambia 'webserver.port' (p. ej. 8080o,8443s) en pihole.toml"
  echo "       Si es 000 estando access_nginx 'running':"
  echo "       --- NetworkMode / mounts ---"
  docker inspect access_nginx --format '  NetworkMode={{.HostConfig.NetworkMode}}' 2>/dev/null || true
  docker inspect access_nginx --format '{{range .Mounts}}  mount {{.Source}} -> {{.Destination}}{{println}}{{end}}' 2>/dev/null || true
  echo "       --- git status (repo) ---"
  git status --short 2>/dev/null || true
  echo "       --- ls -la nginx/conf.d (host) ---"
  ls -la nginx/conf.d 2>/dev/null || true
  echo "       --- docker exec access_nginx ls -la /etc/nginx/conf.d ---"
  docker exec access_nginx ls -la /etc/nginx/conf.d 2>/dev/null || true
  echo "       --- listen/server_name de la config cargada (nginx -T) ---"
  docker exec access_nginx nginx -T 2>&1 | grep -E 'nginx:|listen|server_name' || true
  echo "       --- docker compose logs --tail 15 ingress ---"
  docker compose logs --tail 15 ingress 2>/dev/null || true
fi

c="$(code index.cortexdev.win /laravel.html)"
if [ "$c" = "200" ] || [ "$c" = "302" ]; then
  ok "index.cortexdev.win /laravel.html -> $c (SSO login unico)"
else
  bad "index.cortexdev.win /laravel.html -> ${c:-000}"
fi

# Portal de copias (nginx -> backupcsr-web 127.0.0.1:8089).
c="$(code copias.cortexdev.win /)"
if [ "$c" = "200" ] || [ "$c" = "302" ]; then
  ok "copias.cortexdev.win / -> $c (SSO login unico)"
else
  bad "copias.cortexdev.win / -> ${c:-000} (docker exec access_nginx nginx -s reload; systemctl status backupcsr-web)"
fi

# /api/health queda fuera del SSO; reintenta por si el servicio acaba de reiniciar.
c=""
for _ in 1 2 3 4 5 6; do
  c="$(code copias.cortexdev.win /api/health)"
  [ "$c" = "200" ] && break
  sleep 3
done
if [ "$c" = "200" ]; then
  ok "copias.cortexdev.win /api/health -> 200"
else
  bad "copias.cortexdev.win /api/health -> ${c:-000} (systemctl status backupcsr-web)"
fi

c="$(code grafana.cortexdev.win /login)"
if [ "$c" = "200" ]; then
  ok "grafana.cortexdev.win /login -> 200"
elif [ "$c" = "404" ]; then
  bad "grafana.cortexdev.win /login -> 404 (nginx no ha recargado: docker exec access_nginx nginx -s reload)"
else
  bad "grafana.cortexdev.win /login -> ${c:-000} (docker compose logs --tail 40 grafana)"
fi

info "Monitoring (Prometheus/Grafana, loopback)"

# Tras 'docker compose up' Grafana migra la BD y Prometheus hace su primer
# scrape pasados hasta 30s; reintenta antes de dar FAIL.
http_code() {
  local url="$1" out=""
  for _ in 1 2 3 4 5; do
    out="$(curl -s -o /dev/null -w '%{http_code}' "$url" 2>/dev/null || true)"
    [ "$out" = "200" ] && break
    sleep 3
  done
  printf '%s' "$out"
}

c="$(http_code http://127.0.0.1:9090/-/ready)"
if [ "$c" = "200" ]; then
  ok "Prometheus /-/ready -> 200"
else
  bad "Prometheus /-/ready -> ${c:-000}"
fi

c="$(http_code http://127.0.0.1:3000/api/health)"
if [ "$c" = "200" ]; then
  ok "Grafana /api/health -> 200"
else
  bad "Grafana /api/health -> ${c:-000}"
fi

if command -v jq >/dev/null; then
  n=0
  total=0
  for _ in 1 2 3 4 5 6 7 8 9 10; do
    targets="$(curl -s 'http://127.0.0.1:9090/api/v1/targets?state=active' 2>/dev/null || true)"
    total="$(printf '%s' "$targets" | grep -o '"scrapePool":"netdata"' | wc -l)"
    n="$(printf '%s' "$targets" | grep -o '"health":"up"' | wc -l)"
    [ "$total" -ge 1 ] && [ "$n" -eq "$total" ] && break
    sleep 3
  done
  if [ "$total" -ge 1 ] && [ "$n" -eq "$total" ]; then
    ok "Prometheus targets up: $n/$total"
  else
    bad "Prometheus targets up: $n/$total (los Netdata deben exponer :19999; scripts/diagnose-host.sh <ip> 19999)"
  fi
fi

# Apps de ubuntu-docker (192.168.0.87): el certificado lo sirve nginx_web.
# Se acepta cualquier codigo HTTP != 000 (000 = fallo de TLS/hostname).
info "HTTP apps .win (TLS publico sin -k)"
code_app() {
  curl -sS -o /dev/null -w '%{http_code}' --resolve "$1:443:192.168.0.87" "https://$1/" 2>/dev/null || true
}
for h in apps.cortexdev.win admin-portal-pacientes.cortexdev.win portal-pacientes.cortexdev.win \
         centro-apoyo.cortexdev.win bot-gomedisys.cortexdev.win analisis-datos.cortexdev.win pma.cortexdev.win; do
  c="$(code_app "$h")"
  if [ -n "$c" ] && [ "$c" != "000" ]; then
    ok "$h -> $c (TLS valido)"
  else
    bad "$h -> ${c:-000} (TLS/HTTP)"
  fi
done

info "API del portal (portal-api)"
c="$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8088/api/health || true)"
if [ "$c" = "200" ]; then
  ok "GET /api/health -> 200"
else
  bad "GET /api/health -> ${c:-000} (docker logs --tail 40 portal_api)"
fi

# El catálogo del portal ahora requiere sesión (SSO) si se pide por nginx; se
# consulta portal-api directamente en loopback, que no aplica auth_request.
body="$(curl -s http://127.0.0.1:8088/api/portal 2>/dev/null || true)"
if printf '%s' "$body" | grep -q '"services"'; then
  n="$(printf '%s' "$body" | grep -o '"category"' | wc -l)"
  ok "GET /api/portal -> catalogo ($n servicios)"
else
  bad "GET /api/portal sin catalogo (docker logs --tail 40 portal_api)"
fi

dig_answer() {
  local name="$1" out=""
  for _ in 1 2 3 4; do
    out="$(dig +short "$name" @127.0.0.1 +time=2 +tries=1 2>/dev/null || true)"
    [ -n "$out" ] && break
    sleep 1
  done
  printf '%s' "$out"
}

info "DNS (Pi-hole local, solo .win)"
if command -v dig >/dev/null; then
  for h in index copias ups pihole proxmox backups pbs uptime kuma grafana netdata-services netdata-backups netdata-proxmox netdata-docker; do
    r="$(dig_answer "$h.cortexdev.win")"
    if [ "$r" = "192.168.0.49" ]; then
      ok "$h.cortexdev.win -> 192.168.0.49"
    else
      bad "$h.cortexdev.win -> ${r:-sin respuesta} (falta el override en misc.dnsmasq_lines)"
    fi
  done

  for a in app-admin apps; do
    r="$(dig_answer "$a.cortexdev.win")"
    if [ "$r" = "192.168.0.87" ]; then
      ok "$a.cortexdev.win -> 192.168.0.87 (wildcard apps)"
    else
      bad "$a.cortexdev.win -> ${r:-sin respuesta}"
    fi
  done

  # Fase 3: .lan no debe resolver
  r="$(dig_answer index.cortexdev.lan)"
  if [ -z "$r" ]; then
    ok "index.cortexdev.lan sin respuesta (retirado)"
  else
    bad "index.cortexdev.lan todavia resuelve a $r"
  fi
else
  bad "dig no instalado; no se pudo verificar DNS"
fi

info "Tailscale (acceso remoto a *.cortexdev.win)"
if command -v tailscale >/dev/null; then
  if [ -x "$REPO_DIR/scripts/tailscale-dns.sh" ]; then
    if "$REPO_DIR/scripts/tailscale-dns.sh"; then
      ok "acceso remoto Tailscale OK"
    else
      bad "acceso remoto Tailscale con fallos (detalle arriba)"
    fi
  else
    bad "falta scripts/tailscale-dns.sh (o no es ejecutable)"
  fi
else
  warn "tailscale no instalado; omito la comprobacion de acceso remoto"
fi

info "Puertos"
if command -v ss >/dev/null; then
  for p in 80 443 8080 8443 8088 8089 3000 9090; do
    found=0
    for _ in 1 2 3 4 5; do
      if ss -ltn 2>/dev/null | grep -q ":$p "; then found=1; break; fi
      sleep 2
    done
    if [ "$found" = "1" ]; then
      ok "puerto $p escuchando"
    else
      bad "nada escuchando en $p"
    fi
  done
  echo "     contenedores y puertos publicados:"
  docker ps --format '       {{.Names}} -> {{.Ports}}' 2>/dev/null || true
fi

echo
if [ "$FAIL" = "0" ]; then
  printf '\033[1;32mTodo OK\033[0m\n'
else
  printf '\033[1;31mHay fallos\033[0m\n'
  exit 1
fi
