// Per-developer mini-app config.
//
// Copy this file to `local.config.js` (gitignored) and set apiBase to the
// backend URL you want the simulator AND phone uploads to point at:
//   - Tier 0: http://localhost:8000 (DevTools simulator only)
//   - Tier 1: https://*.trycloudflare.com (rotates per cloudflared restart)
//   - Tier 2: https://your-stable-domain (HK VPS)
//
// Whatever apiBase is here gets compiled into 体验版 / 正式版 uploads, so set
// this to the URL you want testers' phones to hit before clicking 上传.
module.exports = {
  apiBase: 'https://YOUR_BACKEND_DOMAIN'
}
