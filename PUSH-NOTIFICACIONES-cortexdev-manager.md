# Notificaciones push en `cortexdev-manager`

Guía para integrar en **cortexdev-manager** (`manager.cortexdev.win`, 192.168.0.49:8090)
el mismo sistema de notificaciones push (FCM web) y el menú de usuario que ya usan
`index`, `copias` y `ups`.

El servicio central `notify` vive en este repo (`cortexdev-access`) y escucha en
`127.0.0.1:8490`. **No hay que crear otro servicio**: solo registrar el área `manager`,
proxificar `/api/push/` y `/api/auth/` desde su vhost, y publicar eventos desde su backend.

---

## 1. Modelo (cómo funciona)

- **Área**: cada sitio/app es un área (`index`, `copias`, `ups`, y ahora `manager`).
- **Suscripciones**: el navegador obtiene un token FCM y lo manda a
  `POST /api/push/subscribe` con `{area, token, platform}` (requiere sesión SSO).
- **Preferencias** por usuario y área: on/off del área, eventos, severidad mínima,
  horario silencioso; y on/off por *scope* (proyecto/tarea/contenedor) opcional.
- **Ingesta de eventos**: el backend publica en `POST /api/events` (loopback + token)
  y `notify` reparte por FCM (y correo si `NOTIFY_MAIL_TO` está configurado).
- **SSO**: cookie firmada `backupcsr_session` con `Domain=.cortexdev.win`; la valida
  `notify` en `GET /api/auth/verify` (via `auth_request`). Como manager está en
  `*.cortexdev.win`, comparte la sesión.

Piezas ya existentes en `cortexdev-access` (referencia):
- `notify/app/main.py` (endpoints `/api/auth/*`, `/api/push/*`, `/api/events`).
- `notify/app/catalog.py` (catálogo de áreas/eventos).
- `notify/app/channels.py` (FCM/correo, silencio, scopes).
- `portal/push-client.js` y `portal/firebase-messaging-sw.js` (cliente a copiar).
- `nginx/conf.d/snippets/index-body.conf` y `nginx/conf.d/ups.conf` (patrón de vhost).

---

## 2. Registrar el área `manager` en notify

Editar `notify/app/catalog.py` y añadir un área nueva con sus eventos. El `level` por
defecto lo puede subir el emisor, nunca bajar. Marca `scoped: True` solo si vas a
ofrecer avisos por proyecto/contenedor.

```python
AREAS = {
    # ... index, copias, ups ...
    "manager": {
        "title": "Manager (proyectos y despliegues)",
        "scoped": True,   # ponlo False si no quieres filtrado por proyecto/contenedor
        "events": {
            "deploy.failed":    {"title": "Despliegue fallido",     "level": "critical"},
            "deploy.ok":        {"title": "Despliegue completado",  "level": "info"},
            "container.down":   {"title": "Contenedor caído",       "level": "critical"},
            "container.up":     {"title": "Contenedor recuperado",  "level": "info"},
            "build.failed":     {"title": "Build fallido",          "level": "warning"},
        },
    },
}
```

Rebuild/reinicio del servicio notify:

```bash
docker compose build notify && docker compose up -d notify
```

El frontend lee las áreas/eventos automáticamente de `GET /api/push/config`; no hay que
tocar la UI para que aparezcan los nuevos eventos.

---

## 3. Vhost nginx del manager

En el proyecto manager, el `server` de `manager.cortexdev.win` debe:

1. Validar la sesión SSO con `auth_request` contra notify.
2. Proxificar `/api/push/` a notify (config, subscribe, unsubscribe, prefs, scopes, test).
3. Proxificar `/api/auth/me` y `/api/auth/logout` a notify (el login único emite/borra
   la cookie; si el manager ya tiene su propio login, al menos `logout` debe ir a notify).
4. Servir `firebase-messaging-sw.js` **público** (sin `auth_request` ni redirecciones).
5. Mantener su propia `/api/` (la app) protegida por `auth_request`.

Bloques de referencia (copiados del patrón de `ups.conf`/`index-body.conf`; ajusta el
`proxy_pass`/raíz a los del manager):

```nginx
# --- SSO: valida la cookie firmada contra el servicio notify ---
location = /_auth {
    internal;
    proxy_pass http://127.0.0.1:8490/api/auth/verify;
    proxy_pass_request_body off;
    proxy_set_header Content-Length "";
    proxy_set_header Cookie $http_cookie;
    proxy_set_header X-Original-URI $request_uri;
}
location @sso_login {
    # Solo las navegaciones de documento recuerdan el destino; los subrecursos
    # (favicon, JS, CSS...) devuelven 401 sin tocar la cookie (si no, el favicon
    # del login sobrescribe cortex_sso_next y el login acaba en /favicon.ico).
    if ($sso_is_document) {
        add_header Set-Cookie "cortex_sso_next=$scheme://$host$uri; Domain=.cortexdev.win; Path=/; Max-Age=300; Secure; SameSite=Lax";
        return 302 https://index.cortexdev.win/login.html;
    }
    return 401;
}

# --- login/logout/me del servicio notify ---
location /api/auth/ {
    proxy_pass http://127.0.0.1:8490;
    proxy_http_version 1.1;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
}

# --- Notificaciones (servicio notify) ---
location /api/push/ {
    proxy_pass http://127.0.0.1:8490;
    proxy_http_version 1.1;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
}

# --- Service worker FCM: PÚBLICO (un SW con login/redirect no se registra) ---
location = /firebase-messaging-sw.js {
    add_header Cache-Control "no-cache";
    try_files $uri =404;
}

# --- API del manager (protegida por SSO) ---
location /api/ {
    auth_request /_auth;
    error_page 401 = @sso_login;
    proxy_pass http://127.0.0.1:8090;
    proxy_http_version 1.1;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_read_timeout 60s;
}

location / {
    auth_request /_auth;
    error_page 401 = @sso_login;
    try_files $uri $uri/ =404;
}
```

Aplicar sin recrear nginx: `docker exec access_nginx nginx -t && docker exec access_nginx nginx -s reload`
(o el proceso de reload del proyecto manager).

> `$sso_is_document` lo definen los `map` de `nginx/conf.d/sso.conf` (incluido a
> nivel `http`, junto a los demás vhosts). Si el vhost del manager vive en otro
> nginx, hay que definir allí los mismos `map` (solo `Sec-Fetch-Dest: document`
> + `Accept: text/html` recuerdan el destino).

> Nota: los bloques `/api/auth/` están duplicados por vhost en este repo (index/copias/ups).
> Si se replica en manager, considera extraerlos a un snippet compartido
> (`snippets/sso-auth.conf`) para no tener que editar N sitios si cambia el puerto 8490.

---

## 4. Frontend del manager

1. Copiar a la estática del manager (mismos nombres y rutas que en `portal/`):
   - `firebase-messaging-sw.js`
   - `push-client.js`
   Ajustar el `?v=N` al incluirlos para invalidar caché tras cada deploy.

2. Incluir el contenedor de ajustes donde quieras (página de ajustes o modal):

```html
<div id="push-settings" data-area="manager"></div>
```

Si vas a ofrecer avisos por proyecto/contenedor, añade la URL ligera de scopes y su
etiqueta:

```html
<div id="push-settings" data-area="manager"
     data-scopes-url="/api/scopes" data-scopes-label="proyecto"></div>
```

3. Cargar el cliente (y el SDK bootstrap del manager, si aplica):

```html
<script src="/push-client.js?v=1" defer></script>
```

`push-client.js` ya se encarga de: pedir permiso, registrar el service worker, obtener
el token FCM, suscribir, y pintar el formulario de eventos/severidad/silencio y (si hay
`data-scopes-url`) la lista de scopes. Espera que `/api/push/config` responda con sesión.

4. **Menú de usuario** (mismo patrón que portal/copias/ups): un icono que abre un
   desplegable con el nombre (`GET /api/auth/me`), "Cambiar tema", "Ajustes" (abre el
   contenedor `#push-settings`) y "Cerrar sesión" (`POST /api/auth/logout` → redirige a
   `https://index.cortexdev.win/login.html`). En `portal/app.js` están `addUserMenu()`,
   `ensureNotifModal()` y `openNotifModal()` como referencia.

5. `firebase-messaging-sw.js` lee su config de `GET /api/push/fcm-config`, que es
   **público**: no lo pongas detrás de SSO ni cambies ese path.

---

## 5. Backend del manager: publicar eventos

Añade una función de publicación idéntica a `backend/app/notify.py`:

```python
import json
import os
import urllib.request

NOTIFY_URL = os.environ.get("NOTIFY_URL", "").rstrip("/")
INGEST_TOKEN = os.environ.get("NOTIFY_INGEST_TOKEN", "").strip()

def publish(area, event_key, title, body="", level=None, url="", dedupe_key="", scope=""):
    if not (NOTIFY_URL and INGEST_TOKEN):
        return
    payload = {"area": area, "event_key": event_key, "title": title,
               "body": body, "url": url, "dedupe_key": dedupe_key}
    if scope:
        payload["scope"] = scope
    if level:
        payload["level"] = level
    req = urllib.request.Request(
        NOTIFY_URL + "/api/events",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {INGEST_TOKEN}"},
        method="POST",
    )
    try:
        urllib.request.urlopen(req, timeout=5).read()
    except Exception:
        pass  # no romper el flujo del deploy por un fallo de notificación
```

Uso típico:

```python
publish("manager", "deploy.failed", "Proyecto X: despliegue fallido",
        body="El contenedor no arrancó (exit 1).", url="https://manager.cortexdev.win",
        dedupe_key="deploy-x", scope="proyecto-x")
```

Reglas del endpoint `/api/events`:
- Requiere `Authorization: Bearer <NOTIFY_INGEST_TOKEN>`; el token es compartido con
  index/copias/ups (mismo valor de `.env`). No exponerlo al frontend.
- `event_key` debe existir en el catálogo del área; `level` solo puede subir respecto al
  del catálogo.
- `dedupe_key` evita repetir el mismo evento en una ventana (`NOTIFY_DEDUPE_WINDOW`,
  por defecto 900 s). **Usa una clave distinta para caída y recuperación**
  (p. ej. `deploy-x` y `deploy-x-ok`).
- Publica desde un hilo/`asyncio.to_thread` si tu ciclo es async (la llamada es bloqueante).
- Evita emitir en cada sondeo: emite en **transiciones** (caída→recuperación), con
  histéresis si el chequeo puede fallar de forma transitoria (mira
  `backend/app/status.py` y `backend/app/main.py` de este repo como referencia).

### Si quieres avisos por proyecto (scopes)

Implementa en el manager un endpoint ligero `GET /api/scopes` (público o con SSO) que
devuelva:

```json
{ "scopes": [ { "slug": "proyecto-x", "name": "Proyecto X",
               "group": "produccion", "group_title": "Producción" } ] }
```

`push-client.js` lo consume desde `data-scopes-url` y guarda el on/off en
`PUT /api/push/scopes`. Solo on/off: el resto se hereda del área.

---

## 6. Variables de entorno (proyecto manager)

En el `docker-compose.yml`/servicio del manager (host network si usa `127.0.0.1:8490`):

```yaml
environment:
  NOTIFY_URL: "http://127.0.0.1:8490"
  NOTIFY_INGEST_TOKEN: ${NOTIFY_INGEST_TOKEN:-}
```

`NOTIFY_INGEST_TOKEN` y las `FIREBASE_*` viven en el `.env` de `cortexdev-access`
(el `firebase-service-account.json` solo lo usa `notify`; el manager no lo necesita).
Si el manager corre en otro host, `NOTIFY_URL` debe apuntar al host de notify y ese
puerto (8490) no debe quedar expuesto públicamente.

---

## 7. Verificación

1. `docker compose build notify && docker compose up -d notify` y
   `curl -s http://127.0.0.1:8490/health` → `fcm: true`.
2. `GET https://manager.cortexdev.win/api/push/config` con sesión → devuelve `areas`
   incluyendo `manager`.
3. En el navegador: activar notificaciones en los ajustes del manager (obtiene token y
   llama a `/api/push/subscribe`), pulsar "Enviar prueba" (`POST /api/push/test`).
4. Provocar un evento real (`publish(...)`) y comprobar la llegada del push.
5. `firebase-messaging-sw.js` debe cargar sin redirección/401 (si el SW falla, no hay
   notificaciones en segundo plano).

---

## 8. Notas

- **iOS/Safari**: no soporta web push FCM salvo PWA instalada en iOS 16.4+.
- **Permiso denegado**: no se puede re-pedir; el usuario debe restablecerlo desde el
  candado del sitio.
- **Correo**: se activa con `NOTIFY_MAIL_TO`/`*SMTP*` en notify (hoy sin configurar);
  los eventos de `level >= warning` se envían por correo además de FCM.
- **No dupliques lógica**: el reparto, dedupe, silencio y scopes ya los hace notify; el
  manager solo publica eventos y muestra la UI de ajustes.
