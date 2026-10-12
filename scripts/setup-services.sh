#!/usr/bin/env bash
#
# Puesta en marcha de la capa de acceso en ubuntu-services (idempotente).
# Ejecutar EN ubuntu-services, sin sudo: scripts/setup-services.sh
#
# Variables:
#   PIHOLE      nombre del contenedor de Pi-hole (autodetectado si se omite)
#   PORT_HTTP   puerto host para HTTP  (default: 8080)
#   PORT_HTTPS  puerto host para HTTPS (default: 8443)
#   INSTALL_BACKUPCSR=1  instala backupcsr/install.sh al final (copias de seguridad)
#
set -uo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PORT_HTTP="${PORT_HTTP:-8080}"
PORT_HTTPS="${PORT_HTTPS:-8443}"
FAIL=0

info() { printf '\n\033[1;34m==>\033[0m %s\n' "$*"; }
ok()   { printf '  \033[1;32mOK\033[0m   %s\n' "$*"; }
bad()  { printf '  \033[1;31mFAIL\033[0m %s\n' "$*"; FAIL=1; }
port_open() { timeout 1 bash -c "</dev/tcp/127.0.0.1/$1" 2>/dev/null; }

cd "$REPO_DIR"

# Asegura .env (clave admin de Grafana) antes de levantar el compose.
if [ ! -f .env ] && [ -f .env.example ]; then
  cp .env.example .env
  if command -v openssl >/dev/null 2>&1; then
    pw="$(openssl rand -hex 16)"
  else
    pw="$(head -c 24 /dev/urandom | od -An -tx1 | tr -d ' \n')"
  fi
  sed -i "s|^GF_ADMIN_PASSWORD=.*|GF_ADMIN_PASSWORD=$pw|" .env
  chmod 600 .env
  printf 'Creado .env con GF_ADMIN_USER=%s y clave aleatoria\n' "${GF_ADMIN_USER:-admin}"
fi

if [ "${ALLOW_OTHER_HOST:-0}" != "1" ] && ! hostname -I 2>/dev/null | grep -q '192\.168\.0\.49'; then
  printf '\033[1;31mERROR\033[0m setup-services.sh debe correr en ubuntu-services (192.168.0.49); usa ALLOW_OTHER_HOST=1 para forzar\n' >&2
  exit 1
fi

info "1/5 Archivos y certificados"
for p in docker-compose.yml nginx/conf.d/index.conf nginx/conf.d/infra.conf nginx/conf.d/copias.conf nginx/conf.d/snippets/tls-win.conf portal/index.html; do
  if [ -e "$p" ]; then
    ok "$p"
  else
    bad "falta $p (repo incompleto en esta maquina)"
  fi
done

# nginx referencia el par .win; asegura un provisional autofirmado si aun no
# se emitio el de Let's Encrypt (scripts/install-win-cert.sh --issue).
if [ -x scripts/install-win-cert.sh ]; then
  scripts/install-win-cert.sh --ensure || bad "no pude asegurar el certificado *.cortexdev.win"
else
  bad "falta scripts/install-win-cert.sh (nginx no arrancara sin certs/cortexdev.win)"
fi

info "2/5 Pi-hole"
PIHOLE_KIND=""
PIHOLE=""
if systemctl is-active --quiet pihole-FTL 2>/dev/null || command -v pihole-FTL >/dev/null 2>&1; then
  PIHOLE_KIND="native"
else
  PIHOLE="$(docker ps --format '{{.Names}}' 2>/dev/null | grep -i pihole | head -n1 || true)"
  [ -n "$PIHOLE" ] && PIHOLE_KIND="docker"
fi

if [ -z "$PIHOLE_KIND" ]; then
  bad "no detecte Pi-hole en esta maquina (ni servicio pihole-FTL ni contenedor pihole)"
else
  ok "Pi-hole $PIHOLE_KIND${PIHOLE:+ (contenedor $PIHOLE)}"

  if port_open 443; then
    if [ "$PIHOLE_KIND" = "native" ]; then
      printf '  -> Pi-hole nativo en 80/443; muevo su UI a %s/%s (pide sudo)\n' "$PORT_HTTP" "$PORT_HTTPS"
      if sudo pihole-FTL --config webserver.port "${PORT_HTTP}o,${PORT_HTTPS}s"; then
        if sudo systemctl restart pihole-FTL; then
          ok "pihole-FTL reiniciado"
        else
          bad "no se pudo reiniciar pihole-FTL"
        fi
      else
        bad "no se pudo cambiar webserver.port; edita /etc/pihole/pihole.toml (webserver.port) y reinicia pihole-FTL"
      fi
    else
      mode="$(docker inspect "$PIHOLE" --format '{{.HostConfig.NetworkMode}}' 2>/dev/null || echo '?')"
      ports="$(docker inspect "$PIHOLE" --format '{{json .HostConfig.PortBindings}}' 2>/dev/null || echo '?')"
      cdir="$(docker inspect "$PIHOLE" --format '{{index .Config.Labels "com.docker.compose.project.working_dir"}}' 2>/dev/null || true)"
      printf '       NetworkMode=%s\n       PortBindings=%s\n       compose_dir=%s\n' "$mode" "$ports" "${cdir:-?}"

      if [ "$mode" = "host" ]; then
        printf '  -> Pi-hole usa network_mode host; muevo su UI a %s/%s\n' "$PORT_HTTP" "$PORT_HTTPS"
        if docker exec "$PIHOLE" pihole-FTL --config webserver.port "${PORT_HTTP}o,${PORT_HTTPS}s"; then
          docker restart "$PIHOLE" >/dev/null && ok "Pi-hole reiniciado" || bad "no se pudo reiniciar Pi-hole"
        else
          bad "no se pudo cambiar webserver.port (hazlo en Settings -> All settings -> Webserver -> port)"
        fi
      else
        bad "Pi-hole sigue publicando 80/443 en modo bridge"
        printf '       Edita %s/docker-compose.yml y cambia:\n' "${cdir:-<dir-del-compose>}"
        printf '         80:80   ->  %s:80\n' "$PORT_HTTP"
        printf '         443:443 ->  %s:443\n' "$PORT_HTTPS"
        printf '       Luego: cd %s && docker compose up -d pihole\n' "${cdir:-<dir-del-compose>}"
        printf '       (up -d recrea el contenedor; "docker compose restart" NO aplica puertos)\n'
      fi
    fi
  else
    ok "443 libre"
  fi
fi

info "3/5 Esperando 443 libre"
for _ in $(seq 1 30); do
  port_open 443 || break
  sleep 1
done
if port_open 443; then
  bad "algo sigue ocupando 443; el nginx de acceso no podra arrancar"
else
  ok "443 libre"
fi

info "4/5 Levantando nginx de acceso"
docker compose up -d --build || bad "docker compose up -d --build fallo"
sleep 2
st="$(docker inspect --format '{{.State.Status}}' access_nginx 2>/dev/null || true)"
if [ "$st" = "running" ] && docker exec access_nginx sh -c 'ls /etc/nginx/conf.d/*.conf >/dev/null 2>&1'; then
  if docker exec access_nginx nginx -t >/dev/null 2>&1 && docker exec access_nginx nginx -s reload >/dev/null 2>&1; then
    ok "access_nginx running (config recargada)"
  else
    bad "access_nginx corre pero no puede cargar la config; recreando ingress"
    docker compose up -d --force-recreate ingress || bad "no se pudo recrear ingress"
    sleep 2
    st="$(docker inspect --format '{{.State.Status}}' access_nginx 2>/dev/null || true)"
    if [ "$st" = "running" ]; then
      ok "access_nginx running (recreado)"
    else
      bad "access_nginx no arranco (estado: ${st:-inexistente}); ultimos logs:"
      docker compose logs --tail 40 ingress 2>&1 || true
      docker inspect --format '       exit={{.State.ExitCode}} error={{.State.Error}}' access_nginx 2>/dev/null || true
    fi
  fi
else
  if [ "$st" = "running" ]; then
    bad "access_nginx sin nginx/conf.d montado (contenedor viejo); recreando ingress"
  fi
  docker compose up -d --force-recreate ingress || bad "no se pudo levantar ingress"
  sleep 2
  st="$(docker inspect --format '{{.State.Status}}' access_nginx 2>/dev/null || true)"
  if [ "$st" = "running" ]; then
    ok "access_nginx running (recreado)"
  else
    bad "access_nginx no arranco (estado: ${st:-inexistente}); ultimos logs:"
    docker compose logs --tail 40 ingress 2>&1 || true
    docker inspect --format '       exit={{.State.ExitCode}} error={{.State.Error}}' access_nginx 2>/dev/null || true
  fi
fi

info "5/5 DNS y validacion"
if [ -n "$PIHOLE_KIND" ] && [ -x scripts/dns-overrides.sh ]; then
  scripts/dns-overrides.sh || true
elif [ -n "$PIHOLE_KIND" ]; then
  bad "falta scripts/dns-overrides.sh; agrega a mano en misc.dnsmasq_lines:"
  for h in index copias ups pihole proxmox backups pbs grafana netdata-services netdata-backups netdata-proxmox netdata-docker; do
    printf '         address=/%s.cortexdev.win/192.168.0.49\n' "$h"
  done
  printf '       Conserva el wildcard: address=/cortexdev.win/192.168.0.87\n'
  printf '       Purga cualquier linea .lan de misc.dnsmasq_lines.\n'
fi

if [ -x scripts/check.sh ]; then
  scripts/check.sh || true
fi

# Copias de seguridad (opt-in): requieren secretos y llaves ya colocados.
if [ -x backupcsr/install.sh ] && [ "${INSTALL_BACKUPCSR:-0}" = "1" ]; then
  info "backupcsr (copias de seguridad)"
  if sudo backupcsr/install.sh; then
    ok "backupcsr instalado (sudo validar-copias)"
  else
    bad "backupcsr/install.sh fallo"
  fi
fi

echo
if [ "$FAIL" = "0" ]; then
  printf '\033[1;32mListo\033[0m\n'
else
  printf '\033[1;31mRevisa los FAIL de arriba\033[0m\n'
  exit 1
fi
