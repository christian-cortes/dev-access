#!/usr/bin/env bash
# Librería común de los trabajos de copia de seguridad (backupcsr).
# Reemplaza a WinSCP ("synchronize local -mirror") por lftp.
# Cada job espeja el origen remoto DIRECTO a su ruta final en el NAS montado en
# $BACKUPCSR_NAS: no hay staging local ni copia en el disco del equipo.
# Vive en cortexdev-access/backupcsr/lib/common.sh; install.sh la copia a
# /opt/backupcsr/lib/common.sh (modo 0644).
set -euo pipefail

BACKUPCSR_ETC="${BACKUPCSR_ETC:-/etc/backupcsr}"
BACKUPCSR_OPT="${BACKUPCSR_OPT:-/opt/backupcsr}"
BACKUPCSR_LOG="${BACKUPCSR_LOG:-/var/log/backupcsr}"
BACKUPCSR_KEYS="${BACKUPCSR_KEYS:-$BACKUPCSR_ETC/keys}"
BACKUPCSR_NAS="${BACKUPCSR_NAS:-/mnt/nas}"
BACKUPCSR_NAS_ROOT="${BACKUPCSR_NAS_ROOT:-$BACKUPCSR_NAS}"

MIRROR_PARALLEL="${MIRROR_PARALLEL:-2}"
DRY_RUN="${DRY_RUN:-0}"

# --- Presión sobre el anfitrión -------------------------------------------------
# El NAS (CIFS) y la RAM son compartidos con nginx, MySQL, el portal y el stack de
# monitoreo: el anfitrión tiene 2 núcleos y 4 GB (ampliable a 8 GB / 4 vCPU), así que
# lanzar los 6 jobs a la misma hora bloquea el resto del servidor.
#   MIRROR_GATE  respaldo serial: un solo job de copia a la vez (vacío = sin cola).
#                Con el planificador (backupcsr-scheduler) el daemon lanza los jobs
#                con MIRROR_GATE vacío y aplica sus propios cupos (global y por host);
#                este gate solo protege las ejecuciones directas/manuales.
#   GATE_WAIT    segundos máximos de espera por el turno antes de OMITIR la ronda
#                (deja el estado OMITIDO, no FALLO; se reintenta en el próximo ciclo).
#   JOB_NICE     prioridad de CPU del job y de lftp (hijos heredan).
#   JOB_IONICE_* clase/nivel de I/O de lftp.
#   MIRROR_COMPARE  size = solo nuevos y los que cambiaron de tamaño (por defecto);
#                   size+time = comparación clásica tamaño+fecha.
#   MIRROR_RATE_LIMIT  bytes/s totales por corrida (0 = sin límite).
MIRROR_GATE="${MIRROR_GATE-/run/lock/backupcsr-gate.lock}"
GATE_WAIT="${GATE_WAIT:-900}"
JOB_NICE="${JOB_NICE:-15}"
JOB_IONICE_CLASS="${JOB_IONICE_CLASS:-2}"
JOB_IONICE_LEVEL="${JOB_IONICE_LEVEL:-7}"
MIRROR_COMPARE="${MIRROR_COMPARE:-size}"
MIRROR_RATE_LIMIT="${MIRROR_RATE_LIMIT:-0}"
MIRROR_MAX_ERRORS="${MIRROR_MAX_ERRORS:-20}"
# Reintentos por error transitorio de red y tolerancia a errores por archivo:
#   MIRROR_ATTEMPTS  intentos totales del mirror ante un fallo transitorio.
#   RETRY_BACKOFF    segundos de espera entre intentos (se repite el último).
#   PARTIAL_MAX      archivos con error real que aún se toleran (el job cierra "parcial").
#   UNREPR_MAX       entradas que el NAS (CIFS) no puede representar y aún se toleran:
#                    nombres con bytes no UTF-8, con barra invertida ("Invalid argument")
#                    y enlaces simbólicos. Se cuentan aparte de PARTIAL_MAX para que un
#                    fallo real no se disfrace de "parcial".
#   SKIP_EXIT        código de salida cuando no se consigue el turno de la cola.
MIRROR_ATTEMPTS="${MIRROR_ATTEMPTS:-3}"
RETRY_BACKOFF="${RETRY_BACKOFF:-30 120}"
PARTIAL_MAX="${PARTIAL_MAX:-50}"
UNREPR_MAX="${UNREPR_MAX:-200}"
SKIP_EXIT="${SKIP_EXIT:-75}"

log() {
	printf '%s [%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "${JOB:-setup}" "$*"
}

die() {
	log "ERROR: $*"
	exit 1
}

require_cmd() {
	command -v "$1" >/dev/null 2>&1 || die "falta el comando '$1' en PATH"
}

# El origen remoto no puede resolver a loopback: si el DNS del dominio apunta a
# 127.0.0.1/::1, lftp se conecta al propio servidor y agota los reintentos con
# "Connection refused", que se clasifica como transitorio y reintenta en vano. Se
# detecta antes de conectar y se corta de una vez como error fatal de configuración.
require_remote_host() {
	local host="$1" addrs a nonloop=0
	[ -n "$host" ] || die "host de origen vacío en la configuración"
	command -v getent >/dev/null 2>&1 || return 0
	addrs="$(getent ahosts "$host" 2>/dev/null | awk '{print $1}' | sort -u)"
	[ -n "$addrs" ] || die "el host de origen '$host' no resuelve (revisar DNS)"
	while IFS= read -r a; do
		[ -n "$a" ] || continue
		case "$a" in
			127.* | 0.0.0.0 | ::1 | ::ffff:127.*) ;;
			*) nonloop=1 ;;
		esac
	done <<<"$addrs"
	[ "$nonloop" -eq 1 ] || die "el host de origen '$host' resuelve solo a loopback (127.0.0.1): DNS del origen mal configurado"
}

# Cola global: un solo job de copia a la vez. El lock se libera al salir (fd 8).
# Sin MIRROR_GATE (vacío) no hay cola: cada job corre cuando le toca.
acquire_gate() {
	[ -n "$MIRROR_GATE" ] || return 0
	local start elapsed
	start="$(date +%s)"
	exec 8>"$MIRROR_GATE" || die "no se pudo abrir la cola $MIRROR_GATE"
	if ! flock -w "$GATE_WAIT" 8; then
		# Contención: no es un fallo de datos. Se omite la ronda y se reintenta en el
		# próximo ciclo del cron; el portal lo muestra como "OMITIDO" (aviso), no como
		# "FALLO". Se quita el trap ERR para no registrar FALLO ni exit=1.
		log "AVISO: cola ocupada: sin turno tras ${GATE_WAIT}s esperando $MIRROR_GATE"
		log "OMITIDO: otro job tiene la cola; se reintenta en el próximo ciclo"
		trap - ERR
		exit "$SKIP_EXIT"
	fi
	elapsed=$(($(date +%s) - start))
	if [ "$elapsed" -gt 2 ]; then
		log "turno conseguido tras esperar ${elapsed}s"
	fi
	return 0
}

# Prepara el log del job y exige el NAS (los jobs escriben solo en /mnt/nas).
# Sin este guard, un NAS desmontado haría que el mirror escribiera en / (disco local).
job_init() {
	JOB="$1"
	mkdir -p "$BACKUPCSR_LOG"
	exec >>"$BACKUPCSR_LOG/$JOB.log" 2>&1
	set -E
	trap 'log "FALLO: exit=$? linea=$LINENO comando=$BASH_COMMAND"' ERR
	trap '_backupcsr_on_exit' EXIT
	# Las copias no deben competir con nginx/MySQL/panel por CPU ni por I/O.
	renice -n "$JOB_NICE" -p $$ >/dev/null 2>&1 || true
	ionice -c "$JOB_IONICE_CLASS" -n "$JOB_IONICE_LEVEL" -p $$ >/dev/null 2>&1 || true
	log "=== inicio $JOB (dry_run=$DRY_RUN nice=$JOB_NICE trigger=${BACKUP_TRIGGERED_BY:-cron}) ==="
	require_nas
	acquire_gate
}

# Código de salida real de la corrida (lo interpreta el portal como runs.exit_code).
_backupcsr_on_exit() {
	local rc=$?
	log "PROCESO: exit=$rc"
	exit "$rc"
}

# Carga /etc/backupcsr/credentials.env (chmod 600, root:root).
load_credentials() {
	local file="$BACKUPCSR_ETC/credentials.env"
	[ -r "$file" ] || die "no se puede leer $file"
	if grep -v '^[[:space:]]*#' "$file" | grep -q 'CAMBIAR'; then
		die "quedan valores sin reemplazar (CAMBIAR) en $file"
	fi
	set -a
	# shellcheck disable=SC1090
	. "$file"
	set +a
}

# El NAS debe estar montado y escribible antes de publicar.
require_nas() {
	mountpoint -q "$BACKUPCSR_NAS" || die "el NAS no está montado en $BACKUPCSR_NAS"
	[ -w "$BACKUPCSR_NAS" ] || die "sin permiso de escritura en $BACKUPCSR_NAS"
}

# --- Ejecución del mirror: reintentos y tolerancia a errores por archivo --------

# ¿La salida de lftp apunta a un fallo transitorio de red/reconexión?
_lftp_is_transient() {
	grep -qiE 'max-retries|timed out|timeout|connection refused|connection reset|broken pipe|temporarily unavailable|reconnect|connection closed' "$1"
}

# ¿La cadena tiene bytes que no son UTF-8 válido? (nombre que CIFS con iocharset=utf8
# no puede crear). Necesita iconv; sin él se asume válido (no se clasifica).
_is_invalid_utf8() {
	local s="$1"
	command -v iconv >/dev/null 2>&1 || return 1
	printf '%s' "$s" | iconv -f UTF-8 -t UTF-8 >/dev/null 2>&1 || return 0
	return 1
}

# Cuenta en la salida de lftp los errores por archivo separando los "reales" de las
# entradas que el NAS no puede representar (byte no UTF-8, barra invertida o enlace
# simbólico: el montaje CIFS no las admite). Devuelve por stdout "reales no_repr".
_lftp_counts() {
	local line real=0 unrepr=0 path
	while IFS= read -r line; do
		case "$line" in
			'mirror: Fatal error:'*) continue ;;
			'mirror: symlink('*'): Operation not supported') unrepr=$((unrepr + 1)) ;;
			'mirror: '*': Invalid argument') unrepr=$((unrepr + 1)) ;;
			'mirror: '*': No such file or directory')
				path="${line#mirror: }"
				path="${path%: No such file or directory}"
				if _is_invalid_utf8 "$path"; then
					unrepr=$((unrepr + 1))
				else
					real=$((real + 1))
				fi
				;;
			'mirror: '*) real=$((real + 1)) ;;
		esac
	done <"$1"
	printf '%s %s' "$real" "$unrepr"
}

# Ejecuta el programa lftp (archivo) con reintentos y clasificación de errores.
#   0 = terminó (posible "parcial", ya avisado con AVISO: N archivos con error)
#   1 = fallo definitivo (ya logueado como ERROR:)
# La salida de lftp se vuelca al log y a un archivo temporal (no se retiene en RAM).
run_mirror() {
	local program="$1"
	local attempts="$MIRROR_ATTEMPTS"
	local -a backoff=($RETRY_BACKOFF)
	local out
	out="$(mktemp "${TMPDIR:-/tmp}/backupcsr-lftp-out.XXXXXX")"
	local rc attempt=1 delay fatal counts real unrepr
	while :; do
		rc=0
		if lftp -f "$program" >"$out" 2>&1; then
			rc=0
		else
			rc=$?
		fi
		cat "$out"
		if [ "$rc" -eq 0 ]; then
			rm -f "$out"
			return 0
		fi
		if _lftp_is_transient "$out"; then
			if [ "$attempt" -lt "$attempts" ]; then
				delay="${backoff[$((attempt - 1))]:-${backoff[$((${#backoff[@]} - 1))]:-60}}"
				log "AVISO: reintento $((attempt + 1))/$attempts por error transitorio (espera ${delay}s)"
				sleep "$delay"
				attempt=$((attempt + 1))
				continue
			fi
			log "ERROR: lftp agotó $attempts intento(s) por error transitorio [transitorio]"
			rm -f "$out"
			return 1
		fi
		if grep -qiE 'max-errors exceeded' "$out"; then
			log "ERROR: se superó el tope de errores de lftp (--max-errors=$MIRROR_MAX_ERRORS)"
			rm -f "$out"
			return 1
		fi
		fatal="$(grep -cE '^mirror: Fatal error:' "$out" || true)"
		counts="$(_lftp_counts "$out")"
		real="${counts%% *}"
		unrepr="${counts##* }"
		if [ "$fatal" -eq 0 ] && { [ "$real" -gt 0 ] || [ "$unrepr" -gt 0 ]; }; then
			grep -E '^mirror: ' "$out" | awk 'NR <= 5 { sub(/^mirror: /, ""); print }' | while IFS= read -r line; do
				log "AVISO: archivo con error: $line"
			done
			if [ "$real" -gt 0 ]; then
				log "AVISO: $real archivos con error"
			fi
			if [ "$unrepr" -gt 0 ]; then
				log "AVISO: $unrepr entradas no representables en el NAS (nombre no UTF-8, barra invertida o enlace simbólico); se omiten"
			fi
			if [ "$real" -le "$PARTIAL_MAX" ] && [ "$unrepr" -le "$UNREPR_MAX" ]; then
				rm -f "$out"
				return 0
			fi
			if [ "$real" -gt "$PARTIAL_MAX" ]; then
				log "ERROR: $real archivos con error superan el tope de $PARTIAL_MAX"
			fi
			if [ "$unrepr" -gt "$UNREPR_MAX" ]; then
				log "ERROR: $unrepr entradas no representables superan el tope de $UNREPR_MAX"
			fi
			rm -f "$out"
			return 1
		fi
		# lftp con xfer:use-temp-file intenta borrar su temporal *.lftp al reintentar
		# una transferencia abortada; si ya no existe, falla con "No such file" y sale
		# con código 1 aunque el espejo terminó bien (visto en google-bd tras un
		# "file size decreased during transfer"). Es ruido de limpieza, no pérdida.
		if [ "$fatal" -eq 0 ] && [ "$real" -eq 0 ] && [ "$unrepr" -eq 0 ] &&
			grep -qE '^rm: Access failed: .*\.lftp: No such file or directory' "$out"; then
			log "AVISO: lftp no pudo borrar un temporal *.lftp ya ausente (ruido de limpieza; se ignora)"
			rm -f "$out"
			return 0
		fi
		log "ERROR: lftp terminó con código $rc"
		rm -f "$out"
		return 1
	done
}

# Ejecuta un programa de lftp leído por stdin; conserva y devuelve el exit code.
lftp_program() {
	local program
	local rc=0
	program="$(mktemp "${TMPDIR:-/tmp}/backupcsr-lftp.XXXXXX")"
	cat >"$program"
	run_mirror "$program" || rc=$?
	rm -f "$program"
	return "$rc"
}

# Ajustes comunes a los tres protocolos. Se interpolan en el programa de lftp.
lftp_common_settings() {
	cat <<'EOF'
set xfer:clobber on
set mirror:set-permissions false
set mirror:overwrite true
set xfer:use-temp-file true
set xfer:temp-file-name *.lftp
set mirror:skip-noaccess true
EOF
	if [ "$MIRROR_RATE_LIMIT" != "0" ]; then
		printf 'set net:limit-total-rate %s\n' "$MIRROR_RATE_LIMIT"
	fi
}

# Opciones de `mirror` (sin exclusiones) en MIRROR_OPTS.
# MIRROR_COMPARE=size -> --ignore-time: transfiere solo lo nuevo y lo que cambió de
# tamaño. Es lo que evita rebajar cada hora archivos idénticos (el NAS es lento y el
# anfitrión tiene poca RAM). DRY_RUN usa --dry-run de lftp (¡`-n` en mirror es
# --only-newer, no simulación!).
mirror_options() {
	MIRROR_OPTS=(--delete --verbose "--parallel=$MIRROR_PARALLEL" "--max-errors=$MIRROR_MAX_ERRORS")
	if [ "$MIRROR_COMPARE" = "size" ]; then
		MIRROR_OPTS+=(--ignore-time)
	fi
	if [ "$DRY_RUN" = "1" ]; then
		MIRROR_OPTS+=(--dry-run)
	fi
}

# mirror_sftp host port user key remote local [exclusion...]
# Equivale a: "synchronize local -mirror -nopermissions -preservetime <remote> <local>"
mirror_sftp() {
	local host="$1" port="$2" user="$3" key="$4" remote="$5" local_dir="$6"
	shift 6
	local -a excl=()
	local item
	for item in "$@"; do
		# -X (--exclude-glob) y no -x (--exclude): -x es una ERE que no casa los
		# nombres con bytes no-UTF-8 (p. ej. 0xE0) y revienta con un `*` inicial
		# ("Invalid preceding regular expression"). El glob compara byte a byte y
		# además protege esos nombres de --delete.
		excl+=(-X "$item")
	done
	mirror_options
	require_cmd lftp
	[ -r "$key" ] || die "no se puede leer la llave privada $key"
	require_remote_host "$host"
	require_nas
	mkdir -p "$local_dir"
	log "SFTP $user@$host:$port '$remote' -> '$local_dir' (compare=$MIRROR_COMPARE)"
	if ! lftp_program <<EOF
set sftp:connect-program "ssh -a -x -o BatchMode=yes -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new -i $key"
set net:timeout 20
set net:max-retries 4
set net:persist-retries 3
set net:reconnect-interval-base 5
set net:reconnect-interval-max 60
set sftp:auto-confirm yes
$(lftp_common_settings)
open -u "$user","" "sftp://$host:$port"
mirror ${MIRROR_OPTS[*]} ${excl[*]:-} "$remote" "$local_dir"
bye
EOF
	then
		log "no se completó el espejo de $user@$host:$port '$remote' (ver el motivo arriba)"
		return 1
	fi
}

# mirror_sftp_pass host port user pass remote local [exclusion...]
# Igual que mirror_sftp pero con autenticación por contraseña (sshpass -e).
mirror_sftp_pass() {
	local host="$1" port="$2" user="$3" pass="$4" remote="$5" local_dir="$6"
	shift 6
	local -a excl=()
	local item
	for item in "$@"; do
		# exclusiones por glob (-X); ver la nota en mirror_sftp.
		excl+=(-X "$item")
	done
	mirror_options
	require_cmd lftp
	require_cmd sshpass
	require_remote_host "$host"
	require_nas
	mkdir -p "$local_dir"
	log "SFTP(pass) $user@$host:$port '$remote' -> '$local_dir' (compare=$MIRROR_COMPARE)"
	export SSHPASS="$pass"
	if ! lftp_program <<EOF
set sftp:connect-program "sshpass -e ssh -a -x -o BatchMode=no -o PreferredAuthentications=password -o PubkeyAuthentication=no -o StrictHostKeyChecking=accept-new"
set net:timeout 20
set net:max-retries 4
set net:persist-retries 3
set net:reconnect-interval-base 5
set net:reconnect-interval-max 60
set sftp:auto-confirm yes
$(lftp_common_settings)
open -u "$user","" "sftp://$host:$port"
mirror ${MIRROR_OPTS[*]} ${excl[*]:-} "$remote" "$local_dir"
bye
EOF
	then
		unset SSHPASS
		log "no se completó el espejo (password) de $user@$host:$port '$remote' (ver el motivo arriba)"
		return 1
	fi
	unset SSHPASS
}

# mirror_ftp host user pass remote local [exclusion...]
mirror_ftp() {
	local host="$1" user="$2" pass="$3" remote="$4" local_dir="$5"
	shift 5
	local -a excl=()
	local item
	for item in "$@"; do
		# exclusiones por glob (-X); ver la nota en mirror_sftp.
		excl+=(-X "$item")
	done
	mirror_options
	require_cmd lftp
	require_remote_host "$host"
	require_nas
	mkdir -p "$local_dir"
	log "FTP $user@$host '$remote' -> '$local_dir' (compare=$MIRROR_COMPARE)"
	# Los servidores FTP usan certificados autofirmados o con CN que no coincide:
	# se acepta el TLS sin verificar en vez de fallar. LFTP_DEBUG=1 añade trazas.
	local debug_opts=""
	if [ "${LFTP_DEBUG:-0}" = "1" ]; then
		debug_opts=$'debug 5\nset net:verbose true'
	fi
	if ! lftp_program <<EOF
set ftp:ssl-force false
set ftp:ssl-protect-data false
set ftp:ssl-allow true
set ssl:verify-certificate no
set ssl:check-hostname no
set ftp:passive-mode on
set net:timeout 30
set net:max-retries 4
set net:persist-retries 3
set net:reconnect-interval-base 5
set net:reconnect-interval-max 60
$(lftp_common_settings)
${debug_opts:-}
open -u "$user","$pass" "ftp://$host"
mirror ${MIRROR_OPTS[*]} ${excl[*]:-} "$remote" "$local_dir"
bye
EOF
	then
		log "no se completó el espejo FTP de $user@$host '$remote' (ver el motivo arriba)"
		return 1
	fi
}

# Nota: cada job espeja el origen remoto directo a su ruta final en el NAS mediante
# mirror_* (con --delete); no hay staging local.

