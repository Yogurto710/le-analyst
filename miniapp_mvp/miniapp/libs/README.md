# libs/

Vendored client libraries. Currently:

## lottie-miniprogram

Powers the robot mascot animation on the status page. Without it
installed, the page falls back to a CSS-animated 🤖 emoji — visually
plainer but functionally fine for closed-beta.

### Install (one-time, desktop only)

In a terminal at `miniapp_mvp/miniapp/`:

```powershell
cd "c:\Users\Administrator\Documents\AI Coding\le-analyst\miniapp_mvp\miniapp"
npm install
```

Then in WeChat DevTools: **工具 → 构建 npm**.

DevTools writes the compiled package to `miniprogram_npm/lottie-miniprogram/`,
which is what `pages/status/status.js` requires.

After 构建 npm, recompile and the canvas-rendered robot should appear.

### Current mascot asset

`miniapp/assets/lottie/robot-3d.js` — 1080×1080, 30fps, 2.7s loop,
all-vector precomp (no embedded bitmaps). The file is the raw Lottie
JSON wrapped as `module.exports = {...}` so DevTools doesn't scan it
as a mini-app page config (which crashed the WXML compiler when it was
a bare `.json`).

### Swapping the mascot

1. Get a new Lottie JSON.
2. Wrap it: write `module.exports = ` followed by the JSON object,
   save as `miniapp/assets/lottie/your-mascot.js`. From PowerShell:
   ```powershell
   "module.exports = " + (Get-Content your-mascot.json -Raw) | Set-Content your-mascot.js -Encoding utf8
   ```
3. Update the `require('../../assets/lottie/robot-3d.js')` line at the
   top of `pages/status/status.js` to point at the new filename.
4. If aspect ratio is not square, adjust `.mascot-canvas` width/height in
   `pages/status/status.wxss`.
