#!/usr/bin/env bash
#
# Fase 4: retira el legado *.cortexdev.lan del nginx de apps (ubuntu-docker).
# Se ejecuta EN ubuntu-docker: scripts/apps-retire-lan.sh [--apply] [--fix-env] [--purge-certs]
#
# Sin flags solo REPORTA (no cambia nada). Con --apply:
#   - desactiva cortexdev-lan.conf (rename a *.disabled + backup)
#   - nginx -t, reload y verifica que los vhosts .win siguen sirviendo; si falla, restaura
#   - con --fix-env: pasa cortexdev.lan -> cortexdev.win en configs de apps (con backup)
#   - con --purge-certs: desactiva certs/cortexdev.lan/ del nginx de apps
#
# Variables:
#   APPS_NGINX_DIR     base del nginx de apps (default /home/christian/dev/nginx)
#   NGINX_CONTAINER    contenedor nginx (default nginx_web)
#   SEARCH_DIRS        dirs de apps a revisar con --fix-env (default /home/christian/dev)
#   ALLOW_OTHER_HOST=1 permite ejecutar fuera de 192.168.0.87
#
set -uo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APPS_NGINX_DIR="${APPS_NGINX_DIR:-/home/christian/dev/nginx}"
CONF_DIR="$APPS_NGINX_DIR/conf.d"
CERTS_DIR="$APPS_NGINX_DIR/certs"
NGINX_CONTAINER="${NGINX_CONTAINER:-nginx_web}"
SEARCH_DIRS="${SEARCH_DIRS:-/home/christian/dev}"

APPLY=0
FIX_ENV=0
PURGE_CERTS=0
FAIL=0
for arg in "$@"; do
  case "$arg" in
    --apply)       APPLY=1 ;;
    --fix-env)     FIX_ENV=1 ;;
    --purge-certs) PURGE_CERTS=1 ;;
    -h|--help)
      sed -n '2,18p' "$0" | sed 's/^# \{0,1\}//'
      exit 0 ;;
    *) printf 'Opcion desconocida: %s (usa --help)\n' "$arg" >&2; exit 2 ;;
  esac
done

info() { printf '\n\033[1;34m==>\033[0m %s\n' "$*"; }
ok()   { printf '  \033[1;32mOK\033[0m   %s\n' "$*"; }
bad()  { printf '  \033[1;31mFAIL\033[0m %s\n' "$*"; FAIL=1; }
warn() { printf '  \033[1;33mWARN\033[0m %s\n' "$*"; }

if [ "${ALLOW_OTHER_HOST:-0}" != "1" ] && ! hostname -I 2>/dev/null | grep -q '192\.168\.0\.87'; then
  printf '\033[1;31mERROR\033[0m este script corre en ubuntu-docker (192.168.0.87); usa ALLOW_OTHER_HOST=1 para forzar\n' >&2
  exit 1
fi

if [ "$APPLY" != "1" ]; then
  printf '\033[1;33mMODO REPORTE\033[0m (no cambia nada). Usa --apply para aplicar.\n'
fi

info "1/5 Entorno"
command -v docker >/dev/null || { bad "docker no esta instalado"; exit 1; }
[ -d "$CONF_DIR" ] || { bad "no existe $CONF_DIR (ajusta APPS_NGINX_DIR)"; exit 1; }
ok "conf.d: $CONF_DIR"
st="$(docker inspect --format '{{.State.Status}}' "$NGINX_CONTAINER" 2>/dev/null || true)"
[ "$st" = "running" ] || { bad "$NGINX_CONTAINER no esta running (${st:-inexistente})"; exit 1; }
ok "$NGINX_CONTAINER running"

LAN_CONF="$CONF_DIR/cortexdev-lan.conf"

info "2/5 Inventario de restos .lan"
if [ -f "$LAN_CONF" ]; then
  ok "existe cortexdev-lan.conf ($(grep -c 'cortexdev\.lan' "$LAN_CONF" || true) referencias .lan)"
else
  ok "cortexdev-lan.conf ya no existe"
fi
other="$(grep -rln 'cortexdev\.lan' "$CONF_DIR" 2>/dev/null | grep -v 'cortexdev-lan.conf' || true)"
if [ -n "$other" ]; then
  warn "otros .conf con .lan:"; printf '%s\n' "$other" | sed 's/^/       /'
else
  ok "ningun otro .conf con .lan"
fi
if [ -d "$CERTS_DIR/cortexdev.lan" ]; then
  warn "certs .lan presentes en $CERTS_DIR/cortexdev.lan"
else
  ok "sin certs .lan en el nginx de apps"
fi

env_hits=""
for d in $SEARCH_DIRS; do
  [ -d "$d" ] || continue
  h="$(grep -rIl 'cortexdev\.lan' "$d" \
    --include='.env' --include='.env.*' --include='*.env' \
    --include='docker-compose*.yml' --include='docker-compose*.yaml' \
    --include='*.conf' --include='*.properties' --include='*.ini' \
    --include='environment*.ts' --include='*.env.ts' \
    --exclude-dir=nginx --exclude-dir=node_modules --exclude-dir=.git \
    --exclude-dir=vendor --exclude-dir=storage --exclude-dir=dist \
    2>/dev/null || true)"
  [ -n "$h" ] && env_hits="$env_hits
$h"
done
env_hits="$(printf '%s\n' "$env_hits" | grep -v '^$' || true)"
if [ -n "$env_hits" ]; then
  warn "configs de apps con .lan:"; printf '%s\n' "$env_hits" | sed 's/^/       /'
else
  ok "sin .lan en configs de apps"
fi

# Sin --apply termina aqui el reporte.
if [ "$APPLY" != "1" ]; then
  info "Reporte"
  printf '  Para aplicar: scripts/apps-retire-lan.sh --apply [--fix-env] [--purge-certs]\n'
  exit "$FAIL"
fi

info "3/5 Desactivando cortexdev-lan.conf"
if [ -f "$LAN_CONF" ]; then
  ts="$(date +%Y%m%d-%H%M%S)"
  if cp "$LAN_CONF" "$LAN_CONF.disabled-$ts"; then
    ok "backup: $LAN_CONF.disabled-$ts"
  else
    bad "no pude hacer backup de cortexdev-lan.conf"; exit 1
  fi
  if mv "$LAN_CONF" "$LAN_CONF.disabled"; then
    ok "renombrado a cortexdev-lan.conf.disabled (nginx ya no lo carga)"
  else
    bad "no pude renombrar cortexdev-lan.conf"; exit 1
  fi

  if docker exec "$NGINX_CONTAINER" nginx -t >/dev/null 2>&1; then
    ok "nginx -t ok"
    if docker exec "$NGINX_CONTAINER" nginx -s reload >/dev/null 2>&1; then
      ok "nginx recargado"
    else
      bad "no pude recargar; restaurando cortexdev-lan.conf"
      mv "$LAN_CONF.disabled" "$LAN_CONF"
      exit 1
    fi
  else
    bad "nginx -t fallo; restaurando cortexdev-lan.conf"
    docker exec "$NGINX_CONTAINER" nginx -t 2>&1 | sed 's/^/       /' || true
    mv "$LAN_CONF.disabled" "$LAN_CONF"
    docker exec "$NGINX_CONTAINER" nginx -s reload >/dev/null 2>&1 || true
    exit 1
  fi
else
  ok "nada que desactivar"
fi

info "4/5 Verificando vhosts .win"
code_app() { curl -sS -o /dev/null -w '%{http_code}' --resolve "$1:443:127.0.0.1" "https://$1/" 2>/dev/null || true; }
for h in apps.cortexdev.win admin-portal-pacientes.cortexdev.win pma.cortexdev.win \
         pihole.cortexdev.win proxmox.cortexdev.win netdata-services.cortexdev.win; do
  c="$(code_app "$h")"
  if [ -n "$c" ] && [ "$c" != "000" ]; then
    ok "$h -> $c"
  else
    bad "$h -> ${c:-000}"
  fi
done
lan_names="$(docker exec "$NGINX_CONTAINER" nginx -T 2>/dev/null | grep -E 'server_name' | grep -c 'cortexdev\.lan' || true)"
if [ "$lan_names" = "0" ]; then
  ok "sin server_name .lan en la config cargada"
else
  bad "$lan_names server_name .lan siguen cargados"
fi
[ "$FAIL" = "0" ] || warn "hubo fallos; revisa antes de seguir"

info "5/5 Extras"
if [ "$FIX_ENV" = "1" ]; then
  if [ -z "$env_hits" ]; then
    ok "no hay configs de apps que corregir"
  else
    while IFS= read -r f; do
      [ -n "$f" ] || continue
      b="$f.bak-lan-$(date +%Y%m%d-%H%M%S)"
      if cp "$f" "$b"; then
        sed -i 's/cortexdev\.lan/cortexdev.win/g' "$f"
        ok "corregido $f (backup $b) -> reinicia el servicio de esa app"
      else
        bad "no pude respaldar $f"
      fi
    done <<< "$env_hits"
  fi
else
  warn "--fix-env no activado; las configs con .lan siguen como estan (listadas arriba)"
fi

if [ "$PURGE_CERTS" = "1" ]; then
  if [ -d "$CERTS_DIR/cortexdev.lan" ]; then
    ts="$(date +%Y%m%d-%H%M%S)"
    if mv "$CERTS_DIR/cortexdev.lan" "$CERTS_DIR/cortexdev.lan.disabled-$ts"; then
      ok "certs .lan desactivados: cortexdev.lan.disabled-$ts"
    else
      bad "no pude mover $CERTS_DIR/cortexdev.lan"
    fi
  else
    ok "sin certs .lan que purgar"
  fi
else
  warn "--purge-certs no activado; certs/cortexdev.lan sigue en disco"
fi

echo
if [ "$FAIL" = "0" ]; then
  printf '\033[1;32mFase 4 aplicada\033[0m\n'
else
  printf '\033[1;31mFase 4 con fallos\033[0m\n'; exit 1
fi
