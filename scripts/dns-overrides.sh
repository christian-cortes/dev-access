#!/usr/bin/env bash
#
# Aplica los overrides DNS de la capa de acceso en Pi-hole (idempotente).
# Se ejecuta EN ubuntu-services: scripts/dns-overrides.sh
#
# Gestiona SOLO *.cortexdev.win: wildcard a las apps + overrides de la capa de
# acceso (dnsmasq usa la coincidencia mas especifica: el host gana al wildcard).
# Ademas PURGA de misc.dnsmasq_lines cualquier linea *.cortexdev.lan (Fase 3) y
# los overrides de Uptime Kuma (uptime/kuma), servicio retirado.
#
# Variables:
#   PIHOLE     nombre del contenedor de Pi-hole (solo modo docker; autodetectado si se omite)
#   HOSTS      hosts de la capa de acceso separados por espacios
#   ACCESS_IP  IP de la capa de acceso (default 192.168.0.49)
#   APPS_IP    IP de las apps (default 192.168.0.87)
#
set -uo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ACCESS_IP="${ACCESS_IP:-192.168.0.49}"
APPS_IP="${APPS_IP:-192.168.0.87}"
HOSTS="${HOSTS:-index copias ups pihole proxmox backups pbs grafana netdata-services netdata-backups netdata-proxmox netdata-docker}"
PUB_DOMAIN="cortexdev.win"
WILDCARD_PUB="address=/${PUB_DOMAIN}/${APPS_IP}"

FAIL=0
info() { printf '\n\033[1;34m==>\033[0m %s\n' "$*"; }
ok()   { printf '  \033[1;32mOK\033[0m   %s\n' "$*"; }
bad()  { printf '  \033[1;31mFAIL\033[0m %s\n' "$*"; FAIL=1; }

cd "$REPO_DIR"

info "1/4 Detectando Pi-hole"
PIHOLE_KIND=""
PIHOLE=""
if systemctl is-active --quiet pihole-FTL 2>/dev/null || command -v pihole-FTL >/dev/null 2>&1; then
  PIHOLE_KIND="native"
else
  PIHOLE="$(docker ps --format '{{.Names}}' 2>/dev/null | grep -i pihole | head -n1 || true)"
  [ -n "$PIHOLE" ] && PIHOLE_KIND="docker"
fi

if [ -z "$PIHOLE_KIND" ]; then
  bad "no detecte Pi-hole (ni servicio pihole-FTL ni contenedor pihole)"
  exit 1
fi
ok "Pi-hole $PIHOLE_KIND${PIHOLE:+ (contenedor $PIHOLE)}"

ftl() {
  if [ "$PIHOLE_KIND" = "native" ]; then
    sudo pihole-FTL "$@"
  else
    docker exec "$PIHOLE" pihole-FTL "$@"
  fi
}

reload_dns() {
  if [ "$PIHOLE_KIND" = "native" ]; then
    sudo pihole reloaddns || sudo systemctl restart pihole-FTL
  else
    docker exec "$PIHOLE" pihole reloaddns || docker restart "$PIHOLE" >/dev/null
  fi
}

info "2/4 Leyendo misc.dnsmasq_lines"
cur="$(ftl --config misc.dnsmasq_lines 2>/dev/null || true)"
if [ -z "$cur" ]; then
  bad "no pude leer misc.dnsmasq_lines (revisa pihole-FTL o permisos sudo)"
  exit 1
fi
ok "configuracion leida"

existing="$(printf '%s\n' "$cur" | grep -oE '"[^"]*"' | sed 's/^"//; s/"$//' || true)"
if [ -z "$existing" ]; then
  existing="$(printf '%s\n' "$cur" | sed 's/[][]//g' | tr ',' '\n' | grep -oE '[a-zA-Z_-]+=[^[:space:]]+' || true)"
fi

# Fase 3: descartar cualquier linea .lan (wildcard y overrides), conservando el resto.
lan_count="$(printf '%s\n' "$existing" | grep -c 'cortexdev\.lan' || true)"
existing="$(printf '%s\n' "$existing" | grep -v 'cortexdev\.lan' || true)"
[ "$lan_count" = "0" ] && ok "sin lineas .lan que purgar" || ok "purgando $lan_count linea(s) .lan"

# Uptime Kuma retirado: descartar sus overrides si siguen presentes.
kuma_count="$(printf '%s\n' "$existing" | grep -cE 'address=/(uptime|kuma)\.cortexdev\.win/' || true)"
existing="$(printf '%s\n' "$existing" | grep -vE 'address=/(uptime|kuma)\.cortexdev\.win/' || true)"
[ "$kuma_count" = "0" ] && ok "sin overrides de Uptime Kuma que purgar" || ok "purgando $kuma_count override(s) de Uptime Kuma"

declare -A seen=()
lines=()
add() {
  [ -n "$1" ] || return 0
  [ -n "${seen[$1]:-}" ] && return 0
  seen[$1]=1
  lines+=("$1")
}

add "$WILDCARD_PUB"
while IFS= read -r l; do add "$l"; done <<< "$existing"
for h in $HOSTS; do
  add "address=/${h}.${PUB_DOMAIN}/${ACCESS_IP}"
done

json="["
sep=""
for l in "${lines[@]}"; do
  json+="${sep}\"${l}\""
  sep=","
done
json+="]"

info "3/4 Escribiendo overrides"
if [ "$PIHOLE_KIND" = "native" ]; then
  bak="/etc/pihole/pihole.toml.bak-$(date +%Y%m%d-%H%M%S)"
  if sudo cp /etc/pihole/pihole.toml "$bak"; then
    ok "backup en $bak"
  else
    bad "no se pudo hacer backup de /etc/pihole/pihole.toml"
  fi
fi
printf '  valor previo: %s\n' "$cur"

if ftl --config misc.dnsmasq_lines "$json"; then
  ok "misc.dnsmasq_lines actualizado"
else
  bad "no se pudo escribir misc.dnsmasq_lines"
  exit 1
fi

if reload_dns; then
  sleep 1
  ok "DNS recargado"
else
  bad "no se pudo recargar Pi-hole"
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

info "4/4 Verificacion (dig @127.0.0.1)"
if ! command -v dig >/dev/null; then
  bad "dig no instalado; no se pudo verificar"
else
  for h in $HOSTS; do
    r="$(dig_answer "${h}.${PUB_DOMAIN}")"
    if [ "$r" = "$ACCESS_IP" ]; then
      ok "${h}.${PUB_DOMAIN} -> $ACCESS_IP"
    else
      bad "${h}.${PUB_DOMAIN} -> ${r:-sin respuesta}"
    fi
  done
  r="$(dig_answer "app-admin.${PUB_DOMAIN}")"
  if [ "$r" = "$APPS_IP" ]; then
    ok "app-admin.${PUB_DOMAIN} -> $APPS_IP (wildcard apps)"
  else
    bad "app-admin.${PUB_DOMAIN} -> ${r:-sin respuesta} (wildcard roto)"
  fi

  # Fase 3: .lan ya no debe resolver
  r="$(dig_answer "index.cortexdev.lan")"
  if [ -z "$r" ]; then
    ok "index.cortexdev.lan sin respuesta (purga .lan OK)"
  else
    bad "index.cortexdev.lan todavia resuelve a $r"
  fi
fi

if [ "$FAIL" != "0" ]; then
  printf '\n\033[1;31mFallaron los overrides DNS\033[0m. Valor actual de misc.dnsmasq_lines:\n'
  ftl --config misc.dnsmasq_lines 2>/dev/null || true
  exit 1
fi
printf '\n\033[1;32mOverrides DNS aplicados (.win, .lan purgado)\033[0m\n'
