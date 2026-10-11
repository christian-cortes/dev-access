/* Cliente HTTP del portal de copias (módulo ES). */

/* Login único central: copias ya no tiene login propio. Deja la página actual
   en la cookie que lee el backend (URL limpia) y va al login del índice. */
export const LOGIN_URL = 'https://index.cortexdev.win/login.html';

export function goToLogin() {
  try {
    document.cookie = 'cortex_sso_next=' + encodeURIComponent(location.href) +
      ';path=/;domain=.cortexdev.win;max-age=300;secure;samesite=lax';
  } catch (e) { /* sin cookie igual se entra, volviendo al índice */ }
  location.href = LOGIN_URL;
}

function parseBody(text) {
  if (!text) return null;
  try { return JSON.parse(text); } catch (e) { return text; }
}

async function request(path, options) {
  const opts = Object.assign(
    { credentials: 'same-origin', headers: { 'Content-Type': 'application/json' } },
    options || {}
  );
  const res = await fetch('/api' + path, opts);
  const text = await res.text();
  const data = parseBody(text);

  if (res.status === 401) {
    goToLogin();
    const err = new Error('no autenticado');
    err.status = 401;
    throw err;
  }
  if (!res.ok) {
    const detail = data && data.detail ? data.detail : ('HTTP ' + res.status);
    const err = new Error(typeof detail === 'string' ? detail : JSON.stringify(detail));
    err.status = res.status;
    throw err;
  }
  return data;
}

export const api = {
  get: (path) => request(path),
  post: (path, body) => request(path, { method: 'POST', body: JSON.stringify(body || {}) }),
  patch: (path, body) => request(path, { method: 'PATCH', body: JSON.stringify(body || {}) }),
  /* Subida en crudo: el archivo va como cuerpo y el nombre/destino en la query
     (sin multipart, para no depender de python-multipart en el servidor). */
  upload: (path, file) => request(path, { method: 'POST', body: file, headers: {} }),
  request,
};
