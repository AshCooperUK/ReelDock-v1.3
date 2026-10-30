// Apply the saved local appearance before first paint, with the same validation
// as the React provider. This external script preserves the app's CSP.
(function () {
  var scheme = 'dark';
  try {
    var saved = JSON.parse(localStorage.getItem('reeldock-appearance-v13') || 'null');
    var accents = ['blue','cyan','teal','green','lime','yellow','orange','red','pink','grape','violet','indigo'];
    if (saved && (saved.scheme === 'dark' || saved.scheme === 'light') && accents.indexOf(saved.accent) !== -1) scheme = saved.scheme;
  } catch (_) { /* Storage can be disabled; use the current default. */ }
  document.documentElement.setAttribute('data-mantine-color-scheme', scheme);
  document.documentElement.style.colorScheme = scheme;
  document.documentElement.style.backgroundColor = scheme === 'light' ? '#ffffff' : '#242424';
})();
