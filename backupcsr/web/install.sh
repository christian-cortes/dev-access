#!/usr/bin/env bash
# Instalador del portal de administración de copias (backupcsr-web).
# Debe ejecutarse EN ubuntu-services (192.168.0.49), que es quien ejecuta las copias.
#
# Uso:
#   sudo backupcsr/web/install.sh                # venv, deps, systemd y admin (si hay BD)
#   sudo backupcsr/web/install.sh --no-apt       # no toca apt (paquetes ya instalados)
#   sudo backupcsr/web/install.sh --skip-db      # no aplica schema ni crea admin
#   sudo backupcsr/web/install.sh --enable-scheduler   # solo activa el planificador
#   sudo backupcsr/web/install.sh --disable-scheduler  # solo lo desactiva (vuelve al serial)
#
# Prerrequisitos (una vez, ver backupcsr/web/README.md):
#   1) MySQL de ubuntu-docker (.87) publicado en la LAN y con la BD/usuario creados
#      (sql/bootstrap.sql).
#   2) /etc/backupcsr/web.env con BACKUP_DB_* y BACKUP_SECRET_KEY.
set -euo pipefail

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OPT_DIR="${BACKUP_OPT:-/opt/backupcsr}"
WEB_DIR="$OPT_DIR/web"
ENV_FILE="${BACKUP_ETC:-/etc/backupcsr}/web.env"
UNIT=/etc/systemd/system/backupcsr-web.service
SCHED_UNIT=/etc/systemd/system/backupcsr-scheduler.service
PORT=8089

DO_APT=1
DO_DB=1
SCHED_MODE=""
for arg in "$@"; do
	case "$arg" in
		--no-apt) DO_APT=0 ;;
		--skip-db) DO_DB=0 ;;
		--enable-scheduler) SCHED_MODE="enable" ;;
		--disable-scheduler) SCHED_MODE="disable" ;;
		*) echo "Opción desconocida: $arg" >&2; exit 2 ;;
	esac
done

if [ "$(id -u)" -ne 0 ]; then
	echo "ERROR: ejecutar con sudo." >&2
	exit 1
fi
if [ "${ALLOW_OTHER_HOST:-0}" != "1" ] && ! hostname -I 2>/dev/null | grep -q '192\.168\.0\.49'; then
	echo "ERROR: install.sh debe correr en ubuntu-services (192.168.0.49); usa ALLOW_OTHER_HOST=1 para forzar" >&2
	exit 1
fi

# Ajusta una variable del web.env sin reescribir el resto (activa o comenta la clave).
set_env_flag() {
	local key="$1" value="$2"
	if grep -q "^${key}=" "$ENV_FILE"; then
		sed -i "s|^${key}=.*|${key}=${value}|" "$ENV_FILE"
	elif grep -q "^# *${key}=" "$ENV_FILE"; then
		sed -i "s|^# *${key}=.*|${key}=${value}|" "$ENV_FILE"
	else
		printf '\n%s=%s\n' "$key" "$value" >>"$ENV_FILE"
	fi
}

# Atajo: encender/apagar el planificador sin reinstalar venv/deps/BD.
if [ -n "$SCHED_MODE" ]; then
	if [ ! -f "$ENV_FILE" ]; then
		echo "ERROR: no existe $ENV_FILE; ejecuta install.sh completo primero" >&2
		exit 1
	fi
	# Las unidades las genera el install completo (sección 5/6); sin ellas, cambiar el
	# web.env dejaría la bandera y el servicio en estados distintos.
	if [ ! -f "$SCHED_UNIT" ] || [ ! -f "$UNIT" ]; then
		echo "ERROR: faltan las unidades systemd ($SCHED_UNIT / $UNIT);" >&2
		echo "       ejecuta install.sh completo una vez antes de usar --$SCHED_MODE-scheduler" >&2
		exit 1
	fi
	if [ "$SCHED_MODE" = "enable" ]; then
		prev_sched="$(sed -n 's/^BACKUP_SCHEDULER=//p' "$ENV_FILE" | tail -n1 | tr -d ' "')"
		set_env_flag BACKUP_SCHEDULER 1
		systemctl daemon-reload
		if ! systemctl enable --now backupcsr-scheduler.service; then
			set_env_flag BACKUP_SCHEDULER "${prev_sched:-0}"
			echo "ERROR: no se pudo habilitar backupcsr-scheduler.service;" >&2
			echo "       se revierte BACKUP_SCHEDULER=${prev_sched:-0}" >&2
			exit 1
		fi
		systemctl restart backupcsr-web.service || true
		echo "planificador habilitado (BACKUP_SCHEDULER=1); el cron encola en cola"
	else
		set_env_flag BACKUP_SCHEDULER 0
		systemctl daemon-reload
		systemctl disable --now backupcsr-scheduler.service || true
		systemctl restart backupcsr-web.service || true
		echo "planificador deshabilitado (BACKUP_SCHEDULER=0); el cron usa el respaldo serial"
	fi
	exit 0
fi

echo "== 1/6 Paquetes =="
if [ "$DO_APT" = "1" ]; then
	export DEBIAN_FRONTEND=noninteractive
	apt-get update -y
	apt-get install -y python3 python3-venv python3-pip
else
	echo "omitido (--no-apt)"
fi

echo "== 2/6 Copiando la aplicación a $WEB_DIR =="
mkdir -p "$WEB_DIR"
for item in app static sql jobs.yml schema.sql requirements.txt; do
	if [ -e "$SRC_DIR/$item" ]; then
		cp -a "$SRC_DIR/$item" "$WEB_DIR/"
	fi
done
# Quita __pycache__ que pudiera venir del repo.
find "$WEB_DIR/app" -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true
# Restos del frontend monolítico anterior (el nuevo vive en static/js/).
rm -f "$WEB_DIR/static/app.js" "$WEB_DIR/static/api.js"

echo "== 3/6 Entorno virtual y dependencias =="
if [ ! -x "$WEB_DIR/venv/bin/python" ]; then
	python3 -m venv "$WEB_DIR/venv"
fi
"$WEB_DIR/venv/bin/pip" install --upgrade pip >/dev/null
"$WEB_DIR/venv/bin/pip" install -r "$WEB_DIR/requirements.txt"

echo "== 3b/6 CLI del planificador =="
BIN_DIR="$OPT_DIR/bin"
mkdir -p "$BIN_DIR"
install -m 0755 "$SRC_DIR/bin/backupcsr-scheduler" "$BIN_DIR/backupcsr-scheduler"
echo "CLI instalado en $BIN_DIR/backupcsr-scheduler"

echo "== 4/6 Configuración ($ENV_FILE) =="
mkdir -p "$(dirname "$ENV_FILE")"
if [ ! -f "$ENV_FILE" ]; then
	install -m 0600 "$SRC_DIR/conf/web.env.example" "$ENV_FILE"
	sed -i "s|^BACKUP_SECRET_KEY=.*|BACKUP_SECRET_KEY=$(openssl rand -hex 32 2>/dev/null || head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n')|" "$ENV_FILE"
	echo "ADVERTENCIA: se creó $ENV_FILE desde el ejemplo con una SECRET_KEY aleatoria;"
	echo "             completa BACKUP_DB_* (y cambia BACKUP_MANAGE_CRON si aplica) y reejecuta."
else
	echo "$ENV_FILE ya existe (se conserva)"
fi
chmod 600 "$ENV_FILE"

echo "== 5/6 Servicio systemd =="
sed -e "s|__WEB_DIR__|$WEB_DIR|g" "$SRC_DIR/systemd/backupcsr-web.service.template" >"$UNIT"
chmod 0644 "$UNIT"
sed -e "s|__WEB_DIR__|$WEB_DIR|g" "$SRC_DIR/systemd/backupcsr-scheduler.service.template" >"$SCHED_UNIT"
chmod 0644 "$SCHED_UNIT"
systemctl daemon-reload
systemctl enable backupcsr-web.service >/dev/null 2>&1 || true
# El daemon solo se arranca con BACKUP_SCHEDULER=1; con 0 el cron cae a ejecución
# directa serial (gate) y no se mezclan los dos modos.
SCHEDULER_FLAG="$(sed -n 's/^BACKUP_SCHEDULER=//p' "$ENV_FILE" | tail -n1 | tr -d ' \"')"
if [ "${SCHEDULER_FLAG:-0}" = "1" ]; then
	systemctl enable backupcsr-scheduler.service >/dev/null 2>&1 || true
	systemctl restart backupcsr-scheduler.service || true
	echo "planificador habilitado (BACKUP_SCHEDULER=1)"
else
	systemctl disable --now backupcsr-scheduler.service >/dev/null 2>&1 || true
	echo "BACKUP_SCHEDULER=${SCHEDULER_FLAG:-0}: planificador no arrancado (cron usa el fallback serial)"
fi

echo "== 5b/6 Permisos de los lockfiles de jobs =="
# cron crea /run/lock/backupcsr-*.lock como root:root 0644. El portal abre esos
# ficheros en modo append para tomar el mismo flock que cron: si el servicio no
# corre como root, sin lectura+escritura falla con 500 en /api/summary y /api/jobs.
SVC_USER="$(sed -n 's/^User=//p' "$UNIT" | head -n1)"
if [ -n "$SVC_USER" ] && [ "$SVC_USER" != "root" ]; then
	SVC_GROUP="$(id -gn "$SVC_USER" 2>/dev/null || echo "$SVC_USER")"
	FIXED=0
	for lock in /run/lock/backupcsr-*.lock; do
		[ -e "$lock" ] || continue
		chgrp "$SVC_GROUP" "$lock" 2>/dev/null || true
		chmod 0664 "$lock" 2>/dev/null || true
		FIXED=$((FIXED + 1))
	done
	echo "servicio como '$SVC_USER'; lockfiles legibles para $SVC_GROUP (0664): $FIXED"
	echo "nota: si cron los recrea como root, vuelve a ejecutar este paso."
else
	echo "el servicio corre como root; no requiere ajustes"
fi

echo "== 6/6 Base de datos (esquema y admin) =="
if [ "$DO_DB" = "1" ]; then
	set -a
	# shellcheck disable=SC1090
	. "$ENV_FILE"
	set +a
	if (cd "$WEB_DIR" && "$WEB_DIR/venv/bin/python" -m app.cli apply-schema); then
		if ! (cd "$WEB_DIR" && "$WEB_DIR/venv/bin/python" -m app.cli sync-jobs); then
			echo "ADVERTENCIA: no se pudo sincronizar jobs.yml con la BD"
		fi
		ADMIN_USER="${BACKUP_ADMIN_USER:-admin}"
		ADMIN_PASS="${BACKUP_ADMIN_PASSWORD:-}"
		GENERATED=0
		if [ -z "$ADMIN_PASS" ]; then
			ADMIN_PASS="$(openssl rand -hex 12 2>/dev/null || head -c 24 /dev/urandom | od -An -tx1 | tr -d ' \n')"
			GENERATED=1
		fi
		if (cd "$WEB_DIR" && "$WEB_DIR/venv/bin/python" -m app.cli create-admin \
			--username "$ADMIN_USER" --password "$ADMIN_PASS"); then
			if [ "$GENERATED" = "1" ]; then
				PW_FILE="$(dirname "$ENV_FILE")/admin-password"
				printf '%s\n' "$ADMIN_PASS" >"$PW_FILE"
				chmod 600 "$PW_FILE"
				echo "usuario admin '$ADMIN_USER' creado; contraseña en $PW_FILE (0600)"
			fi
		else
			echo "ADVERTENCIA: no se pudo crear el usuario admin"
		fi
	else
		echo "ADVERTENCIA: MySQL no disponible; revisa BACKUP_DB_* y el Anexo A de README/plan."
		echo "             El portal arrancará en modo lectura y reintentará al arrancar."
	fi
else
	echo "omitido (--skip-db)"
fi

systemctl restart backupcsr-web.service || true
sleep 1
if curl -fsS "http://127.0.0.1:$PORT/api/health" >/dev/null 2>&1; then
	echo "backupcsr-web responde en 127.0.0.1:$PORT"
else
	echo "ADVERTENCIA: backupcsr-web no responde aún; revisa: journalctl -u backupcsr-web -n 50"
fi

cat <<'NEXT'

Instalación del portal lista.

Siguientes pasos:
  1) nginx: docker compose up -d --force-recreate ingress (monta ./backupcsr/web/static)
     y recarga: docker exec access_nginx nginx -t && docker exec access_nginx nginx -s reload
  2) DNS: scripts/dns-overrides.sh (agrega copias.cortexdev.win -> 192.168.0.49)
  3) Validar: scripts/check.sh  y  https://copias.cortexdev.win/
  4) Gestión de cron: solo tras validar el render, poner BACKUP_MANAGE_CRON=1 en
     /etc/backupcsr/web.env y reiniciar el servicio.
NEXT
