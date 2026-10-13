/* Tema anti-parpadeo para UPS. Aplica antes de pintar: tema oscuro por defecto,
 * persistido en localStorage['nut-theme'] (la misma clave que usa app.js).
 * Define data-theme y data-bs-theme para que actúen tanto nuestros tokens
 * --mgr-* como los componentes de Bootstrap 5.3. */
(function () {
  var theme = 'dark';
  try {
    var saved = localStorage.getItem('nut-theme');
    if (saved === 'light' || saved === 'dark') theme = saved;
  } catch (e) {}
  var root = document.documentElement;
  root.setAttribute('data-theme', theme);
  root.setAttribute('data-bs-theme', theme);
})();
