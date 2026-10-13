# backupcsr/web — portal de administración de copias

Portal web (Bootstrap 5) para administrar el subsistema `backupcsr/` que corre en
**ubuntu-services (192.168.0.49)**. Se sirve en **https://copias.cortexdev.win** (no confundir
con `backups.cortexdev.win`/`pbs`, que es Proxmox Backup Server).

## Qué permite

- **Panel**: app-shell empresarial (barra lateral + topbar) con KPIs (GB almacenados, crecimiento
  7/30 días, jobs OK/fallidos, duración media, NAS libre), gráficos de tendencia y tabla de jobs
  con estado (OK / EN CURSO / TARDE / FALLÓ / NUNCA / DESHABILITADA), **progreso**, tamaño,
  última y próxima ejecución.
  La tabla usa **selección múltiple estilo DataTables** (clic selecciona, Ctrl/Cmd+clic alterna,
  Mayús+clic toma un rango; sin casillas por fila) y **acciones masivas** en la barra superior
  (ejecutar, reintentar fallidas, dry-run, medir tamaños, habilitar/deshabilitar, exportar CSV),
  encoladas en el **planificador** (`app/scheduler.py`), que limita la concurrencia (2 copias a
  la vez, 1 por host) y reintenta los fallos transitorios. Ver
  [Planificador](../README.md#planificador-backupcsr-scheduler). Las
  acciones específicas de cada tarea viven en el menú **⋮** de la fila y las columnas **Destino**
  y **Programación** se muestran en una fila de detalle tras el signo **+** (estilo DataTables
  *Responsive*).
  Cada fila muestra la **criticidad** (badge) y las **etiquetas**, y el buscador filtra además por
  etiquetas, responsable y notas (ver [Ficha de la tarea](#ficha-de-la-tarea)).
- **Progreso por tarea**: mientras la copia corre, la columna **Estado** (no hay columna extra)
  muestra el **porcentaje solo si es real** (medido; hoy los scripts no lo publican, así que lo
  normal es el tiempo); si no hay porcentaje real, el **tiempo transcurrido** va dentro del propio
  badge `EN CURSO` (`EN CURSO · 12m`) y sin ninguno de los dos queda `EN CURSO` a secas. El tiempo
  se refresca con el reloj local, no solo cada 30 s. En reposo no se muestra barra ni tiempo.
  Ver [Progreso y acciones masivas](#progreso-y-acciones-masivas).
- **Tamaño (GB) por tarea**: recorrido cacheado del destino de cada job en `/mnt/nas`, con
  histórico (`size_snapshots`) para ver crecimiento. `ruta56-bd` excluye `storage` (`size_exclude`
  en `jobs.yml`) para no contar dos veces el árbol compartido con `ruta56-web`.
- **Historial**: corridas con duración, tamaño, archivos transferidos/eliminados, log, detalle y
  **export CSV/JSON** del filtro actual.
- **Archivos**: navegador de `/mnt/nas` por job, con tamaño medido y **gestión** (admin):
  nueva carpeta, renombrar, mover, subir, descargar y eliminar, con confirmación y auditoría.
  Ver [Gestión de archivos](#gestión-de-archivos-pestaña-archivos).
- **Programación**: el editor de tarea tiene dos pestañas: **General** (la ficha, ver
  [Ficha de la tarea](#ficha-de-la-tarea)) y **Programación** (habilitar/deshabilitar, cron y
  `lockfile`, con **presets y previsualización** de las próximas 5 ejecuciones); con la gestión
  activa reescribe `/etc/cron.d/backupcsr`.
  Una tarea `enabled: false` en `jobs.yml` se lista con la etiqueta *deshabilitada*, sin línea en el
  cron, sin alertas y sin medición de tamaño; el portal **no deja habilitarla si falta
  `/opt/backupcsr/jobs/<slug>.sh`** (`409`). Así se registran tareas que hoy no corren (ver
  `README.md`, "Tareas deshabilitadas").
- **Usuarios** (admin): alta, rol `admin`/`viewer`, activar/desactivar y reseteo de contraseña;
  cada usuario puede cambiar la suya.
- **Auditoría** (admin): quién ejecutó, editó, deshabilitó o gestionó usuarios y cuándo.
- **Notificaciones**: botón de campana en la barra superior (con contador) que abre el listado de
  jobs fallidos/atrasados, MySQL no disponible y desfase de reloj, en tarjetas con icono y nivel.
  Ya no se muestran dentro del panel.
- **Almacenamiento NAS**: el disco con anillo de uso se muestra siempre en el panel, en la misma
  fila que los gráficos de tendencia y duración (verde/ámbar/rojo según el uso).

## Tema

Bootstrap 5.3 y **Font Awesome 6.5.2** vendoreados en `static/vendor/` (CSS + webfonts), y
**tema claro por defecto** (oscuro con el mismo toggle, persistido en `localStorage['cortexdev-theme']`).
Los gráficos del panel son SVG propios embebidos (`static/js/chart.js`): sin Chart.js, sin CDN ni
build, para que el portal funcione en LAN/Tailscale sin red.

El frontend se sirve **en vivo** desde `backupcsr/web/static` (bind mount de nginx), pero el backend
corre desde `/opt/backupcsr/web`: tras cambiar `app/` hay que **reinstalar** (`install.sh`) y
reiniciar el servicio, o el navegador pedirá endpoints que el backend viejo no tiene (404).

## Zona horaria y datos derivados

Los jobs escriben sus logs con la **hora local del host**, no con `CRON_TZ`. El portal interpreta
esos timestamps en la zona del host y los convierte a `BACKUP_TZ` (por defecto `America/Bogota`)
antes de comparar, así que duraciones, `TARDE` y la alerta de desfase son correctas aunque el host
no esté en `BACKUP_TZ`. Si el host está en UTC verás la advertencia "logs por delante del reloj"
solo cuando el desfase real supere 10 min.

`runs`/`run_files` son datos **derivados** de los logs. Si se corrige la zona horaria con que se
guardaron, hay que regenerarlos (si no, se duplicarían con horas desplazadas):

```bash
cd /opt/backupcsr/web && venv/bin/python -m app.cli resync-runs --dry-run   # ver qué haría
cd /opt/backupcsr/web && venv/bin/python -m app.cli resync-runs             # aplicar
```

Los tamaños (`size_snapshots`) se miden con `snapshot-sizes` o al cerrar cada corrida.

## Arquitectura

```
navegador → nginx (access_nginx :443) → /api/ → 127.0.0.1:8089 (backupcsr-web, systemd, root)
                                                    ├─ MySQL (ubuntu-docker .87, esquema backupcsr)
                                                    ├─ /var/log/backupcsr/*.log[.1|.gz]
                                                    ├─ /etc/cron.d/backupcsr   (solo MANAGE_CRON=1)
                                                    ├─ /opt/backupcsr/jobs/*.sh (flock)
                                                    └─ /mnt/nas (solo lectura)
```

El backend corre **nativo con systemd** (no en contenedor) porque necesita root para leer el NAS
y los logs, y para lanzar los jobs con el mismo `flock` que usa cron. nginx (contenedor con
`network_mode: host`) proxifica `/api/`.

## Requisitos previos

1. **MySQL del docker de ubuntu-docker** publicado en la LAN. Por defecto `mysql_ci` escucha solo
   en `127.0.0.1:3306`; hay que mapearlo a `192.168.0.87:3306:3306` en su compose y recrear el
   contenedor (`docker compose up -d`). Confirmar antes que el contenedor tenga un volumen
   persistente (`docker inspect mysql_ci --format '{{json .Mounts}}'`): si no lo tiene o un
   pipeline de CI lo limpia, usar un MySQL dedicado con volumen propio.
2. **Base y usuario**: ejecutar `sql/bootstrap.sql` como admin de MySQL (cambiar la clave). El
   usuario `backupcsr_web` siempre lleva password fuerte; nunca vacío.
3. **`/etc/backupcsr/web.env`** (0600 root) a partir de `conf/web.env.example`, con `BACKUP_DB_*`,
   `BACKUP_SECRET_KEY`, `BACKUP_MANAGE_CRON` y, si se usa el planificador, `BACKUP_SCHEDULER=1`
   (más `BACKUP_MAX_JOBS`, `BACKUP_MAX_PER_HOST`, `BACKUP_RETRY_*` y `BACKUP_JOB_TIMEOUT`); con
   `0`, `install.sh` no arranca `backupcsr-scheduler` y el cron usa el respaldo serial (que
   produce rondas `OMITIDO`).

## Instalación

```bash
# En ubuntu-services, desde el checkout del repo:
sudo backupcsr/web/install.sh          # venv + deps + systemd + schema + admin
sudo backupcsr/web/install.sh --no-apt # si python3-venv ya está
```

Encender o apagar el planificador sin reinstalar todo:

```bash
sudo backupcsr/web/install.sh --enable-scheduler    # BACKUP_SCHEDULER=1 + enable --now
sudo backupcsr/web/install.sh --disable-scheduler   # vuelve al modo serial
```

Luego, para publicar la UI:

```bash
docker compose up -d --force-recreate ingress
docker exec access_nginx nginx -t && docker exec access_nginx nginx -s reload
scripts/dns-overrides.sh               # agrega copias.cortexdev.win -> 192.168.0.49
scripts/check.sh
```

Si no defines `BACKUP_ADMIN_PASSWORD`, el instalador genera una y la deja en
`/etc/backupcsr/admin-password` (0600). Para fijarla o cambiarla a mano:

```bash
cd /opt/backupcsr/web && venv/bin/python -m app.cli create-admin --username admin
```

## Fases (gestión de cron)

- **Fase 1** (`BACKUP_MANAGE_CRON=0`, por defecto): el portal observa, ejecuta e historifica; el
  horario se muestra pero no se edita. La vista *Programación* muestra un aviso y el editor abre
  en modo solo lectura (el botón Guardar se deshabilita), en lugar de fallar al guardar.
- **Fase 2**: validar que el render coincide con el archivo actual:
  ```bash
  cd /opt/backupcsr/web && venv/bin/python -m app.cli render-cron   # 0 = coincide
  ```
- **Fase 3** (`BACKUP_MANAGE_CRON=1`): habilitar/deshabilitar/editar pasa a reescribir
  `/etc/cron.d/backupcsr` (backup en `/etc/backupcsr/cron-backups/` fuera de `cron.d`, escritura
  atómica, validación de sintaxis y `systemctl reload cron`). Reiniciar:
  `systemctl restart backupcsr-web`.

`tools/validar-copias.sh` sigue leyendo el cron generado y ahora además avisa si dos jobs
comparten minuto (señal de que el cron se reescribió con una BD desviada).

### Ficha de la tarea

Cada tarea tiene una ficha editable (pestaña **General** del editor, y botón **Ficha** en el Panel):

- **Descriptivos**: nombre, descripción y orden.
- **Operativos**: tipo de origen, origen y destino. `lockfile` vive en la pestaña *Programación*
  porque cambia el comando del cron. El **script real** (`/opt/backupcsr/jobs/<slug>.sh`) es el que
  manda en la ejecución: el editor muestra una sección *Ver script real* y avisa (best-effort) si el
  origen o el destino de la ficha no aparecen en el script; en tareas multi-ruta el aviso puede
  sobrar, por eso solo informa.
- **Nuevos**: criticidad (alta/media/baja), responsable, retención (días), etiquetas, exclusiones de
  tamaño y notas.

Permisos: la **ficha** la edita cualquier `admin` sin necesidad de `BACKUP_MANAGE_CRON`; habilitar,
cron y `lockfile` exigen `admin` **y** `BACKUP_MANAGE_CRON=1`. Un `viewer` solo puede verla.

Autoridad: **la BD manda tras el alta**. `sync-jobs` inserta las tareas nuevas desde `jobs.yml` y en
las existentes no pisa la ficha (ni `enabled`/cron); los campos nuevos se siembran del YAML solo si
están vacíos (así una instalación anterior hereda `size_exclude: storage` de `ruta56-bd`).

### Horario: el cron instalado manda

El **cron instalado** (`/etc/cron.d/backupcsr`) es la fuente de verdad del **estado** y de la
**próxima ejecución**: es lo que realmente dispara. La tabla `jobs` de la BD es el horario
**editable** del portal y puede quedar desviada (p. ej. tras copiar el cron del repo con
`install.sh`, porque `sync-jobs` no pisa los horarios de filas existentes).

- Con desvío, la vista *Programación* marca **desvío** (badge + `BD: …`); con
  `BACKUP_MANAGE_CRON=1` la campana añade una alerta agregada `kind: schedule`. Guardar en el
  editor adopta el horario efectivo y limpia el desvío.
- `adopt-cron` alinea la BD con el cron instalado **sin** tocar `enabled` ni reescribir el
  archivo; `install.sh` lo ejecuta tras instalar el cron:
  ```bash
  cd /opt/backupcsr/web && venv/bin/python -m app.cli adopt-cron --dry-run   # ver cambios
  cd /opt/backupcsr/web && venv/bin/python -m app.cli adopt-cron             # aplicar
  ```

## Crear tareas desde el portal

Con `BACKUP_MANAGE_JOBS=1` (y rol `admin`) el Portal muestra **Nueva tarea**, que crea una
tarea de copia completa: script en `/opt/backupcsr/jobs/<slug>.sh`, credenciales, catálogo,
fila en la BD y (si `BACKUP_MANAGE_CRON=1`) su línea de cron. Se puede **partir de cero**
(plantilla por tipo de origen) o **clonar** una tarea existente (precarga tipo de origen y
rutas). El modal tiene pestañas **General**, **Rutas**, **Credenciales** y **Programación**,
más una **vista previa** del script y de las próximas ejecuciones antes de guardar.

- **Rutas**: una o varias parejas origen remoto → destino relativo al NAS (`/mnt/nas/<dest>`).
  Exclusiones (glob) y `GATE_WAIT` opcionales.
- **Credenciales**: se reutiliza un grupo existente por su prefijo (se listan los prefijos de
  `/etc/backupcsr/credentials.env`, **sin mostrar valores**) o se crea uno nuevo
  (`<PREFIJO>_HOST/_PORT/_USER/_PASS`, `0600`). La llave SFTP es un nombre de archivo bajo
  `/etc/backupcsr/keys/` (no un secreto) y viaja en la definición.
- **Sin `BACKUP_MANAGE_CRON=1`** la tarea se crea **deshabilitada** (sin horario); se habilita
  al activar la gestión de cron.
- Las tareas creadas por el portal viven en un **overlay durable**
  `/etc/backupcsr/jobs.d/<slug>.yml` (sobrevive a `install.sh`, que sobrescribe
  `/opt/backupcsr/web/jobs.yml`). `catalog.py` fusiona `jobs.yml` + el overlay, así que
  `sync-jobs` no las deshabilita.
- Se pueden **re-editar** (regenera script/overlay/cron) y **eliminar** (borra script, overlay
  y línea de cron, deshabilita la fila de BD y **conserva el histórico**; las credenciales no
  se borran solas porque pueden ser compartidas). Todo queda en la **Auditoría**
  (`job-create`, `job-update`, `job-delete`).

Endpoints: `POST /api/jobs`, `PUT /api/jobs/{slug}`, `DELETE /api/jobs/{slug}`,
`POST /api/jobs/preview`, `GET /api/jobs/{slug}/definition`, `GET /api/credentials/groups`
(admin; `POST`/`PUT`/`DELETE` exigen además `BACKUP_MANAGE_JOBS=1`).

## Alertas e incidentes

Cada corrida se clasifica (`app/logs.py`) a partir del contrato de líneas del job
(`OMITIDO:`, `AVISO: N archivos con error`, `ERROR: ... [transitorio]`, `PROCESO: exit=N`) en
`fatal`, `transitorio`, `contencion` o `parcial`, y se guarda en `runs.error_class` /
`runs.error_lines` (+ `exit_code` y `triggered_by`). La tabla `incidents` mantiene **un
incidente abierto por job** (se abre en no-OK, se actualiza con `count`/`consecutive` y se
resuelve al volver `OK`); `contencion` no abre incidente.

`build_alerts` (campana) aplica severidad y debounce: `fatal` → Crítico; `transitorio` →
Aviso solo con 2 corridas no-OK consecutivas; `contencion` → Aviso solo tras **3 rondas
`OMITIDO` seguidas** (una omisión aislada no alarma); `parcial` → Aviso. Si
`BACKUP_SCHEDULER=1` y el socket del planificador no responde, se añade un aviso
`kind="scheduler"` (el cron está usando el respaldo serial). La `criticality` del job sube
severidad, no la baja. Estados `OMITIDO`/`PARCIAL` no alteran la frescura (se evalúa con la
última corrida real).

Aviso externo opcional (`app/notify.py`, `BACKUP_ALERT_*` en `web.env`): `fatal` inmediato,
resumen diario (`venv/bin/python -m app.cli notify-digest`) y recuperación; sin
`BACKUP_ALERT_MAIL_TO` es no-op. Retención: `venv/bin/python -m app.cli prune`
(`runs` 180 d, incidentes resueltos 365 d).

## API

Todo bajo `/api/`, con sesión salvo `/api/health` y `/api/auth/login`.

- `GET /api/health`, `GET /api/summary`, `GET /api/jobs`, `GET /api/jobs/{slug}`
- `GET /api/sizes?days=30` (series de tamaño), `POST /api/jobs/{slug}/size/refresh` (admin)
- `POST /api/jobs/{slug}/run` `{dry_run,retry}` (admin), `PATCH /api/jobs/{slug}` (admin; la ficha
  solo exige admin, `enabled`/cron/`lockfile` exigen `MANAGE_CRON`),
  `POST /api/jobs/{slug}/reset-schedule` (admin), `GET /api/jobs/{slug}/cron-preview` (admin),
  `GET /api/jobs/{slug}/script` (script real en solo lectura + `drift`)
- `POST /api/jobs` / `PUT /api/jobs/{slug}` / `DELETE /api/jobs/{slug}` (admin +
  `BACKUP_MANAGE_JOBS=1`), `POST /api/jobs/preview`, `GET /api/jobs/{slug}/definition`,
  `GET /api/credentials/groups` (admin)
- `GET /api/runs`, `GET /api/runs/{id}`, `GET /api/runs/{id}/log`,
  `GET /api/runs/export?format=csv|json` (filtros `job`, `status`, `desde`, `hasta`)
- `GET /api/incidents` (incidentes abiertos + últimos resueltos)
- `GET /api/files`, `GET /api/jobs/{slug}/files`, `GET /api/cron`
- `GET /api/jobs/{slug}/files/entry?path=` (datos para confirmar), `GET /api/jobs/{slug}/files/download?path=`
- `POST /api/jobs/{slug}/files/mkdir|rename|move|delete` (admin)
- `POST /api/jobs/{slug}/files/upload?path=&name=&overwrite=` (admin; el archivo va como **cuerpo en crudo**)
- `GET/POST /api/users`, `PATCH /api/users/{username}`, `POST /api/users/{username}/password`
  (admin), `POST /api/auth/password` (propia)
- `GET /api/audit` (admin)
- `POST /api/auth/login`, `POST /api/auth/logout`, `GET /api/auth/me`

### Tamaño almacenado

`app/sizes.py` recorre el `dest_rel` de cada job con `os.scandir` (sin seguir symlinks, con tope de
entradas y timeout). El resultado se cachea `BACKUP_SIZE_TTL` y nunca bloquea la petición: si aún
no hay medida, la API responde `pending:true` y un hilo en segundo plano (`warm`) la calcula. El
sondeo guarda un snapshot en `size_snapshots` al cerrar cada corrida y, si falta, uno diario.

### Gestión de archivos (pestaña Archivos)

`app/files.py` resuelve cada ruta **por partes**: el directorio padre debe quedar dentro del ancla
(la raíz del NAS, o el `dest_rel` del job) y el último segmento se trata como hoja **sin seguir
enlaces**, de modo que borrar o renombrar un enlace nunca actúe sobre su destino. Navegar *a
través* de un enlace que sale del ancla (p. ej. `enlace-a-/etc/passwd`) se rechaza con `400`.

Reglas de las operaciones (todas auditadas e invalidando el tamaño medido):

| Regla | Detalle |
|-------|---------|
| Rol | Solo `admin`; un `viewer` ve el listado y puede descargar |
| Job en curso | `409`: si el `flock` del job está tomado, la gestión se bloquea hasta que termine |
| Interruptor | `BACKUP_MANAGE_FILES=0` deshabilita la escritura (el listado queda de solo lectura) |
| Base de datos | Requiere MySQL: la auditoría es obligatoria (`503` si no está) |
| Raíz protegida | No se puede borrar, renombrar ni mover la raíz del ancla ni la del NAS |
| Nombres | Sin `/ \ : * ? " < > \|` ni caracteres de control; no vacíos, sin espacios en los extremos, sin punto final, ni nombres reservados (`CON`, `NUL`, `COM1`…) |
| No pisar | Crear/renombrar/mover/subir falla con `409` si el destino existe (subir admite `overwrite=1`) |
| Borrado | Exige `confirm` igual al nombre; el portal lo pide escrito para carpetas, enlaces y archivos ≥ `BACKUP_FILES_CONFIRM_MB` |
| Subida | Tope `BACKUP_FILES_MAX_UPLOAD_MB`; se escribe en un temporal del mismo directorio y se renombra (atómico). Usa el cuerpo en crudo, así que **no** necesita `python-multipart` |
| Descarga | `FileResponse` en streaming; si el enlace apunta fuera del ancla, `400` |

Variables en `/etc/backupcsr/web.env`:

```ini
BACKUP_MANAGE_FILES=1              # 0 = pestaña Archivos de solo lectura
BACKUP_FILES_MAX_UPLOAD_MB=512     # tope por archivo subido
BACKUP_FILES_CONFIRM_MB=100        # desde este tamaño hay que escribir el nombre para borrar
```

Aviso que se muestra en la propia pestaña: lo que se elimina o cambia es la **copia del NAS**; la
próxima corrida del job volverá a bajar del origen lo que siga existiendo allí. Acciones
registradas en la Auditoría: `file-mkdir`, `file-rename`, `file-move`, `file-delete`,
`file-upload`, `file-download`.

## Degradación

- **Sin MySQL**: el panel y el historial por logs siguen funcionando (solo lectura); editar
  horarios y el detalle de corridas almacenadas responden `503`. Al volver MySQL, el sondeo
  rellena las corridas.
- **Sin NAS**: `/api/jobs/{slug}/files` responde `503`; el resto sigue.
- **Sin cron accesible**: el estado usa el horario del YAML cuando no puede leer el archivo.

## Seguridad

- El servicio escucha solo en `127.0.0.1:8089`; nginx termina TLS con el comodín `*.cortexdev.win`.
- Login con bcrypt y cookie de sesión firmada (`HttpOnly`, `Secure`, `SameSite=Lax`).
- No hay ejecución arbitraria: solo se lanzan slugs del catálogo (`jobs.yml`).
- NAS y logs se confinan con `realpath`; la escritura (crear, renombrar, mover, subir, borrar) es
  solo para `admin`, queda auditada y se bloquea mientras el job corre.
- `backupcsr_web` con password fuerte y `GRANT` limitado a `192.168.0.49`.

## Diagnóstico

```bash
systemctl status backupcsr-web
journalctl -u backupcsr-web -n 100
curl -fsS http://127.0.0.1:8089/api/health | jq
cd /opt/backupcsr/web && venv/bin/python -m app.cli status

# Pruebas (NAS temporal; no tocan /mnt/nas, ni MySQL, ni /run/lock, ni /etc):
cd <checkout> && /opt/backupcsr/web/venv/bin/python backupcsr/web/tests/test_files_manage.py
cd <checkout> && /opt/backupcsr/web/venv/bin/python backupcsr/web/tests/test_jobs_disabled.py
cd <checkout> && /opt/backupcsr/web/venv/bin/python backupcsr/web/tests/test_progress.py
cd <checkout> && /opt/backupcsr/web/venv/bin/python backupcsr/web/tests/test_batch_queue.py
cd <checkout> && /opt/backupcsr/web/venv/bin/python backupcsr/web/tests/test_scheduler.py
cd <checkout> && /opt/backupcsr/web/venv/bin/python backupcsr/web/tests/test_cronfile.py
```

### El Panel no lista tareas ("Sin tareas que mostrar")

La tabla **nunca se vacía por un fallo de API**: conserva la última información válida y muestra
un banner con el endpoint que falló (`/api/summary → HTTP 500`, por ejemplo). El mensaje del
centro de la tabla indica el motivo real:

| Mensaje | Significado | Acción |
| --- | --- | --- |
| `Cargando tareas…` | Primera carga en curso | Esperar; si no cambia, mirar el banner |
| `No se pudieron cargar las tareas (…)` | Falló `/api/jobs` o `/api/auth/me` | Ver el banner y pulsar **Reintentar**; `journalctl -u backupcsr-web` |
| `El catálogo no tiene tareas` | `jobs.yml` + `/etc/cron.d/backupcsr` vacíos | Revisar `jobs.yml` y `app.cli sync-jobs` |
| `Sin coincidencias para «X»` | Hay filtro activo | **Quitar filtro** o borrar «Buscar job…» |

Comprobaciones rápidas:

```bash
# ¿El backend desplegado es el actual? (la versión nueva incluye nas_stats)
curl -fsS http://127.0.0.1:8089/api/health | jq                       # sin nas_stats => hay que reinstalar
diff -rq /opt/backupcsr/web/app backupcsr/web/app                     # diferencias => install.sh pendiente
ls -l /run/lock/backupcsr-*.lock                                      # deben ser legibles por el usuario del servicio
```

Desde la consola del navegador: `__backupcsr.state` (jobs, errores de API, filtro activo y
selección) y `__backupcsr.reload()` para recargar sin refrescar la página.

Si el frontend está al día pero el backend no, varios botones fallan por diseño: Usuarios,
Auditoría, Tamaños y «Exportar CSV/JSON» (`/api/runs/export` navega a un 404). Reinstala con
`backupcsr/web/install.sh --no-apt`.

### El buscador se rellena solo con el usuario guardado

Firefox (y a veces Chrome) autocompletan los campos de texto con el usuario de la plataforma
porque la página tiene un modal con contraseñas. Están puestos los dos frenos:

- `autocomplete="off"` (más `name`, `type="search"` y `data-form-type="other"`) en `#jobs-search`,
  y `autocomplete="off"` en los formularios de filtros. Firefox ignora `off` en los campos que cree
  de login, así que esto solo no basta.
- **`readonly`** en el buscador de la tabla (atributo en el HTML): los navegadores no rellenan
  campos de solo lectura. Se libera en cuanto hay intención real (`pointerdown`, `touchstart`,
  `focusin`, `keydown`), así que escribir funciona igual.
- Guardia en `shell.js` (`setupFilters`): solo `keydown`, `paste` y `compositionstart` marcan el
  campo como escrito por la persona. El `input` del autofill —llega a veces **después** del clic—
  se descarta y se limpia el valor, de modo que nunca deja la tabla filtrada y vacía. También se
  limpia al enfocar si el valor no lo escribió nadie.

El modal de contraseñas conserva `autocomplete="current-password"` para que el gestor de
contraseñas funcione. Si tras el cambio de código el navegador sigue rellenándolos, recarga con
<kbd>Ctrl</kbd>+<kbd>Shift</kbd>+<kbd>R</kbd>: el atributo `readonly` viaja en `index.html`, que es
lo único que puede quedar en caché.

### Las tareas desaparecen al hacer clic en una fila

- Hacer clic en una fila la **selecciona** (estilo DataTables); Ctrl/Cmd+clic alterna y
  Mayús+clic toma un rango. No cambia de vista ni reconstruye la tabla. El <kbd>+</kbd> (o
  <kbd>Enter</kbd>) es lo que **expande/colapsa** el detalle.
- El contenedor de la tabla usa `overflow-y: clip` a propósito: si fuera un scrollport vertical,
  al enfocar una fila el navegador podía desplazarlo y dejar la tabla fuera de vista.
- El refresco automático de 30 s no repinta si los datos no cambiaron y se pausa con un modal o un
  menú desplegable abiertos, para no reconstruir la tabla mientras se opera. La selección y las
  filas expandidas se conservan entre refrescos (`state.checked` / `state.expanded`).

### Progreso y acciones masivas

- **Progreso en curso**: `api._progress` devuelve `percent` solo si viene de una medición real
  (el script puede publicarlo en la corrida); si no, `percent` es `null`, `real=false` y se envía
  `elapsed_s`. El panel, en el badge `EN CURSO`, añade `· <n>%` cuando el porcentaje es real; si
  no, añade el tiempo transcurrido (`EN CURSO · 12m`); con ninguno de los dos deja `EN CURSO`. Un
  ticker local actualiza el tiempo cada 2 s con `last_run.started_at`, sin depender del refresco de
  30 s. Se considera en curso también cuando el estado es `EN_CURSO` aunque el lockfile no sea
  legible. La barra de progreso solo se dibuja cuando hay un porcentaje real.
  No modifica los scripts ni añade un pre-pase de listado remoto.
- **Sin barra en reposo**: la columna Progreso se eliminó; el estado de una tarea detenida se lee
  en su píldora (`OK`/`TARDE`/`FALLO`/…).
- **Acciones masivas**: `POST /api/jobs/batch` (admin) con `action` ∈ `run|dry|retry|size` y una
  lista de `slugs`. Con `BACKUP_SCHEDULER=1` cada acción se **encola en el planificador**
  (`app/scheduler.py`), que ordena, limita la concurrencia y reintenta; sin él, `app/batch.py`
  conserva la FIFO serial anterior. En ambos casos se audita cada lanzamiento.
  `GET /api/jobs/batch` expone `running`/`pending`/`done` del lote y
  `GET /api/jobs/scheduler` el estado del planificador (cupos, cola y corridas).
- **Habilitar/Deshabilitar en bloque** y **Exportar CSV** no usan la cola: el primero hace `PATCH`
  por tarea en secuencia (requiere admin y `BACKUP_MANAGE_CRON=1`) y el segundo se genera en el
  cliente.
