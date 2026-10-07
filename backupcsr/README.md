# backupcsr — copias de seguridad

Subsistema de copias que corre en **ubuntu-services (192.168.0.49)**. Cada job espeja el
origen remoto (FTP o SFTP) **directo a su ruta final en el NAS** montado en `/mnt/nas`
(`//192.168.0.179/Backups`), sin staging local. Reemplaza las tareas de Windows (WinSCP/VBS)
y el cron de `ubuntu-docker`.

> `backups.cortexdev.win` es el Proxmox Backup Server; este subsistema es aparte.

## Estructura

```
lib/common.sh                 # helpers lftp: mirror_ftp, mirror_sftp, mirror_sftp_pass
jobs/*.sh                     # un job por origen (ver horarios abajo)
tools/validar-copias.sh       # validador de solo lectura (frescura de cada job)
tools/diagnostico-copias.sh   # monta el NAS y prueba los FTP
tools/diagnostico-latino-encoding.sh  # solo lectura: bytes/espacios de nombres en latino-web
tools/copiar-nombre-invalido.sh       # copia una vez un nombre no-UTF-8 a un nombre válido en el NAS
tools/subir-historiasclinicas.sh  # NAS -> VPS Google (manual, rsync)
conf/*.example                # credenciales y fstab de referencia (los reales NO van a git)
cron/backupcsr.cron           # se instala en /etc/cron.d/backupcsr
logrotate/backupcsr           # se instala en /etc/logrotate.d/backupcsr
install.sh                    # instalador idempotente
web/                          # portal web de administración (copias.cortexdev.win)
```

Rutas en runtime: `/opt/backupcsr` (scripts), `/etc/backupcsr` (credenciales y llaves),
`/var/log/backupcsr/*.log`.

## Instalación

En `ubuntu-services`, desde el checkout del repo:

```bash
sudo backupcsr/install.sh            # paquetes, archivos, credenciales, cron, logrotate y NAS
sudo backupcsr/install.sh --no-apt   # si los paquetes ya están
sudo backupcsr/install.sh --no-nas   # sin tocar /etc/fstab
```

Antes de instalar, colocar los secretos (no versionados):

- `backupcsr/conf/credentials.env` y `backupcsr/conf/nas.creds` (0600), copiados de
  `conf/*.example` y sin valores `CAMBIAR`; o directamente en `/etc/backupcsr/`.
- Llaves de `google-bd` y `latino-bd` en `/etc/backupcsr/keys/{google,latino}` (0600, sin
  passphrase). Vienen de los `.ppk`; alternativa: dejar `backupcsr/keys/*.ppk` (ignorado por
  git) y usar `sudo INSTALL_KEYS=1 backupcsr/install.sh`.

## Jobs y horarios

`cron/backupcsr.cron` (hora Bogotá; el host **debe** estar en `America/Bogota`, ver
"Zona horaria del anfitrión"):

| Job | Origen | Destino NAS |
|-----|--------|-------------|
| `google-bd` | SFTP `GOOGLE_BD_HOST` `/var/www/html/backupsAutomaticos` | `google/backupsAutomaticos` |
| `latino-bd` | SFTP `LATINO_HOST:2200` `/home/chequeos/taskManager/backupsAutomaticos` | `latino/backupsAutomaticos` |
| `ruta56-bd` | FTP `taskManager/ruta56` | `ruta56` (excluye `storage`) |
| `ruta56-web` | FTP `ruta56/storage` | `ruta56/storage/app` |
| `latino-web` | SFTP `LATINO_HOST:2200` `/home/chequeos/educacion` y `/home/chequeos/daruma302.socimedicostools.info` | `latino-web/educacion` y `latino-web/daruma302.socimedicostools.info` |
| `gastro-bd` | FTP `taskManager/gastro` | `gastro` |
| `enter-bd` | FTP `taskManager/pedidos` | `pedidos` |

Los `*-bd` corren cada hora de 6 a 19 con los minutos **escalonados** (`2`, `14`, `26`,
`38`, `50`); los `*-web` 4 veces al día (`3, 10, 16 y 20`), `ruta56-web` a los `:20` y
`latino-web` a los `:8`. Cada línea del cron **encola** la ronda en el planificador
(`backupcsr-scheduler submit <slug>`, ver [Planificador](#planificador-backupcsr-scheduler)),
que aplica los cupos de concurrencia y reintenta los fallos transitorios; las rondas que no
caben **esperan** en la cola, ya no quedan `OMITIDAS` (con el planificador apagado, `flock`
y `MIRROR_GATE` conservan el comportamiento serial anterior). El mirror usa `--delete`: si el
origen remoto queda incompleto, el destino del NAS refleja ese estado.

`ruta56-bd` y `ruta56-web` comparten `/mnt/nas/ruta56`; por eso `ruta56-bd` excluye `storage`
para no borrar lo que publica `ruta56-web`.

### Planificador (`backupcsr-scheduler`)

Daemon systemd (`app/scheduler.py`) que es el **dueño de la concurrencia**. El cron y el
portal encolan por un socket Unix; el daemon lanza los scripts con `MIRROR_GATE=""` y aplica:

| Regla | Variable | Defecto |
|-------|----------|---------|
| Copias simultáneas en total | `BACKUP_MAX_JOBS` | `2` |
| Copias simultáneas por host de origen | `BACKUP_MAX_PER_HOST` | `1` |
| Reintentos por ronda ante fallo transitorio | `BACKUP_RETRY_MAX` | `3` |
| Espera entre reintentos (s, ±`BACKUP_RETRY_JITTER`) | `BACKUP_RETRY_BACKOFF` | `300,900,2700` |

- **`origin_host`** (`jobs.yml` / tabla `jobs`) agrupa por servidor remoto: `latino-bd` y
  `latino-web` comparten `latino`, `ruta56-bd` y `ruta56-web` comparten `ruta56`. Es un cupo
  lógico por servidor, no por job: evita abrir varias sesiones al mismo remoto.
- **Reintentos**: al terminar, el daemon clasifica la corrida con `logs.classify_error`; un
  `transitorio` se reencola con backoff y *jitter* en vez de marcar `FALLO` de inmediato.
  Un pendiente por slug: si llega la ronda horaria mientras hay un reintento esperando, se
  **fusionan** (coalescing). Tras agotar `BACKUP_RETRY_MAX` sí queda `FALLO`.
- **Estado**: `/var/lib/backupcsr/scheduler.json` (atómico). Al reiniciar recarga la cola y
  **reencola una vez** las corridas que estaban en curso.
- **CLI**: `backupcsr-scheduler submit <slug> | status | cancel <slug>`. Cada línea del cron es
  `backupcsr-scheduler submit <slug> || exec flock -n /run/lock/backupcsr-<slug>.lock <script>`:
  si el daemon o el binario no están, el propio shell ejecuta el script con `flock` (serial), así
  una caída del portal no detiene las copias. El portal hace lo mismo (`app/runner.py`). Con
  `BACKUP_SCHEDULER=0` el daemon no se arranca (`install.sh` lo condiciona) y todo vuelve al modo
  serial anterior.
- **API**: `GET /api/jobs/scheduler` expone cupos, cola y corridas.

Validación rápida: `systemctl status backupcsr-scheduler`, `backupcsr-scheduler status` y
`journalctl -u backupcsr-scheduler -f`. Para probar sin copiar datos:
`DRY_RUN=1` en una ronda encolada (`--action dry`).

### Zona horaria del anfitrión

El cron de Ubuntu/Debian **no implementa `CRON_TZ`**: la cadena no existe en `/usr/sbin/cron`
y la línea `CRON_TZ=` solo exporta la variable a los jobs, no programa en esa zona. Los
horarios del cron se evalúan en la **zona del anfitrión**, así que `ubuntu-services` debe
estar en `America/Bogota`:

```bash
sudo timedatectl set-timezone America/Bogota    # install.sh lo hace salvo SET_TIMEZONE=0
sudo systemctl restart cron
timedatectl                                       # Time zone: America/Bogota
```

Con el host en UTC los jobs `6-19` corren en realidad `01:00-14:00` Colombia: dejan de
disparar por la tarde y hay que ejecutarlos a mano; además los logs quedan en hora UTC y el
portal/validador los interpretan con el desfase (estado `TARDE`). `validar-copias.sh` avisa
con un `ERROR` si detecta host y cron en zonas distintas. Si la zona del host no se puede
cambiar, hay que reescribir las horas del cron (y del catálogo/BD) a la zona del host.

### Tareas deshabilitadas

Quedaron fuera de la migración y están **registradas, deshabilitadas, en el catálogo**
(`web/jobs.yml`) para no perderlas: no tienen script en `/opt/backupcsr/jobs`, así que no
entran al cron, ni al validador, ni se miden en el panel (y el portal no avisa por ellas).

| Tarea | Origen | Destino | Por qué quedó fuera |
|-------|--------|---------|---------------------|
| `google-web` | SFTP `GOOGLE_WEB_HOST` `/var/www/html/centro-apoyo/assets`, `/var/www/html/sapg/assets` y `/var/www/html/historiasclinicas/files` | `google-web` | Publicaba a OneDrive (vetado); script corregido, sigue deshabilitado |
| `ticware-bd` | FTP `TICWARE_HOST`, ruta por confirmar | por confirmar | Ticware no debe llenar el NAS |
| `ticware-web` | FTP `TICWARE_HOST`, ruta por confirmar | por confirmar | Ticware no debe llenar el NAS |

`borrar_lista.sh` (limpieza destructiva con rutas del VPS) tampoco se registró: no es una copia.

Para habilitar una: ajustar `source`/`dest_rel` en `web/jobs.yml`, reinstalar (`sudo install -m
0755 jobs/<slug>.sh /opt/backupcsr/jobs/`) y habilitarla en el panel. El portal **rechaza
habilitarla si falta el script** (`409`), para que no quede un horario apuntando a un archivo
inexistente. Ojo con `google-web`: ya tiene script corregido (espeja a `/mnt/nas/google-web`);
si se habilita, revisar antes el destino (con `--delete` borraría del NAS lo que aún no esté en
el VPS) y, para conservar el flujo de `subir-historiasclinicas`, apuntar su `SRC_DIR` a
`/mnt/nas/google-web/historiasClinicas/files`.

## Validación

```bash
sudo validar-copias              # informe completo
sudo validar-copias --quiet      # solo problemas (útil en cron/monitoreo)
sudo validar-copias --grace 120  # tolerancia para jobs en curso
```

Salida: `0` = todo OK, `1` = advertencias, `2` = errores. Revisa cron, credenciales, llaves,
NAS y la frescura de cada job (leyendo `/etc/cron.d/backupcsr`).

| Estado | Significado |
|--------|-------------|
| `OK` | Cerró con `=== fin <job> ===` después de su última ejecución esperada |
| `PARCIAL` | Cerró bien pero con archivos que fallaron (`AVISO: N archivos con error`) |
| `EN_CURSO` | Empezó y aún no cierra, dentro de la gracia |
| `OMITIDO` | No consiguió turno de la cola; se reintenta en el próximo ciclo (no es fallo) |
| `TARDE` | Cerró bien pero antes de la última ejecución esperada (revisar horario/zona) |
| `NUNCA` | No existe el log del job |
| `FALLO` | Terminó en `FALLO:`/`ERROR:` o quedó a medias pasada la gracia |
| `DESCONOCIDO` | No se pudo interpretar el log |

## Alertas y errores

Los scripts emiten un **contrato de líneas** que el portal interpreta para clasificar cada
corrida (ver `lib/common.sh`):

- `AVISO: reintento N/M por error transitorio` → se reintenta con backoff (`MIRROR_ATTEMPTS`,
  `RETRY_BACKOFF`) hasta agotar; luego `ERROR: ... [transitorio]`.
- `AVISO: N archivos con error` → el mirror terminó con fallos por archivo: el job cierra
  `PARCIAL` (hasta `PARTIAL_MAX`, por defecto 50) y se listan los 5 primeros en el historial.
- `AVISO: N entradas no representables` → nombres que CIFS no admite (bytes no UTF-8,
  barra invertida o enlace simbólico): se toleran aparte (`UNREPR_MAX`, por defecto 200) y
  la ronda cierra `PARCIAL`; no cuentan contra `PARTIAL_MAX` para no ocultar errores reales.
- `AVISO: cola ocupada` + `OMITIDO:` → la ronda no consiguió turno; estado `OMITIDO`.
- `ERROR:` / `FALLO:` / `PROCESO: exit=N` → fallo definitivo y código de salida real.

`require_remote_host` (`lib/common.sh`) aborta antes de conectar con `ERROR: el host de
origen '...' resuelve solo a loopback` si el DNS del origen apunta a `127.0.0.1`/`::1`
(caso de `ftp.gastronomia33app.com`): evita reintentos inútiles y lo clasifica como
`fatal` (configuración), no como `transitorio`. El ruido de limpieza de lftp
(`rm: Access failed: ... .lftp: No such file or directory`) se ignora, no es un fallo.

Clasificación y severidad (portal): `fatal` (auth/NAS/destino) → **Crítico**; `transitorio`
→ **Aviso** y solo tras 2 corridas no-OK consecutivas (debounce); `contencion` (`OMITIDO`)
→ **Aviso** "ronda omitida"; `parcial` → **Aviso** con el número de archivos. La
`criticality` del job puede subir la severidad, nunca bajarla. Los incidentes se guardan en
la tabla `incidents` (abierto/actualizado/resuelto; `GET /api/incidents`) y el detalle en
`runs.error_class`/`runs.error_lines`.

Aviso por correo (opcional, `conf/web.env.example` → `BACKUP_ALERT_*`): `fatal` inmediato,
resto en un resumen diario, y aviso de recuperación al resolverse. Programar el resumen y
la retención:

```cron
0 8 * * *    root  bash -c 'set -a; . /etc/backupcsr/web.env; set +a; cd /opt/backupcsr/web && venv/bin/python -m app.cli notify-digest'
30 4 * * 0   root  bash -c 'set -a; . /etc/backupcsr/web.env; set +a; cd /opt/backupcsr/web && venv/bin/python -m app.cli prune'
```

`prune` borra corridas de más de 180 días e incidentes resueltos de más de 365; los logs
rotan a 16 semanas (`logrotate/backupcsr`).

### Nombres con bytes no UTF-8 (latino-web)

El montaje CIFS usa `iocharset=utf8`: un nombre que **no sea UTF-8 válido** no se puede crear
en el NAS y lftp lo reporta como `No such file or directory` sobre la ruta del destino (el
directorio padre sí existe). No es la conexión ni la llave. Caso real: `RESOLUCIàN No 00034.pdf`
con la `à` codificada en Latin-1 (byte `0xE0`), que no es UTF-8; el job la excluye y cierra
`PARCIAL` (aviso), no `FALLO`.

La exclusión se pasa a lftp como **glob** (`-X`/`--exclude-glob`), no como `-x`. `-x` es una
expresión regular: no casa los nombres con bytes no-UTF-8 y además falla con un `*` inicial
(`Invalid preceding regular expression`), que era justo lo que dejaba la ronda en `PARCIAL`. El
glob compara byte a byte y también protege el nombre ya copiado del `--delete` del espejo.

`tools/diagnostico-latino-encoding.sh` (solo lectura) muestra el nombre real con `%q` y en
hex, en el NAS y en el origen, y si el archivo llegó a crearse:

```bash
sudo /opt/backupcsr/tools/diagnostico-latino-encoding.sh
```

`jobs/latino-web.sh` excluye el patrón inválido con un glob (`RESOLUCI*N*No*00034.pdf`): la
ronda cierra sin error y el archivo **se ignora** (no se copia). El glob también protege del
`--delete` cualquier copia con ese nombre que ya esté en el NAS; si se quiere quitar, borrarla a
mano:

```bash
sudo rm -f "/mnt/nas/latino-web/daruma302.socimedicostools.info/daruma_original/web/uploads/staff/assets/user14/CONCILIACIONES JUDICIALES Y EXTRAJUDICIALES/RESOLUCION_No_00034.pdf" \
           "/mnt/nas/latino-web/daruma302.socimedicostools.info/daruma_original/web/uploads/staff/assets/user14/CONCILIACIONES JUDICIALES Y EXTRAJUDICIALES/RESOLUCIàN No 00034.pdf"
```

Alternativa (no usada hoy): renombrar el archivo en el origen a un nombre UTF-8 válido, si no
está referenciado por nombre. `tools/copiar-nombre-invalido.sh` sirve para copiarlo a un nombre
válido si en algún momento se decide conservarlo.

### Otras entradas no representables en el NAS (latino-web)

El origen de `daruma302.socimedicostools.info` trae, además de bytes no UTF-8, entradas que
CIFS no puede representar: nombres con **barra invertida** (`\`), que lftp reporta como
`Invalid argument`, y **enlaces simbólicos**, que reporta como
`symlink(...): Operation not supported`. `run_mirror` (`lib/common.sh`) cuenta estas
entradas por separado (`UNREPR_MAX`, por defecto 200) de los errores reales
(`PARTIAL_MAX`): la ronda cierra **PARCIAL** (aviso, con la lista de rutas en el historial)
sin que un fallo real quede disfrazado de "parcial". Para eliminarlas del todo hay que
renombrar en el origen o cambiar el `iocharset`/formato del montaje CIFS.

## Cuándo compite con el servidor (RAM, NAS, CPU)

Síntoma: durante las copias el servidor se siente bloqueado (panel lento, SSH que no
responde). Medido en `ubuntu-services` (2 núcleos, 4 GB de RAM): con los mirrors a la vez el
swap se llenaba y el load subía, aunque las copias son **I/O**, no CPU: el recorrido y la
escritura del NAS (CIFS) llenan la caché y el kernel manda al swap a todo lo demás (nginx,
MySQL, panel, agentes). El planificador limita la concurrencia para que eso no pase.

Lo que ya hace el repo:

| Medida | Dónde | Efecto |
|--------|-------|--------|
| `--ignore-time` (`MIRROR_COMPARE=size`) | `lib/common.sh` | Transfiere solo lo nuevo y lo que cambió de tamaño: se acabaron las rebajas de miles de archivos idénticos cada hora |
| `mirror:overwrite` + `xfer:use-temp-file` | `lib/common.sh` | Reemplaza el archivo sin borrarlo antes; la escritura es temporal+rename (atómica) |
| Cola global (`MIRROR_GATE`) | `lib/common.sh` | Respaldo serial: un solo job de copia a la vez si se ejecuta un script directo; con el planificador los jobs se lanzan con `MIRROR_GATE=""` y el daemon aplica los cupos |
| Planificador (cupos global y por host) | `app/scheduler.py`, `jobs.yml` | 2 copias a la vez y 1 por `origin_host`; las rondas que no caben esperan, sin `OMITIDO` |
| Reintentos de ronda (`BACKUP_RETRY_*`) | `app/scheduler.py` | Un fallo transitorio se reencola con backoff y coalescing en vez de marcar `FALLO` al primer intento |
| Reintentos de red (`MIRROR_ATTEMPTS`, `RETRY_BACKOFF`) | `lib/common.sh` | Reintenta la conexión ante errores transitorios (timeout, `max-retries`) antes de fallar |
| Tolerancia por archivo (`PARTIAL_MAX`) | `lib/common.sh` | Unos pocos archivos con error real no tumban el job: cierra `PARCIAL` y los lista |
| Tolerancia a no representables (`UNREPR_MAX`) | `lib/common.sh` | Nombres no UTF-8, barras invertidas y enlaces se cuentan aparte y no disparan `FALLO` |
| `nice`/`ionice` (`JOB_NICE=15`, clase 2 prio 7) | `lib/common.sh` | Las copias ceden CPU e I/O al resto de servicios |
| Minutos escalonados | `cron/backupcsr.cron`, `jobs.yml` | Los 6 jobs ya no arrancan en el mismo minuto |
| No medir tamaños mientras el job corre | `web/app/sizes.py` | El portal no recorre con `du` un árbol que se está escribiendo (`force=True` —refresco manual— sí lo hace) |
| Tope de memoria por contenedor | `docker-compose.yml` | nginx 96 MB, portal-api 192 MB, prometheus/grafana 384 MB |

Ajustes por job (se ponen antes del `source` de la librería en `jobs/*.sh` o en
`/etc/backupcsr/credentials.env`): `MIRROR_PARALLEL`, `MIRROR_COMPARE`, `MIRROR_GATE` (vacío =
sin cola), `GATE_WAIT`, `JOB_NICE`, `MIRROR_RATE_LIMIT` (p. ej. `5M`), `MIRROR_ATTEMPTS`,
`RETRY_BACKOFF`, `PARTIAL_MAX`, `UNREPR_MAX`, `SKIP_EXIT`.
Nota: `GATE_WAIT` se lee al entrar a `job_init`, antes de `load_credentials`; los valores que
vengan de `credentials.env` no le afectan (se aplican tras la cola).

Recomendaciones de anfitrión (fuera del repo):

1. Recursos recomendados: **8 GB de RAM y 4 vCPU** (con 6 GB y 2 vCPU el tope global debe
   quedarse en 2). Verificar el real con `nproc`, `free -m` y `cat /proc/meminfo`; el cuello de
   botella es el NAS CIFS y la red al remoto, no la CPU.
2. Dar aire al swap: `zram` o un swapfile extra; sin margen, `Committed_AS` supera el
   `CommitLimit` y el kernel manda al swap a los servicios.
3. Mover fuera de este servidor las sesiones interactivas (VS Code/agentes): entre sus procesos
   sumaban ~1 GB de RSS y ~450 MB de swap.
4. Reducir el trabajo en el NAS: con los dumps diarios, los `*-bd` pueden pasar a
   `6-19/2` (cada 2 h) o a horas fijas; el `--ignore-time` ya baja el volumen por corrida.
5. Opcional, en el montaje CIFS: bajar `rsize/wsize` de 4M a 1M en
   `conf/fstab.backupcsr.example` (menos memoria en vuelo por operación).

Prueba en seco (no descarga ni sube nada — `DRY_RUN=1` usa `--dry-run` de lftp; ojo: `-n` en
`mirror` es `--only-newer`, no simulación) y diagnóstico:

```bash
sudo DRY_RUN=1 /opt/backupcsr/jobs/ruta56-bd.sh
tail -n 50 /var/log/backupcsr/ruta56-bd.log
sudo LFTP_DEBUG=1 DRY_RUN=1 /opt/backupcsr/jobs/ruta56-bd.sh   # detalle de la conexión
sudo bash backupcsr/tools/diagnostico-copias.sh                # NAS + los tres FTP
sudo journalctl -u cron --since today | grep backupcsr
free -m; cat /proc/loadavg; grep -E "SwapFree|Committed_AS|CommitLimit" /proc/meminfo
```

Aplicar estos cambios en el servidor (los jobs viven en `/opt`, no en el repo):

```bash
sudo backupcsr/install.sh --no-apt                     # lib/ + jobs/ + tools/ a /opt
sudo backupcsr/web/install.sh --no-apt                 # portal + CLI del planificador + systemd
sudo install -m 0644 backupcsr/cron/backupcsr.cron /etc/cron.d/backupcsr   # encola por el planificador
sudo docker compose up -d                              # topes de memoria de los contenedores
sudo DRY_RUN=1 /opt/backupcsr/jobs/enter-bd.sh && tail -n 5 /var/log/backupcsr/enter-bd.log
sudo /opt/backupcsr/bin/backupcsr-scheduler status     # cola y cupos del daemon
sudo /opt/backupcsr/jobs/enter-bd.sh && grep -c "Transferring file" /var/log/backupcsr/enter-bd.log
```

> El cron invoca `/opt/backupcsr/bin/backupcsr-scheduler` con respaldo `flock`+script a nivel de
> shell, así que si falta el portal igual corre la copia. Arranca el planificador con
> `BACKUP_SCHEDULER=1` en `/etc/backupcsr/web.env` (con `0`, `install.sh` no lo arranca y se usa
> el modo serial).

Si el portal tiene `BACKUP_MANAGE_CRON=1`, el horario efectivo sale de la tabla `jobs`: cambia
los minutos en el panel de Programación (o en la BD) para que el escalonado no se pierda al
reescribir el cron. El planificador protege en cualquier caso (y `MIRROR_GATE` si se ejecuta
un script a mano).

## Subida puntual al VPS (`subir-historiasclinicas`)

`tools/subir-historiasclinicas.sh` sube `/mnt/nas/files` a
`/var/www/html/historiasclinicas/files` del VPS Google con `rsync` sobre SSH (aditivo, sin
`--delete`, reanudable). Es manual, no está en cron:

```bash
sudo DRY_RUN=1 subir-historiasclinicas            # o tools/subir-historiasclinicas.sh
sudo subir-historiasclinicas
```

Variables: `DRY_RUN`, `ONLY_MISSING` (por defecto 1), `COUNT_FILES`, `RSYNC_VERBOSE`,
`SKIP_PREFLIGHT`, `SRC_DIR`, `DST_DIR`. Log en `/var/log/backupcsr/subir-historiasclinicas.log`.

## Portal web (web/)

Interfaz de administración en **https://copias.cortexdev.win** (no es `backups.cortexdev.win`,
que es PBS): estado de los jobs, ejecución manual (real o `DRY_RUN`), historial de corridas con
archivos, navegador y **gestión de `/mnt/nas`** (nueva carpeta, renombrar, mover, subir, descargar
y eliminar; solo admin, auditado y bloqueado mientras el job corre) y, opcionalmente, edición del
horario.

```bash
sudo backupcsr/web/install.sh     # venv + deps + systemd (backupcsr-web) + schema + admin
sudo BACKUP_MANAGE_CRON=1 ...     # fase 2: el portal reescribe /etc/cron.d/backupcsr
```

Detalle (requisitos de MySQL, fases, API, seguridad y diagnóstico) en
[`web/README.md`](web/README.md). El backend corre nativo por systemd (127.0.0.1:8089) y usa el
mismo `flock` que cron; `validar-copias.sh` no cambia.

## Monitoreo continuo

```cron
30 8 * * *  root  /opt/backupcsr/tools/validar-copias.sh --quiet || echo "backupcsr: revisar" | mail -s "backupcsr" tu@correo
```

## Corte desde ubuntu-docker

Nunca deben correr los dos hosts a la vez (ambos espejan con `--delete` al mismo destino).
Tras validar en `ubuntu-services`:

```bash
sudo mv /etc/cron.d/backupcsr /etc/cron.d/backupcsr.disabled
sudo systemctl reload cron
```

## Secretos

`conf/credentials.env`, `conf/nas.creds` y las llaves están en `.gitignore`: **nunca** se
versionan (solo los `.example`). El repo debe ser privado.
