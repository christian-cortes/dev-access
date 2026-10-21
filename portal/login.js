(function () {
  'use strict';
  var form = document.getElementById('login-form');
  var errorBox = document.getElementById('login-error');
  var submit = document.getElementById('login-submit');

  /* El destino tras entrar lo decide el backend: nginx guarda la página de
     origen en la cookie cortex_sso_next y /api/auth/login responde con `next`
     ya validado. La URL del login queda limpia. */

  /* Si ya hay sesión válida, no mostrar el formulario. */
  fetch('/api/auth/me', { credentials: 'same-origin' })
    .then(function (res) { if (res.ok) location.replace('/'); })
    .catch(function () { });

  form.addEventListener('submit', async function (event) {
    event.preventDefault();
    errorBox.style.display = 'none';
    submit.disabled = true;
    try {
      var res = await fetch('/api/auth/login', {
        method: 'POST',
        credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          username: document.getElementById('username').value,
          password: document.getElementById('password').value
        })
      });
      var text = await res.text();
      var data = null;
      try { data = text ? JSON.parse(text) : null; } catch (e) { }
      if (!res.ok) {
        throw new Error((data && data.detail) || ('HTTP ' + res.status));
      }
      location.href = (data && data.next) || '/';
    } catch (err) {
      errorBox.textContent = 'No se pudo entrar: ' + err.message;
      errorBox.style.display = 'block';
      submit.disabled = false;
    }
  });
})();
