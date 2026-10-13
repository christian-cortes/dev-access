(function () {
  var theme = 'dark';
  try {
    var saved = localStorage.getItem('cortexdev-theme');
    if (saved === 'light' || saved === 'dark') theme = saved;
  } catch (e) {}
  var root = document.documentElement;
  root.setAttribute('data-theme', theme);
  root.setAttribute('data-bs-theme', theme);
})();
