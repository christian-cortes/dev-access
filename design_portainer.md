# Design System — Gestor Docker (clon visual de Portainer)

> **Objetivo:** especificación de diseño de la interfaz web de administración de
> proyectos y recursos Docker. Describe el *look & feel*, los patrones de
> navegación, los componentes y el contrato funcional **reales** del gestor,
> para poder **reimplementar la interfaz en otro entorno** sin arrastrar
> dependencias de la instalación de origen.
>
> No se copian logotipos ni identidad de marca de Portainer: se replica su
> **claridad operativa** (jerarquía, densidad, estados, acciones). La base es
> **Bootstrap 5.3** vendido localmente, tema **oscuro por defecto** y una capa de
> tokens propios (`--mgr-*`) en un único archivo de tema.
>
> **Cómo leer este documento.** Las secciones 1–3 fijan principios, stack y
> restricciones. La sección 4 inventaría lo que la plataforma **ya** implementa.
> Las secciones 5–19 definen el diseño (visual, shell, pantallas, componentes,
> interacción, accesibilidad, responsive, microcopy). Las secciones 20–22 y los
> anexos son la **guía de portabilidad**: contrato de rutas, configuración,
> checklist de implementación, mapa de archivos y criterios de aceptación.

## Cambios integrados en esta revisión

Esta versión reconcilia el diseño con el estado actual del gestor. Respecto de
la especificación anterior, incorpora:

- **Autenticación SSO central** (login único por cabecera `X-Auth-User`); ya no
  hay login propio. El menú de cuenta es compartido y hace logout contra el SSO.
- **Notificaciones**: campana de incidencias en la topbar + **push FCM** por
  dispositivo, con preferencias de eventos, severidad, horario de silencio y
  avisos por proyecto.
- **Consola web restringida** (`docker exec`) sobre contenedores gestionados.
- **Páginas separadas** de Imágenes, Volúmenes y Redes; **Disco** con
  `docker system df` y *prune* seguro.
- **Proyectos**: favicon por proyecto (subir/elegir/quitar), **papelera** con
  restauración y **renombrado** (nombre + dominio), **acciones masivas** y
  **modal unificado Ver/Editar**.
- **Tipos** (manifiestos) y **Auditoría** como secciones de sistema.
- **Clon visual de Portainer** con tema oscuro por defecto y **conmutador a
  claro** persistente; paleta y densidad tomadas del Portainer real.
- Se documenta explícitamente lo que **no** existe: editor de Compose, métricas
  de host (CPU/RAM/disco del host), consola PTY interactiva y multi-host en la UI
  (la abstracción de entorno está preparada, pero hoy se resuelve un único host).

---

## 1. Principios de diseño

1. **Operación primero.** Estados, errores y acciones frecuentes visibles sin
   navegar por varias pantallas.
2. **Entorno y proyecto como contexto.** Dejar claro qué host/entorno está
   seleccionado y qué recursos pertenecen a él.
3. **Densidad útil.** Tablas legibles y paneles compactos antes que tarjetas
   enormes o espacios vacíos.
4. **Acciones seguras.** Diferenciar acciones normales, destructivas y
   disruptivas; las irreversibles piden confirmación explícita.
5. **Consistencia.** Mismos estados, botones, tablas, filtros y detalles en
   toda la aplicación.
6. **Responsive real.** Escritorio como escenario principal, usable en tablet y
   móvil.
7. **Datos reales.** Se usa la información que ya entrega el backend. Sin datos
   de demostración, sin controles decorativos y sin sustituir la lógica
   existente.

## 2. Stack y restricciones

- **UI:** Bootstrap 5.3 **vendido** (sin CDN) en
  `public/assets/vendor/bootstrap/` (`bootstrap.min.css`,
  `bootstrap.bundle.min.js`).
- **Tema:** tokens propios en `public/assets/manager/theme.css` (única fuente).
- **JavaScript:** vanilla, sin *build* para la UI del gestor. Los archivos
  `public/assets/manager/*.js` se cargan directamente con `asset()`.
  (El repositorio tiene Vite/Tailwind para el andamiaje Laravel, pero la
  interfaz del gestor **no** los usa.)
- **Iconos:** SVG inline (trazo `currentColor`, `stroke-width` 1.7–2). No hay
  dependencia de una fuente de iconos.
- **Tipografía:** `Inter` si está disponible, con *fallback* a la pila del
  sistema (ver §5.3). Empaquetar `Inter` como webfont local es opcional y queda
  como mejora pendiente para un clon 1:1.
- **Restricciones duras:**
  - No modificar rutas, API, permisos, autenticación, consultas, comandos Docker
    ni reglas de negocio para resolver un problema visual.
  - No eliminar funcionalidades ni inventar acciones que el backend no soporte.
  - No añadir frameworks, dependencias ni bibliotecas nuevas sin justificación.
  - Reutilizar layouts, componentes y utilidades existentes antes de duplicar.
  - Verificar que CSS y JS de Bootstrap se carguen correctamente y que no haya
    estilos duplicados ni conflictos de especificidad.

## 3. Decisiones de diseño

- **Clon visual de Portainer** en paleta, densidad y jerarquía (no en marca).
- **Tema oscuro por defecto**, con **conmutador a claro** persistente.
- **Tablas compactas globales** (sin selector de densidad): fila ~32 px, fuente
  13 px, cabecera 11 px en mayúsculas.
- **Sin editor de Compose** en la UI: el ciclo de vida de stacks se opera con
  acciones (`up`/`rebuild`/`restart`/`down`/`pull`/validar), no editando YAML.
- **Sin métricas de host**: solo métricas de contenedor (`docker stats`). No se
  simulan CPU/RAM/disco del host.
- **HTML/CSS/JS/JSON en UTF-8** servidos por nginx (`charset utf-8`) para evitar
  *mojibake* en acentos y símbolos.

---

## 4. Estado actual de la plataforma

Inventario funcional que la interfaz debe reflejar. Sirve como contrato para una
reimplementación.

### 4.1 Arquitectura y despliegue

Control plane Laravel que vive **fuera** del stack que administra (su propio
`docker-compose.yml` evita que un render erróneo del compose central lo tumbe).
Tres contenedores propios:

- `manager_nginx` (nginx): sirve `public/` y publica en `127.0.0.1:$HTTP_PORT`.
- `manager_app` (php-fpm): renderiza la interfaz y ejecuta la lógica.
- `manager_queue` (queue worker): despliegues y operaciones de stack en segundo
  plano (comandos `docker`/`docker compose`), con auditoría.

Acceso al Docker del host mediante `DOCKER_HOST` (por defecto `ssh://…`) y el
repo de infraestructura montado en `/infra`. Un único entorno activo
(`config('manager.environment')`, p. ej. `dev87`), resuelto por id para no
reescribir el núcleo si se añade multi-host.

### 4.2 Páginas y navegación real

Sidebar agrupado exactamente en estas secciones y enlaces:

| Grupo | Enlace | Ruta | Contenido |
|-------|--------|------|-----------|
| General | Dashboard | `/dashboard` | Resumen operativo (§7) |
| General | Proyectos | `/projects` | Listado de proyectos (§9.1) |
| General | Papelera | `/trash` | Proyectos eliminados, restaurar/purgar (§9.6) |
| Docker | Contenedores | `/containers` | Listado + acciones + modales (§10) |
| Docker | Stacks | `/stacks` | Compose lifecycle + operaciones (§11) |
| Docker | Imágenes | `/docker/images` | Pull/borrar imágenes (§12) |
| Docker | Volúmenes | `/docker/volumes` | Listar/borrar volúmenes (§12) |
| Docker | Redes | `/docker/networks` | Listar/borrar redes (§12) |
| Docker | Disco | `/system` | `docker system df` + prune (§12) |
| Sistema | Tipos | `/types` | Manifiestos de tipos + sincronizar (§13) |
| Sistema | Auditoría | `/audits` | Registro paginado de acciones (§13) |

También existen páginas fuera del sidebar: detalle de proyecto
(`/projects/{id}`), alta de proyecto (`/projects/create`), edición
(`/projects/{id}/edit`), favicon (`/projects/{id}/favicon`) y consola
(`/terminal/{container}`). `/` redirige a `/dashboard`; `/docker` redirige a
`/docker/images`. No hay páginas de Eventos/Logs globales: los logs se abren en
un **modal por recurso**.

### 4.3 Capacidades por recurso

| Recurso | Operaciones soportadas | Límites / protecciones |
|---------|------------------------|------------------------|
| **Proyectos** | crear, editar, previsualizar (dry-run), aplicar/deploy, reconcile (+aplicar), rollback, renombrar (n+d), favicon (subir/elegir/quitar), eliminar→papelera, restaurar, purgar, acciones masivas (reconcile/apply) | Sin editor de Compose |
| **Contenedores** | start/stop/restart/kill/pause/resume, remove (individual y masivo), logs, inspect, stats, consola `exec` | Contenedores del gestor no controlables; infra compartida sin consola (detalle abajo) |
| **Stacks** | up, rebuild (`up -d --build`), restart, down, pull, validar (`compose config`), logs, historial de operaciones | El stack de infra y los protegidos no admiten `down`; los no gestionados solo logs |
| **Disco** | `docker system df`; prune seguro (imágenes colgantes + contenedores parados de stacks gestionados); prune de build cache (compartida) | Nunca limpia volúmenes en bloque |
| **Imágenes** | pull, remove (confirmación por referencia) | — |
| **Volúmenes** | listar (con tamaño), remove con frase de confirmación | Volúmenes protegidos nunca se borran |
| **Redes** | listar, remove con frase de confirmación | `bridge`/`host`/`none` no eliminables |
| **Tipos** | listar manifiestos, sincronizar con BD (`manager:sync-types`) | — |
| **Auditoría** | listar (paginado 50) | — |

**Tres niveles de protección de contenedores** (crítico para la UI):

- **Propios del gestor** (`MANAGER_SELF_CONTAINERS`: `manager_app`,
  `manager_queue`, `manager_nginx`): **nunca** se controlan (un stop dejaría el
  control plane inaccesible). En la tabla se marcan con el chip `gestor` y sin
  botón de consola.
- **Infraestructura compartida** (`MANAGER_PROTECTED_CONTAINERS`: `nginx_web`,
  `mysql_ci`, `laravel_web`, `codeigniter_web`): acción de ciclo de vida sí,
  pero **sin consola**.
- **Gestionados** (`MANAGER_MANAGED_CONTAINERS`): allowlist que habilita la
  **consola web** (`docker exec`). El resto del host admite ciclo de vida y logs,
  pero no `exec`.

### 4.4 Autenticación, identidad y menú compartido

- **SSO central**: el gestor no tiene login propio. El nginx de acceso valida la
  cookie firmada y pasa `X-Auth-User`; el middleware `AuthenticateSso` mapea ese
  usuario a un registro local (`created_by`, auditoría, despliegues) e inicia
  sesión. Solo se acepta la cabecera desde redes de confianza
  (`MANAGER_SSO_TRUSTED_PROXIES`).
- La ruta `login` (nombre conservado) redirige al login central.
- **Menú de cuenta** (topbar): nombre del usuario vía `/api/auth/me`; **Cambiar
  tema**; **Ajustes** (abre el modal de notificaciones); **Cerrar sesión**
  (`POST /api/auth/logout`, luego redirección al login central).
- Los endpoints `/api/auth/*` y `/api/push/*` los sirve el **servicio
  compartido** (SSO/notify), no este repositorio. Si no están disponibles, la UI
  debe degradar con elegancia (ver §21.2).

### 4.5 Notificaciones (campana + push)

- **Campana (en la app):** deriva incidencias del **snapshot de `/status`**:
  proyectos cuyo tono es `error`/`dead`/`stopped`/`degraded`, **excluyendo**
  los apagados a propósito (`disabled`) y borradores (`draft`). Severidad
  `critical` (error/dead/stopped) o `warning` (degraded). Se pueden descartar
  por navegador (`localStorage`) y "Limpiar todo". Si la página ya sondea
  `/status`, reutiliza el evento `manager:status` (no duplica sondeo).
- **Push (servicio `notify`):** `push-client.js` activa FCM por dispositivo en
  el área `manager`, con preferencias de eventos, severidad mínima y horario de
  silencio, más el on/off por proyecto (`/api/scopes`). Requiere sesión.
- El backend puede **publicar** avisos (p. ej. `drift.detected` al reconciliar)
  mediante `Notifier`, que no debe romper la operación si el servicio cae.

### 4.6 Estado en vivo (polling)

- Endpoint `GET /status` (JSON) con snapshot **cacheado** (`MANAGER_STATUS_TTL`,
  por defecto 10 s). La página sondea cada `MANAGER_STATUS_POLL` (5000 ms) y
  pausa cuando la pestaña está oculta.
- El snapshot incluye: `projects`, `containers`, `stacks`, `stats` y `ts`.
- La UI **no reconstruye** filas: hidrata los elementos ya renderizados
  marcados con `data-live-scope` / `data-live-id` / `data-live-field` y escribe
  `stats` en celdas `data-stats-id` / `data-stats-field`. El timestamp se pinta
  en `[data-live-updated]`.
- `docker stats` se cachea **aparte** con su propio TTL (`MANAGER_STATUS_STATS_TTL`,
  20 s) y se acota a contenedores gestionados.

---

## 5. Dirección visual

### 5.1 Fuente única del tema

Todo el tema vive en `public/assets/manager/theme.css`: tokens `--mgr-*` +
*overrides* de Bootstrap 5.3. Cambiar ese archivo re-viste toda la aplicación.
La paleta y la densidad provienen del Portainer real (bundle `main.*.css`).

### 5.2 Tokens (valores reales)

```css
:root, [data-bs-theme="light"] {           /* tema claro */
  --mgr-bg: #f2f4f7;      --mgr-bg-soft: #ffffff;
  --mgr-surface: #ffffff; --mgr-surface-2: #f2f4f7; --mgr-surface-3: #eaecf0;
  --mgr-border: #d0d5dd;  --mgr-border-soft: #eaecf0; --mgr-border-strong: #98a2b3;
  --mgr-input-border: #d0d5dd;
  --mgr-text: #1d2939;    --mgr-muted: #475467; --mgr-faint: #667085;
  --mgr-primary: #0086c9; --mgr-primary-strong: #026aa2; --mgr-accent: #0ba5ec;
  --mgr-brand-soft: rgba(0,134,201,.10); --mgr-brand-wash: rgba(0,134,201,.18);
  --mgr-success: #12b76a; --mgr-warning: #f79009; --mgr-danger: #f04438;
  --mgr-radius: 8px; --mgr-radius-sm: 6px; --mgr-radius-xs: 4px; --mgr-radius-lg: 10px;
  --mgr-sidebar-w: 240px; --mgr-topbar-h: 56px;
  --mgr-row-y: 6px; --mgr-row-x: 12px; --mgr-table-font: 13px;
  color-scheme: light;
}

[data-bs-theme="dark"] {                    /* tema oscuro (por defecto) */
  --mgr-bg: #202124;      --mgr-bg-soft: #26272b;
  --mgr-surface: #26272b; --mgr-surface-2: #2e2f33; --mgr-surface-3: #34363b;
  --mgr-border: #3b3d44;  --mgr-border-soft: #31333a; --mgr-border-strong: #4d5761;
  --mgr-input-border: #3b3d44;
  --mgr-text: #eff1f4;    --mgr-muted: #98a2b3; --mgr-faint: #667085;
  --mgr-primary: #0086c9; --mgr-primary-strong: #0ba5ec; --mgr-accent: #36bffa;
  --mgr-brand-soft: rgba(11,165,236,.14); --mgr-brand-wash: rgba(11,165,236,.22);
  --mgr-success: #32d583; --mgr-warning: #fdb022; --mgr-danger: #f97066;
  color-scheme: dark;
}
```

| Uso | Portainer | token |
|-----|-----------|-------|
| Fondo app | `#202124` | `--mgr-bg` |
| Superficie / paneles | `#26272b` | `--mgr-surface` |
| Superficie-2 (cabeceras de tabla, footer) | `#2e2f33` | `--mgr-surface-2` |
| Superficie-3 (chips, hovers) | `#34363b` | `--mgr-surface-3` |
| Bordes | `#3b3d44` / `#4d5761` | `--mgr-border` / `--mgr-border-strong` |
| Texto / secundario / terciario | `#eff1f4` / `#98a2b3` / `#667085` | `--mgr-text` / `--mgr-muted` / `--mgr-faint` |
| Primario | `#0086c9` / `#0ba5ec` | `--mgr-primary` / `--mgr-primary-strong` |
| Acento (sky, enlaces dark) | `#36bffa` | `--mgr-accent` |
| Éxito / aviso / error | `#32d583` / `#fdb022` / `#f97066` | `--mgr-success/warning/danger` |

Los valores admiten ajustes, pero **respetando el contraste real** y sin
gradientes decorativos. No usar negro absoluto en todas las superficies.

### 5.3 Densidad, formas, tipografía y movimiento

- **Radio:** 4 px (xs) / 6 px (sm) / 8 px (base) / 10 px (lg); píldora solo para
  contadores y *badges* redondos.
- **Tipografía base:** `13.5px`; pila
  `"Inter", ui-sans-serif, system-ui, …, Arial, sans-serif`; datos técnicos
  (IDs, imágenes, puertos, rutas, nombres de contenedor, YAML, logs) en
  `.mono` (monoespaciada, 12 px).
- **Filas de tabla:** `--mgr-row-y: 6px`, `--mgr-row-x: 12px`,
  `--mgr-table-font: 13px`; `white-space: nowrap`; cabecera 11 px en mayúsculas
  sobre `surface-2`.
- **Controles:** botones ~30–32 px, inputs/selects ≥36 px, icon-buttons 34 px.
- **Espaciado:** múltiplos de 4/8 px.
- **Movimiento:** transiciones 100–180 ms; se respeta
  `prefers-reduced-motion`. Sin animaciones permanentes salvo el *pulse* del
  punto de estado "en ejecución" y el *spinner* de carga.
- **Sombra:** elevaciones sutiles (`--mgr-shadow`) solo en overlays (modales,
  dropdowns, sidebar en móvil). Sin brillos intensos ni glassmorphism.
- **Tema:** el tema guardado se aplica **antes de pintar** (script inline que lee
  `localStorage['mgr-theme']`) para evitar parpadeo; por defecto `dark`.

---

## 6. Shell de la aplicación

### 6.1 App shell

`body > .app-shell` es `display:flex`. `.app-sidebar` (240 px) + `.app-main`
(`flex:1`, `min-width:0`) con `.app-topbar` *sticky* y `.app-content`
(padding 18 px, `max-width:1600px`).

### 6.2 Sidebar

- **Cabecera:** marca (favicon + "Docker Manager").
- **Navegación:** grupos `General`, `Docker`, `Sistema` (ver §4.2). Cada
  `nav-link` combina icono SVG + etiqueta.
- **Sección activa:** fondo `--mgr-brand-soft`, color de enlace y barra/acento
  lateral de 3 px (`::before`), según `routeIs()`.
- **Grupo "Entorno" al pie:** punto de estado + `config('manager.environment')`.
- **Reglas:** no mostrar enlaces a pantallas inexistentes; contadores de grupo
  solo si aportan; el enlace activo se resuelve por patrón de ruta
  (`containers.*`, `projects.*`, etc.).
- **Móvil (<992 px):** el sidebar se convierte en panel deslizante
  (`transform`, clase `.show`) con botón *hamburguesa* en la topbar; se cierra al
  navegar. No usa el `offcanvas` de Bootstrap, pero se comporta como tal.

### 6.3 Topbar

Contiene, en este orden:

1. Botón *hamburguesa* (solo móvil).
2. **Título de página** (una sola línea).
3. `actualizado <hora>` (dato vivo, oculto en móvil).
4. **Campana** de notificaciones con contador (badge ámbar o rojo).
5. **Cuenta** (usuario, cambiar tema, ajustes, cerrar sesión).

La topbar **no** repite el título en el contenido ni acumula botones de acciones
de tabla. No hay breadcrumbs (el título de página hace de contexto; el sidebar
marca la sección).

### 6.4 Área de contenido

- Padding 18 px (14 px en móvil); títulos de sección dentro de `.hero`/`.page-head`
  (cabecera con título, descripción breve y acciones principales alineadas).
- En páginas de administración, priorizar el ancho disponible para tablas y logs.
- Errores de validación de formularios se muestran como `alert-danger` arriba del
  contenido (además de junto al campo cuando aplique).

### 6.5 Responsive

| Breakpoint | Comportamiento |
|------------|----------------|
| **≥1200 px** | Sidebar fijo; dashboard en cuadrícula (4 columnas); tablas completas |
| **992–1199 px** | Sidebar fijo contraído por ancho de contenido; tarjetas 2 columnas |
| **<992 px** | Sidebar deslizante; tarjetas apiladas; acciones secundarias en menú; tablas con scroll horizontal; logs/código con scroll interno |

---

## 7. Dashboard / Overview

Responde rápido: **¿Docker está conectado? ¿Qué funciona? ¿Qué falla?**

- **Encabezado:** título, descripción con la red (`config('manager.docker.network')`)
  y acciones `Contenedores` / `Gestionar proyectos`.
- **Tarjetas de resumen** (4, `col-6 col-xl-3`, con icono y tono):
  - Proyectos (total, desde BD).
  - Activos (`status = active`).
  - Con error (`status = error`, tono rojo).
  - Despliegues (total de `Deployment`).
- **Tabla de proyectos** reusable (versión `compact`, sin checkbox ni columnas
  secundarias), con enlace "Ver todos".
- **Últimos despliegues** (10): `#`, proyecto, acción (+`dry-run`), estado,
  fecha relativa.
- **Auditoría reciente** (8): acción, proyecto, resultado, fecha relativa.

No se muestran gráficos vacíos ni porcentajes ficticios. Si el backend no
entrega una métrica, no se simula.

---

## 8. Listados de recursos (patrón común)

Proyectos, contenedores, imágenes, volúmenes, redes y stacks comparten el mismo
esqueleto.

### 8.1 Cabecera / toolbar

- Título + contadores (`chip`), botón primario de la acción real.
- Búsqueda por texto (`data-table-search`), filtros `select`
  (`data-table-filter`), control de "items por página" y paginación.
- Actualización manual solo si existe (hoy el refresco es por polling + acciones).

### 8.2 Motor de tabla

Implementado en `app.js` sobre `table[data-table]`:

- **Búsqueda:** sobre `data-search` de cada fila (`tbody tr[data-row]`).
- **Filtros:** cada `select[data-table-filter="<clave>"]` compara con
  `data-<clave>` de la fila; valor `all` = sin filtro.
- **Orden:** cabeceras `th[data-sort="<clave>"]` con flechas SVG/CSS
  (`.sort-asc` / `.sort-desc`).
- **Paginación:** selector `data-table-page-size` + paginador
  `data-table-pagination`; opción `all`.
- **Filtro vacío:** fila `tr[data-empty]` con "Sin resultados…".
- **Estado vacío real:** fila `tr[data-empty]` separada (sin `.d-none`) con el
  mensaje de "no se pudo leer / sin datos".
- **Selección masiva:** checkbox `.js-select` + `.js-select-all`, contador
  `.js-selected-count` y `bulkForm` con name (p. ej. `ids[]`).

Reglas: cabecera distinguible del cuerpo; cifras y fechas alineadas; nombres
largos truncados con `title`/tooltip (o celda de stack ampliable); en móvil,
scroll horizontal controlado; el color nunca es el único indicador de estado.

### 8.3 Acciones masivas

Sobre la tabla o en una barra `.bulk-bar`, cuando hay selección:

- **Contenedores:** Iniciar, Detener, Reiniciar, Kill, Pausar, Reanudar,
  Eliminar (botones deshabilitados sin selección; destructivos en rojo y con
  confirmación).
- **Proyectos:** Reconcile / Aplicar (select + botón).

Siempre con confirmación previa (`data-confirm`) y `loading` en el envío.

### 8.4 Estados de la tabla

- **Carga:** spinner/en el botón que ejecuta; el snapshot llega por polling.
- **Vacío:** mensaje contextual ("¿Docker accesible?").
- **Error de conexión:** mensaje textual; reintento donde exista.
- **Sin coincidencias:** fila de "Sin resultados para el filtro actual".

---

## 9. Proyectos

### 9.1 Listado (`/projects`)

Toolbar con búsqueda (nombre/dominio/tipo/estado/contenedor/BD), filtros por
**tipo**, **estado** (`draft|active|pending|error|disabled`) y **perfil**
(`integrated|standalone`), y botón **Nuevo proyecto**.

Columnas: checkbox, Proyecto (favicon + nombre + dominio), Tipo, Perfil, Estado
(live), Salud (live), Contenedor, Workers (`q`/`s`), Último deploy, Acciones
(dropdown: Ver detalle, Editar, Dry-run, Aplicar, Reconcile, Favicon, Quitar
favicon, Eliminar).

### 9.2 Modal unificado Ver/Editar

Un único `#projectModal` con dos modos:

- **Ver:** badges de estado/salud, chips de tipo/perfil y pares clave-valor
  (dominio, contenedor, PHP, puerto, rutas, BD, workers).
- **Editar:** mismo formulario que la edición completa (dominio, perfil, PHP,
  puerto, rutas, BD, contraseña — vacía conserva —, workers).
- Enlaces: "Página completa" y conmutador de modo. Se puede abrir directamente
  en modo edición desde el listado/detalle (`data-project-open="edit"`).

Los datos llegan serializados en `data-project` (JSON). Al hacer clic en el
nombre se abre en modo ver; los botones de acción no disparan la navegación de
fila.

### 9.3 Detalle (`/projects/{id}`)

Cabecera: nombre, badges de estado/salud, chips tipo/perfil, enlace al dominio y
acciones (Volver, Editar, Dry-run, Reconcile, Favicon, **Aplicar**, Eliminar).

Bloques: **Renombrar** (nombre + dominio, con "Previsualizar" dry-run),
**Resultado** (salida de la última operación), **Drift detectado** (con botón
"Reconcile + aplicar") y **tabs**:

- **Resumen:** clave-valor del proyecto.
- **Plan (dry-run):** pre/`mgr-pre` con el plan renderizado.
- **Artefactos:** kind / path / checksum / render.
- **Despliegues:** `#`, acción (+dry-run), estado, fecha y **Rollback** (si hay
  snapshot y no fue dry-run).

### 9.4 Favicon (`/projects/{id}/favicon`)

- Vista previa del favicon actual + origen (propio, o por defecto del portal/tipo).
- **Subir** imagen (png/ico/svg/jpg/webp/gif, máx. 2 MB).
- **Elegir** una imagen de la carpeta del proyecto (raíz y `public/`).
- **Quitar** para volver al icono por defecto.
- Se guarda en la config del proyecto (`favicon`) y en
  `public/assets/projects/<name>.<ext>` (volumen compartido, no versionado).

### 9.5 Renombrar

Actualiza nombre y dominio con previsualización: limpia el bloque nginx y las
listas de workers antiguos y re-aplica el plan. **No** mueve la carpeta en disco.

### 9.6 Papelera (`/trash`)

Proyectos eliminados: nombre (favicon), dominio, tipo, fecha de borrado,
integridad de archivos. Acciones: **Restaurar** y **Eliminar definitivamente**
(confirmación por frase con el nombre). Al eliminar, los archivos se **mueven** a
`storage/manager/recovery`; no se borran.

---

## 10. Contenedores

### 10.1 Listado (`/containers`)

Toolbar: búsqueda, filtro por **estado** (`running|exited|created|paused|restarting`)
y por origen (`self`: externos/del gestor). Barra de **acciones masivas**.

Columnas: checkbox, Nombre (botón `.link-name` abre **Inspect**; chip `gestor`
si es del propio gestor), Estado (badge live, + salud si aplica), Acciones
rápidas, Stack (chips de proyecto ampliables), Imagen, IP, Puertos publicados
(los `tcp` enlazan a `http://<host>:<puerto>`).

**Acciones rápidas (iconos con `aria-label` + tooltip):** Logs, Inspect, Stats y
Consola (si no es del gestor). El ciclo de vida se opera por acciones masivas o
endpoints.

### 10.2 Modales de recurso

- **Logs** (`#logsModal`): `pre.mgr-pre.logs`, "Refrescar" y "Copiar".
- **Inspect / Stats** (`#outputModal`): salida JSON/texto; `stats` se formatea.
- Ambos son genéricos y se reutilizan por URL (`data-logs-url`,
  `data-output-url` + `data-output-field`).

### 10.3 Consola web (`/terminal/{container}`)

- Disponible solo para contenedores **gestionados** y **no protegidos**.
- UI tipo terminal: salida en `pre#term-output`, prompt `$`, input monoespaciado,
  historial con flechas ↑/↓.
- No es un PTY completo (sin `vim`/`top` interactivos). Cada comando se
  **audita** y se **redacta** en la auditoría.

---

## 11. Stacks

- Cabecera: descripción (compose lifecycle) + contador; entorno activo.
- **Tabla de stacks:** Stack (nombre + etiqueta), Origen (`infra`/proyecto/
  `external`; chip `protegido`), Estado (live), `running / total`, Acciones:
  `Logs`, `Validar` y dropdown `Control` → Rebuild (confirmación), Subir
  (`up -d`), Reiniciar (confirmación), Pull imágenes y **Detener (down)** en
  rojo (solo si no está protegido).
- **Operaciones recientes** (15): `#`, stack, acción, estado, inicio, fin y
  "Ver" (salida de la operación).
- Las acciones se **encolan** (`manager_queue`) y devuelven un aviso con el `#`
  de operación; el estado se sigue desde esa tabla. Los stacks no gestionados
  solo admiten logs.

---

## 12. Disco, imágenes, volúmenes y redes

- **Disco (`/system`):** `docker system df` en tabla (Tipo, Total, Activos,
  Tamaño, Reclamable) + **Prune seguro** (confirmación) y **Limpiar build cache**
  (confirmación, advierte del impacto compartido). Nota: los volúmenes no se
  limpian en bloque; enlace a Volúmenes.
- **Imágenes (`/docker/images`):** formulario de **Pull** + tabla (Repositorio,
  Tag, ID, Tamaño, Creada) con "Eliminar" (oculto para `<none>`).
- **Volúmenes (`/docker/volumes`):** tabla (Nombre, Driver, Scope, Tamaño) con
  "Eliminar" protegido por **frase de confirmación** (nombre) y bloqueo de
  volúmenes protegidos.
- **Redes (`/docker/networks`):** tabla (Nombre, Driver, Scope) con "Eliminar"
  (frase = nombre); `bridge`/`host`/`none` muestran chip `sistema` y no se borran.

---

## 13. Tipos y Auditoría

- **Tipos (`/types`):** tabla de manifiestos (id, label, perfiles, estrategia de
  scaffold, sincronizado) + botón "Sincronizar con la BD".
- **Auditoría (`/audits`):** tabla paginada (50) con `#`, acción, proyecto,
  usuario, resultado (badge), fecha y payload resumido.

---

## 14. Estados, badges y vocabulario

Vocabulario visual constante. Los *badges* combinan **texto + color** (nunca solo
color), con punto opcional y, para estados activos, *pulse*.

| Estado | Tono (`tone`) | Clase |
|--------|---------------|-------|
| Running / Healthy / Activo / Success | verde | `st-running` / `st-active` / `st-success` |
| Stopped / Exited / Disabled | gris | `st-stopped` / `st-exited` / `st-disabled` |
| Starting / Restarting / Paused / Pending / Rolled back / En progreso | ámbar | `st-pending` / `st-degraded` / `st-rolled_back` |
| Warning / Unhealthy / Atención | ámbar (salud) → rojo | `st-degraded` / `st-error` |
| Error / Failed / Dead | rojo | `st-error` / `st-failed` / `st-dead` |
| Draft / Created / Desconocido | gris | `st-draft` / `st-created` / `st-unknown` |

Mapeos reales que la UI debe respetar:

- Estado de contenedor → tono: `running`→running, `exited`→**stopped** (no
  error), `paused`/`restarting`/`removing`→degraded, `dead`→dead,
  `created`→created.
- Salud: `healthy`→running, `unhealthy`→error, `starting`→pending, sin health
  → `up`/`down` según estado.
- Proyecto: si corre y el **healthcheck HTTP** devuelve un código no permitido
  (no `200/301/302`) → tono `degraded` (no running).
- `exited` **no** significa error: depende del contexto y del código de salida.
- **Conectividad con Docker ≠ estado de contenedores:** el primer fallo se
  refleja en estados `unknown`/vacíos, no en badges rojos indistintos.

Clases de punto: `dot-running`, `dot-stopped`, `dot-pending`, `dot-error`,
`dot-unknown`, etc. El chip neutro (`.chip`) es para metadatos (tipo, perfil,
origen), no para estados.

---

## 15. Formularios, modales, confirmaciones y feedback

- **Formularios:** etiquetas visibles (`.form-label` en mayúsculas pequeñas),
  ayudas breves y errores junto al campo; agrupar por contexto con `row g-3`;
  no borrar lo ingresado ante error (`withInput`). La contraseña de BD vacía
  **conserva** la existente.
- **Confirmaciones:** modal genérico `#confirmModal` con título, cuerpo y
  **frase de confirmación** opcional (para acciones destructivas). Se dispara
  con `data-confirm`, `data-confirm-title` y `data-confirm-phrase` en formularios,
  enlaces, `button[data-confirm]` y acciones masivas.
  - Acciones destructivas: eliminar proyecto (frase = nombre), eliminar
    volumen/red (frase = nombre), purgar papelera (frase = nombre), kill/eliminar
    contenedores (título claro).
  - Acciones disruptivas (rebuild, down, prune, redeploy): confirmación sin frase.
- **Toasts:** `#toastStack` para resultados breves (`session('status')` /
  `session('error')`). Mensaje: acción, resultado y recurso afectado. Los errores
  importantes se mantienen visibles (alert o panel), no solo en un toast.
- **Busy/loading:** `markBusy` deshabilita el botón de envío y muestra
  `data-loading-label` con spinner. Evita dobles envíos.
- **No** comunicar éxito antes de la respuesta del backend; permitir ver el
  detalle (`pre.mgr-pre`) con "Copiar".

---

## 16. Accesibilidad

- Contraste suficiente para texto normal y estados (comprobar en ambos temas).
- Navegación completa por teclado; foco visible (`:focus-visible` con
  `--mgr-ring`).
- Botones *icon-only* con `aria-label` y tooltip de Bootstrap.
- Estados por **texto + color**, no solo color.
- Cada input asociado a su etiqueta (`for`/`id`).
- `prefers-reduced-motion` respetado; animaciones no esenciales.
- Objetivos táctiles adecuados en móvil (icon-buttons 34 px, filas de acción).
- Modales con título, cierre accesible y `aria-hidden` gestionado por Bootstrap.

---

## 17. Componentes y utilidades reutilizables

No hay framework de componentes: son **partials Blade + helpers JS**. Mapa
conceptual → real:

| Concepto | Implementación real |
|----------|---------------------|
| `AppLayout` | `resources/views/manager/layout.blade.php` |
| `SidebarNavigation` | array `$navGroups` dentro de `layout.blade.php` |
| `Topbar` | bloque `.app-topbar` en `layout.blade.php` |
| `PageHeader` | `.hero` / `.page-head` (CSS en `theme.css`) |
| `EnvironmentStatus` | `.status-dot` + `config('manager.environment')` en el sidebar |
| `MetricCard` | `.stat-card` (icono + valor + label) |
| `StatusBadge` | `.badge-status.st-*` / `.status-dot.dot-*` |
| `ResourceTable` | `table.mgr-table[data-table]` + motor en `app.js` |
| `TableToolbar` | `.toolbar` / `.card-header` + `data-table-search/filter` |
| `EmptyState` | `.empty-state` + `tr[data-empty]` |
| `LoadingState` | `.spin` + `data-loading-label` |
| `ErrorState` | `.alert-danger` + filas "sin datos" |
| `ConfirmActionModal` | `#confirmModal` + `data-confirm*` (en `app.js`) |
| `ToastNotifications` | `#toastStack` (en `layout.blade.php` + `app.js`) |
| `LogViewer` / `CodePanel` | `#logsModal` / `#outputModal` + `pre.mgr-pre` |
| `ProjectModal` | `#projectModal` + `_form_edit.blade.php` (Ver/Editar) |
| `PushSettings` | `#pushModal` + `push-client.js` |

JS: `app.js` (utilidades), `cortex-menu.js` (menú compartido), `push-client.js`
(FCM). No crear equivalentes duplicados.

---

## 18. Reglas de interacción

- Las filas pueden abrir el detalle; los botones de acción **no** deben activar
  la navegación de la fila.
- Mostrar `loading` en la acción que se ejecuta; evitar dobles envíos.
- Actualizar la UI solo cuando el backend confirma, o marcarlo como pendiente
  (p. ej. operaciones de stack encoladas).
- Mantener búsqueda/filtros al volver del detalle cuando sea viable.
- Mostrar errores de conexión con reintento donde exista.
- No refrescar toda la página si puede actualizarse el bloque (polling de
  `/status` + hidratación selectiva).
- Los controles deben hacer algo real; nada de botones decorativos.
- Tema y descartes de notificaciones persisten por navegador (`localStorage`).

---

## 19. Microcopy

Textos breves, técnicos y consistentes, en español, conservando términos Docker
reconocidos (`stack`, `container`, `image`, `volume`, `network`) cuando
coincidan con el vocabulario de la app. No mezclar idiomas sin criterio.

`En ejecución` · `Detenido` · `Reiniciando` · `No saludable` ·
`Docker conectado` · `Docker desconectado` · `Última actualización` ·
`Ver logs` · `Inspeccionar` · `Reintentar` · `Eliminar proyecto` ·
`Rebuild` · `Validar` · `Prune seguro` · `Página completa` · `Ver detalle`.

Mensajes de estado reales: los badges usan el `label` que entrega el backend
(`running`, `stopped`, `healthy`, `http 404`, `parcial`, `sin contenedor`, …).

---

## 20. Contrato de rutas y endpoints

Necesario para reimplementar la UI contra otro backend. Todas las rutas usan
autenticación SSO salvo la redirección de login.

| Método | Ruta | Nombre | Uso en la UI |
|--------|------|--------|--------------|
| GET | `/` | — | Redirige a `/dashboard` |
| GET | `/login` | `login` | Redirige al login central |
| GET | `/dashboard` | `dashboard` | Dashboard |
| GET | `/status` | `status` | Snapshot JSON (polling + campana) |
| GET | `/projects` | `projects.index` | Listado |
| GET | `/projects/create` | `projects.create` | Alta (página) |
| POST | `/projects` | `projects.store` | Alta (modal/página) |
| GET | `/projects/{p}` | `projects.show` | Detalle |
| GET | `/projects/{p}/edit` | `projects.edit` | Edición (página) |
| PUT | `/projects/{p}` | `projects.update` | Guardar (modal/página) |
| POST | `/projects/{p}/plan` | `projects.plan` | Previsualizar |
| POST | `/projects/{p}/deploy` | `projects.deploy` | Dry-run / aplicar |
| POST | `/projects/{p}/reconcile` | `projects.reconcile` | Reconcile (+apply) |
| POST | `/projects/bulk` | `projects.bulk` | Acciones masivas |
| GET/POST | `/projects/{p}/favicon*` | `projects.favicon*` | Favicon |
| POST | `/projects/{p}/rename` | `projects.rename` | Renombrar |
| POST | `/projects/{p}/delete` | `projects.destroy` | Eliminar → papelera |
| GET | `/trash` | `trash` | Papelera |
| POST | `/trash/{p}/restore` · `/purge` | `projects.restore`/`purge` | Restaurar / purgar |
| POST | `/deployments/{d}/rollback` | `deployments.rollback` | Rollback |
| GET | `/api/scopes` | `projects.scopes` | Lista de proyectos para push |
| GET | `/containers` | `containers.index` | Listado |
| POST | `/containers/bulk` | `containers.bulk` | Acciones masivas |
| POST | `/containers/{c}/action` | `containers.action` | start/stop/restart/kill/pause/resume |
| POST | `/containers/{c}/remove` | `containers.remove` | Eliminar |
| GET | `/containers/{c}/stats` · `/inspect` · `/logs` | … | Modales |
| GET | `/stacks` | `stacks.index` | Listado + operaciones |
| POST | `/stacks/{s}/action` | `stacks.action` | up/rebuild/restart/down/pull |
| POST | `/stacks/{s}/validate` | `stacks.validate` | Validar compose |
| GET | `/stacks/{s}/logs` · `/stack-operations/{o}` | … | Logs |
| GET | `/system` | `system.index` | `docker system df` |
| POST | `/system/prune` · `/prune-builder` | `system.prune*` | Limpieza |
| GET | `/docker/images` · `/volumes` · `/networks` | `docker.*.index` | Listados |
| POST | `/docker/{images\|volumes\|networks}/…` | `docker.*` | Pull/borrar |
| GET/POST | `/terminal/{c}` · `/terminal/{c}/exec` | `terminal.*` | Consola |
| GET | `/types` · POST `/types/sync` | `types.*` | Manifiestos |
| GET | `/audits` | `audits.index` | Auditoría |

**Endpoints externos** (servicio SSO/notify, no en este repo):
`GET /api/auth/me`, `POST /api/auth/logout`, `GET/PUT/POST /api/push/*`.

---

## 21. Configuración y portabilidad a otro entorno

La interfaz se replica en otro entorno sustituyendo, sobre todo, **valores de
configuración**, no código. Puntos de acoplamiento y cómo neutralizarlos:

### 21.1 Variables de entorno relevantes

| Variable | Por defecto | Para qué |
|----------|-------------|----------|
| `APP_URL` | `https://manager.cortexdev.win` | URL pública del gestor |
| `MANAGER_ENV_ID` / `MANAGER_ENV_LABEL` | `dev87` / `ubuntu-docker (.87)` | Entorno mostrado en sidebar |
| `MANAGER_DOCKER_HOST` | `ssh://…` | Docker del host (o socket local) |
| `MANAGER_INFRA_PATH` | `/infra` | Raíz del repo de infraestructura |
| `MANAGER_DOMAIN_SUFFIX` | `cortexdev.win` | Sufijo de dominios de proyecto |
| `MANAGER_SELF_CONTAINERS` | `manager_app,manager_queue,manager_nginx` | Contenedores no controlables |
| `MANAGER_MANAGED_CONTAINERS` | allowlist | Habilita consola `exec` |
| `MANAGER_PROTECTED_CONTAINERS` | infra | Sin consola |
| `MANAGER_PROTECTED_VOLUMES` | `manager_db,manager_storage` | Volúmenes no borrables |
| `MANAGER_INFRA_STACK_NAME` / `MANAGER_PROTECTED_STACKS` | `dev` / vacío | Stacks sin `down` |
| `MANAGER_STATUS_TTL` / `MANAGER_STATUS_POLL` / `MANAGER_STATUS_STATS_TTL` | `10` / `5000` / `20` | Polling y caché |
| `MANAGER_STATUS_STATS` / `MANAGER_LOGS_TAIL` | `true` / `200` | Stats y colas de logs |
| `MANAGER_TERMINAL_ENABLED` | `true` | Consola web |
| `MANAGER_LOGIN_URL` | login central | SSO |
| `MANAGER_SSO_TRUSTED_PROXIES` / `MANAGER_SSO_USERS` | redes privadas / vacío | Defensa SSO |
| `MANAGER_NOTIFY_ENABLED` / `MANAGER_NOTIFY_URL` / `NOTIFY_INGEST_TOKEN` | `true` / URL / — | Push e ingesta |
| `MANAGER_HEALTHCHECK_URL` / `_HOST` | `https://nginx_web/` | Healthcheck HTTP |

### 21.2 Dependencias externas y degradación

- **SSO central:** sin él, el middleware no autentica y `login` redirige al
  login configurado. Para un entorno sin SSO, sustituir por auth propia
  manteniendo el contrato visual (menú de cuenta con nombre/tema/ajustes/logout).
- **Servicio notify / FCM:** si falta (`/api/push/config` sin configurar o 401),
  `push-client.js` muestra un aviso y el resto de la UI funciona. El botón
  "Ajustes" abre el modal igualmente.
- **Docker host:** si no responde, los listados caen a estados vacíos con
  mensajes explícitos ("¿Docker accesible?"); no debe romper el layout.
- **`/api/auth/me` y `/api/auth/logout`:** si no existen, el nombre de cuenta queda
  como "Cuenta" y logout es best-effort (redirige igualmente al login).

### 21.3 Checklist de implementación

1. **Base:** Laravel + Blade + Bootstrap 5.3 vendido + `theme.css` (tokens) +
   `app.js`/`cortex-menu.js`/`push-client.js`. Sin build para la UI.
2. **Shell:** `layout.blade.php` con sidebar agrupado, topbar (título, campana,
   cuenta) y modales globales (`confirm`, `logs`, `output`, `project`, `push`) +
   `#toastStack`.
3. **Contrato visual:** definir `--mgr-*` (oscuro por defecto + claro) y
   `data-bs-theme` con script anti-parpadeo y `localStorage['mgr-theme']`.
4. **Endpoints mínimos:** `/status` (snapshot con `projects/containers/stacks/stats/ts`)
   y las rutas de §20 que apliquen a las capacidades reales.
5. **Motor de tabla** (`app.js`): búsqueda, filtros, orden, paginación y
   selección masiva; hidratación `data-live-*` y `data-stats-*`.
6. **Pantallas (en este orden):** Dashboard → Contenedores → Proyectos → Stacks →
   Disco → Imágenes/Volúmenes/Redes → Papelera/Favicon/Renombrar → Tipos/Auditoría
   → Consola.
7. **Seguridad de UI:** respetar self/protected/managed; confirmaciones
   contextuales (frase en destructivas); redacción de secretos en
   inspect/logs/auditoría.
8. **Validación:** responsive (3 breakpoints), accesibilidad (foco, labels,
   aria), ambos temas, y **consola del navegador sin errores** ni assets 404.
9. **Aislamiento:** no versionar imágenes de favicon de proyectos; secretos
   cifrados; el gestor no debe poder tumbarse a sí mismo.

---

## 22. Criterios de aceptación

- [ ] Identidad oscura coherente y legible; conmutador a claro funcional y
      persistente.
- [ ] Sidebar y topbar consistentes en todas las páginas; sección activa marcada.
- [ ] Dashboard con datos reales y navegación a los recursos.
- [ ] Tablas con búsqueda/filtros/orden/paginación según los datos existentes.
- [ ] Estados distinguibles por **texto y color**; sin color como único indicador.
- [ ] Acciones muestran progreso, éxito o error reales (y estado encolado en
      stacks).
- [ ] Acciones destructivas con confirmación contextual (frase cuando aplique).
- [ ] Protecciones respetadas: gestor no controlable, infra sin consola,
      volúmenes/stacks protegidos sin borrado.
- [ ] Logs, JSON, nombres largos y tablas no rompen el layout.
- [ ] Funciona en escritorio, tablet y móvil.
- [ ] No se eliminaron funcionalidades, ni se alteraron permisos o contratos de
      API.
- [ ] No hay datos ficticios ni controles sin funcionamiento.
- [ ] Sin errores nuevos en consola ni recursos CSS/JS fallidos.

---

## 23. Resultado esperado

Una interfaz de administración Docker inspirada en la claridad operativa de
Portainer: navegación por recursos, resumen del entorno, tablas compactas,
estados inequívocos y acceso rápido a acciones y logs. Debe **sentirse nativa**
de la aplicación existente y ser **reimplementable** en otro entorno cambiando
configuración, no diseño.

---

## Anexo A. Mapa de archivos

```
app/Http/Controllers/        Dashboard, Project, Container, Stack, Docker,
                             System, Terminal, Type, Audit, Status
app/Http/Middleware/         AuthenticateSso.php
app/Manager/                 ManagerService, Status/StatusService,
                             Docker/{DockerAdapter,ComposeAdapter,…},
                             Notify/Notifier, Project/AssetManager, …
app/Models/                  Project, Deployment, StackOperation, Artifact,
                             ProjectType, Audit, Setting, User
config/manager.php           Toda la configuración del gestor
routes/web.php               Contrato de rutas
resources/views/manager/     layout.blade.php + dashboard + projects/*
                             containers + stacks + docker + system +
                             terminal + types + audits
public/assets/manager/       theme.css, app.js, cortex-menu.js, push-client.js
public/assets/vendor/        bootstrap (css + bundle js)
public/firebase-messaging-sw.js
docker/                      Dockerfile, entrypoint.sh, nginx.conf, www.conf
docker-compose.yml           Stack del gestor (nginx + app + queue)
scripts/setup-services.sh    Puesta en marcha (SSH, sshfs, .env, vhost, DNS)
```

## Anexo B. Iconografía de tecnologías

`public/assets/tech/` incluye iconos por tecnología
(`laravel`, `codeigniter`, `php`, `nginx`, `mysql`, `docker`, `react`,
`angular`, `python`, `powerbi`, …). Se usan como **favicon por defecto** de un
proyecto cuando no hay un favicon propio ni el del portal
(`public/assets/favicons/`). El mapeo tipo→icono vive en
`Project::TYPE_ICONS`. Estas imágenes no se versionan.

## Anexo C. Mejoras pendientes (fuera de alcance)

- Empaquetar `Inter` como webfont local para un clon 1:1 (hoy usa Inter si está
  instalada, con *fallback* al sistema).
- PTY interactivo real para la consola (sidecar con websocket) y multi-host en
  la UI (la abstracción de entorno ya está preparada).
- Editor de Compose en la UI (hoy solo acciones de ciclo de vida).
