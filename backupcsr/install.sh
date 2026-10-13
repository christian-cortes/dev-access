#!/usr/bin/env bash
# Instalador de las copias de seguridad (backupcsr) migradas desde WinSCP/VBS (PC Windows).
# Debe ejecutarse EN ubuntu-services (192.168.0.49), que es quien ejecuta las copias.
#
# Uso:
#   sudo backupcsr/install.sh                 # paquetes, archivos, credenciales, cron, logrotate y NAS
#   sudo backupcsr/install.sh --no-apt        # no toca apt (paquetes ya instalados)
#   sudo backupcsr/install.sh --no-nas        # no agrega la entrada del NAS a /etc/fstab ni monta
#   sudo INSTALL_KEYS=1 backupcsr/install.sh  # convierte keys/*.ppk a /etc/backupcsr/keys
#
# Variables opcionales: INSTALL_KEYS, INSTALL_NAS_FSTAB (0 para omitir el NAS),
#                       RUN_USER, NAS_SERVER, NAS_SHARE, ALLOW_OTHER_HOST,
#                       SET_TIMEZONE (0 para no ajustar el host a America/Bogota).
set -euo pipefail

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OPT_DIR=/opt/backupcsr
ETC_DIR=/etc/backupcsr
LOG_DIR=/var/log/backupcsr
NAS_SERVER="${NAS_SERVER:-192.168.0.179}"
NAS_SHARE="${NAS_SHARE:-Backups}"
NAS_MOUNT=/mnt/nas
RUN_USER="${RUN_USER:-root}"

DO_APT=1
DO_NAS="${INSTALL_NAS_FSTAB:-1}"
for arg in "$@"; do
	case "$arg" in
		--no-apt) DO_APT=0 ;;
		--no-nas) DO_NAS=0 ;;
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

echo "== 1/7 Paquetes =="
if [ "$DO_APT" = "1" ]; then
	export DEBIAN_FRONTEND=noninteractive
	apt-get update -y
	apt-get install -y lftp cifs-utils putty-tools rsync sshpass
else
	echo "omitido (--no-apt)"
fi

echo "== 2/7 Directorios =="
mkdir -p "$OPT_DIR/lib" "$OPT_DIR/jobs" "$OPT_DIR/tools" "$ETC_DIR/keys" "$ETC_DIR/keys-src" \
	"$LOG_DIR" "$NAS_MOUNT" "$ETC_DIR/jobs.d" "$ETC_DIR/jobs-backups"
# Overlay del portal (tareas creadas desde la web) y respaldos de scripts: 0700.
chmod 0700 "$ETC_DIR/jobs.d" "$ETC_DIR/jobs-backups" 2>/dev/null || true

echo "== 3/7 Scripts =="
install -m 0644 "$SRC_DIR/lib/common.sh" "$OPT_DIR/lib/common.sh"
for job_src in "$SRC_DIR"/jobs/*.sh; do
	install -m 0755 "$job_src" "$OPT_DIR/jobs/$(basename "$job_src")"
done
for tool_src in "$SRC_DIR"/tools/*.sh; do
	install -m 0755 "$tool_src" "$OPT_DIR/tools/$(basename "$tool_src")"
done
ln -sfn "$OPT_DIR/tools/validar-copias.sh" /usr/local/sbin/validar-copias
ln -sfn "$OPT_DIR/tools/subir-historiasclinicas.sh" /usr/local/sbin/subir-historiasclinicas
# Windows puede haber guardado CRLF; lftp/ssh/bash interpretan mal el \r al final de cada línea.
for f in "$OPT_DIR/lib/common.sh" "$OPT_DIR"/jobs/*.sh "$OPT_DIR"/tools/*.sh; do
	sed -i 's/\r$//' "$f"
done
# Validación de sintaxis antes de habilitar cron.
for f in "$OPT_DIR/lib/common.sh" "$OPT_DIR"/jobs/*.sh "$OPT_DIR"/tools/*.sh; do
	if ! bash -n "$f"; then
		echo "ERROR: sintaxis inválida en $f" >&2
		exit 1
	fi
done
echo "sintaxis bash verificada: $(ls -1 "$OPT_DIR"/jobs/*.sh | wc -l) jobs + herramientas"

echo "== 4/7 Credenciales =="
# Instala un secreto desde conf/: prefiere el archivo real (ignorado por git) y cae al
# .example si falta. Si el destino existe y difiere, guarda un respaldo con fecha.
install_secret() {
	local name="$1"
	local src="$SRC_DIR/conf/$name"
	local used_example=0
	if [ ! -e "$src" ]; then
		src="$SRC_DIR/conf/$name.example"
		used_example=1
	fi
	if [ ! -e "$src" ]; then
		echo "ADVERTENCIA: no existe conf/$name ni conf/$name.example, se omite" >&2
		return 0
	fi
	if [ -e "$ETC_DIR/$name" ] && ! cmp -s "$src" "$ETC_DIR/$name"; then
		local backup="$ETC_DIR/$name.bak.$(date '+%Y%m%d%H%M%S')"
		cp -p "$ETC_DIR/$name" "$backup"
		echo "respaldo del $name anterior en $backup"
	fi
	install -m 0600 "$src" "$ETC_DIR/$name"
	sed -i 's/\r$//' "$ETC_DIR/$name"
	chown "$RUN_USER" "$ETC_DIR/$name"
	chmod 600 "$ETC_DIR/$name"
	if [ "$used_example" = "1" ]; then
		echo "ADVERTENCIA: $ETC_DIR/$name vino del ejemplo, quedan valores CAMBIAR por completar"
	else
		echo "$ETC_DIR/$name instalado desde conf/$name"
	fi
}
install_secret nas.creds

# credentials.env puede contener grupos añadidos por el portal web (crear tareas). En
# vez de sobrescribirlo entero (lo que rompería esas tareas tras un reinstall), se
# FUSIONA: se conservan las líneas existentes y se añaden solo las claves del repo que
# falten. Si no existía, se instala tal cual.
merge_credentials() {
	local src="$1" dst="$ETC_DIR/credentials.env"
	[ -e "$src" ] || return 0
	if [ ! -e "$dst" ]; then
		install -m 0600 "$src" "$dst"
		sed -i 's/\r$//' "$dst"
		chown "$RUN_USER" "$dst" 2>/dev/null || true
		chmod 600 "$dst"
		echo "credentials.env instalado"
		return 0
	fi
	local line key
	while IFS= read -r line; do
		case "$line" in
			'#'*|'') continue ;;
			*=*) key="${line%%=*}" ;;
			*) continue ;;
		esac
		key="${key#export }"
		if grep -qE "^[[:space:]]*(export[[:space:]]+)?${key}=" "$dst"; then
			continue
		fi
		printf '%s\n' "$line" >>"$dst"
	done <"$src"
	chmod 600 "$dst"
	echo "credentials.env fusionado (se conservan los grupos añadidos por el portal)"
}
CRED_SRC="$SRC_DIR/conf/credentials.env"
[ -e "$CRED_SRC" ] || CRED_SRC="$SRC_DIR/conf/credentials.env.example"
merge_credentials "$CRED_SRC"

echo "== 5/7 Llaves SSH =="
# Las llaves no se versionan. Opciones:
#  a) dejar los .ppk en backupcsr/keys/ (ignorado por git) y usar INSTALL_KEYS=1;
#  b) copiar ya convertidas a /etc/backupcsr/keys/{google,latino} (modo 600) y omitir esto.
shopt -s nullglob
for ppk in "$SRC_DIR"/keys/*.ppk; do
	install -m 0600 "$ppk" "$ETC_DIR/keys-src/$(basename "$ppk")"
done
shopt -u nullglob
DO_KEYS="${INSTALL_KEYS:-0}"
if [ "$DO_KEYS" != "1" ]; then
	shopt -s nullglob
	for ppk in "$ETC_DIR"/keys-src/*.ppk; do
		[ -e "$ETC_DIR/keys/$(basename "$ppk" .ppk)" ] || DO_KEYS=1
	done
	shopt -u nullglob
fi
if [ "$DO_KEYS" = "1" ]; then
	shopt -s nullglob
	for ppk in "$ETC_DIR"/keys-src/*.ppk; do
		name="$(basename "$ppk" .ppk)"
		echo "convirtiendo $ppk -> $ETC_DIR/keys/$name (si el .ppk tiene passphrase, la pedirá)"
		puttygen "$ppk" -O private-openssh -o "$ETC_DIR/keys/$name"
		chmod 600 "$ETC_DIR/keys/$name"
		if ! ssh-keygen -y -f "$ETC_DIR/keys/$name" </dev/null >/dev/null 2>&1; then
			echo "la llave $name requiere passphrase; se pedirá para quitarla (cron no puede usarla cifrada)"
			if ssh-keygen -p -f "$ETC_DIR/keys/$name" -N ''; then
				echo "passphrase quitada de $name"
			else
				echo "ADVERTENCIA: $name sigue con passphrase; cron no podrá usarla."
			fi
		fi
	done
	shopt -u nullglob
	chown -R "$RUN_USER" "$ETC_DIR/keys" "$ETC_DIR/keys-src"
else
	echo "llaves ya presentes en $ETC_DIR/keys (forzar con INSTALL_KEYS=1)"
fi

echo "== 6/7 Cron y logrotate =="
CURRENT_TZ="$(timedatectl show -p Timezone --value 2>/dev/null || cat /etc/timezone 2>/dev/null || echo desconocida)"
echo "zona horaria del servidor: $CURRENT_TZ"
# El cron de Ubuntu/Debian NO implementa CRON_TZ: la línea `CRON_TZ=` se ignora al
# programar (solo se exporta a los jobs) y los horarios se evalúan en la zona del
# anfitrión. Con el host en UTC, los jobs 6-19 correrían 01:00-14:00 en Colombia y
# dejarían de disparar por la tarde (lo que obliga a ejecutarlos a mano). Por eso la
# zona del host debe ser America/Bogota; se ajusta aquí salvo SET_TIMEZONE=0.
if [ "$CURRENT_TZ" != "America/Bogota" ]; then
	if [ "${SET_TIMEZONE:-1}" = "1" ]; then
		echo "ajustando zona horaria del host a America/Bogota (SET_TIMEZONE=0 para omitir)"
		timedatectl set-timezone America/Bogota
		systemctl restart cron 2>/dev/null || true
		echo "zona horaria ajustada: $(timedatectl show -p Timezone --value 2>/dev/null)"
	else
		echo "ADVERTENCIA: el host está en $CURRENT_TZ y los horarios del cron son hora" >&2
		echo "             Colombia. Este cron NO honra CRON_TZ, así que los jobs se" >&2
		echo "             dispararán en $CURRENT_TZ. Ajustar con:" >&2
		echo "             timedatectl set-timezone America/Bogota" >&2
	fi
fi
install -m 0644 "$SRC_DIR/cron/backupcsr.cron" /etc/cron.d/backupcsr
install -m 0644 "$SRC_DIR/logrotate/backupcsr" /etc/logrotate.d/backupcsr
sed -i 's/\r$//' /etc/cron.d/backupcsr /etc/logrotate.d/backupcsr "$ETC_DIR/credentials.env" "$ETC_DIR/nas.creds"
chmod 0644 /etc/cron.d/backupcsr /etc/logrotate.d/backupcsr
chmod 600 "$ETC_DIR/credentials.env" "$ETC_DIR/nas.creds"

# El cron instalado es la verdad para el estado; la BD del portal es el horario editable.
# Al copiar este cron la BD puede quedar desviada (p. ej. el `:20` viejo) y el panel marcaría
# TARDE a jobs que sí corren: se sincroniza el catálogo (inserta jobs nuevos y refresca
# metadata) y se adopta el cron en la BD. OJO: `sync-jobs` nunca cambia `enabled` de filas
# existentes, así que `adopt-cron` avisa si el cron dispara un job que la BD tiene enabled=0.
# Con BACKUP_MANAGE_CRON=1 el cron se genera desde la BD, así que además se regenera: evita
# que el cron dispare un job que la BD tiene deshabilitado (y que un guardado posterior lo
# borre de golpe). Best-effort: si el portal todavía no está desplegado (no existe el venv) o
# la gestión está desactivada, solo se avisa.
if [ -x "$OPT_DIR/web/venv/bin/python" ] && [ -f "$ETC_DIR/web.env" ]; then
	if ( cd "$OPT_DIR/web" && set -a && . "$ETC_DIR/web.env" && set +a \
		&& venv/bin/python -m app.cli sync-jobs \
		&& venv/bin/python -m app.cli adopt-cron ); then
		if grep -qE '^[[:space:]]*(export[[:space:]]+)?BACKUP_MANAGE_CRON=1' "$ETC_DIR/web.env"; then
			( cd "$OPT_DIR/web" && set -a && . "$ETC_DIR/web.env" && set +a \
				&& venv/bin/python -m app.cli render-cron --apply ) \
				|| echo "ADVERTENCIA: no se pudo regenerar el cron desde la BD" >&2
		fi
	else
		echo "ADVERTENCIA: no se pudo reconciliar la BD con el cron instalado" >&2
	fi
fi

echo "== 7/7 NAS =="
if [ "$DO_NAS" = "1" ]; then
	FSTAB_LINE="//$NAS_SERVER/$NAS_SHARE  $NAS_MOUNT  cifs  credentials=$ETC_DIR/nas.creds,vers=3.0,uid=$RUN_USER,gid=$RUN_USER,file_mode=0660,dir_mode=0770,iocharset=utf8,_netdev,nofail,x-systemd.automount  0  0"
	if grep -qE "^[^#]*[[:space:]]$NAS_MOUNT[[:space:]]" /etc/fstab; then
		CURRENT_LINE="$(grep -E "^[^#]*[[:space:]]$NAS_MOUNT[[:space:]]" /etc/fstab | head -n 1)"
		if [ "$CURRENT_LINE" = "$FSTAB_LINE" ]; then
			echo "/etc/fstab ya tiene la entrada correcta para $NAS_MOUNT"
		else
			cp -p /etc/fstab "/etc/fstab.bak.$(date '+%Y%m%d%H%M%S')"
			grep -vE "^[^#]*[[:space:]]$NAS_MOUNT[[:space:]]" /etc/fstab >/etc/fstab.new
			mv /etc/fstab.new /etc/fstab
			printf '%s\n' "$FSTAB_LINE" >>/etc/fstab
			echo "entrada de $NAS_MOUNT actualizada en /etc/fstab ($NAS_SERVER/$NAS_SHARE)"
		fi
	else
		printf '%s\n' "$FSTAB_LINE" >>/etc/fstab
		echo "entrada agregada a /etc/fstab ($NAS_SERVER/$NAS_SHARE -> $NAS_MOUNT)"
	fi
	systemctl daemon-reload || true
	if mountpoint -q "$NAS_MOUNT"; then
		echo "$NAS_MOUNT ya está montado"
	elif mount "$NAS_MOUNT"; then
		echo "$NAS_MOUNT montado"
	else
		echo "ADVERTENCIA: el montaje falló, revisar credenciales/vers y /etc/fstab"
	fi
else
	echo "omitido (--no-nas / INSTALL_NAS_FSTAB=0); entrada de referencia en conf/fstab.backupcsr.example"
fi

cat <<'NEXT'

Instalación lista (scripts, credenciales, cron, logrotate y NAS).

Los jobs activos (ruta56-bd, ruta56-web, gastro-bd, enter-bd, google-bd, latino-bd,
latino-web) espejan el origen remoto DIRECTO a su ruta final en el NAS (/mnt/nas). NO hay
staging local: los jobs abortan con require_nas si el NAS no está montado, para no escribir
en /.
google-bd, latino-bd y latino-web usan /etc/backupcsr/keys/{google,latino}.

Pasos siguientes:

1) Verificar NAS:
   mountpoint /mnt/nas && ls /mnt/nas
   (si falla: revisar /etc/backupcsr/nas.creds, vers= y conf/fstab.backupcsr.example)

2) Validar que las copias se ejecutan:
   sudo validar-copias            # 0=todo OK, 1=advertencias, 2=errores

3) Prueba en seco de cada job (no descarga ni sube nada):
   sudo DRY_RUN=1 /opt/backupcsr/jobs/ruta56-bd.sh
   tail -n 50 /var/log/backupcsr/ruta56-bd.log

4) Prueba real de un job pequeño y comparar con el destino del NAS
   (\\192.168.0.179\Backups\...).

5) Confirmar que cron los dispara:
   sudo journalctl -u cron --since today | grep backupcsr

ANTES del corte: deshabilitar el cron de backupcsr en cualquier otro host (nunca deben
correr dos mirrors con --delete al mismo destino del NAS).
NEXT
