// Sets data-theme before first paint so there's no flash of the wrong theme.
// Light is the default whatever the operating system prefers; dark applies
// only when someone chose it with the sidebar toggle. Keep the storage key and
// fallback in sync with src/theme/theme.ts.
//
// This is an external file rather than an inline <script> so the
// Content-Security-Policy can use a plain script-src 'self' (see
// nginx.conf.template).
(function () {
  try {
    var stored = localStorage.getItem("cb-onboarding-theme");
    document.documentElement.dataset.theme = stored === "dark" ? "dark" : "light";
  } catch (e) {
    document.documentElement.dataset.theme = "light";
  }
})();
