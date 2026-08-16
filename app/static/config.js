/* FINCORE — the single source of truth for where the API lives.
 *
 * The Render URL appears HERE and nowhere else. app.js reads `FINCORE.apiBase`
 * and never contains a hostname of its own.
 *
 * There is no build step and no server to interpolate environment variables,
 * so the base is resolved by asking one honest question: is this page being
 * served by the API itself, or by the static site?
 *
 *   served by the API (local dev, or visiting the API host directly)
 *       -> same origin, apiBase = ""     (no CORS involved at all)
 *   served by the static site
 *       -> apiBase = the deployed API origin
 *
 * Nothing secret belongs in this file, and nothing secret is in it: the API
 * origin is public by definition. There is no database URL, no credential, and
 * no key here or anywhere else in the browser bundle.
 */
window.FINCORE = (function () {
  'use strict';

  // The deployed execution API. Change this one line if the API moves.
  var API_ORIGIN = 'https://fincore-chaos-lab.onrender.com';

  var here = window.location.origin;
  var isLocal = /^https?:\/\/(localhost|127\.0\.0\.1|\[::1\])(:\d+)?$/.test(here);
  var servedByApi = here === API_ORIGIN;

  return {
    // "" means same-origin: fetch("/api/...") exactly as before.
    apiBase: (isLocal || servedByApi) ? '' : API_ORIGIN,
    apiOrigin: API_ORIGIN,
    // Header carrying the opaque demo session id. Used instead of a cookie so
    // the demo does not depend on third-party cookies surviving a cross-site
    // fetch; see app.js for what that id is and is not.
    sessionHeader: 'X-Fincore-Session',
    sessionStorageKey: 'fincore.session',
  };
})();
