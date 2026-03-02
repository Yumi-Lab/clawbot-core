# ClawbotOS Module Specification

A ClawbotOS module is a self-contained GitHub repository that adds a new capability to ClawbotOS. Modules are installed and managed via ClawbotCore.

---

## Repository Structure

```
clawbot-my-module/
├── manifest.json          ← required — describes your module
├── install.sh             ← required — runs once on install
├── clawbot-my-module.service ← required — systemd service
├── main.py                ← your module code
└── README.md
```

---

## manifest.json — Required Fields

| Field | Type | Description |
|-------|------|-------------|
| `id` | string | Unique identifier (lowercase, hyphens) e.g. `"camera"` |
| `name` | string | Display name e.g. `"Camera Vision"` |
| `version` | string | Semver e.g. `"1.0.0"` |
| `author` | string | GitHub username or org |
| `description` | string | One-sentence description |
| `category` | string | One of: `interface`, `sensors`, `integration`, `tools` |
| `ram_mb` | number | Estimated RAM usage in MB |
| `service` | string | Systemd service filename e.g. `"clawbot-camera.service"` |
| `repo` | string | Full GitHub URL |

### Optional Fields

| Field | Type | Description |
|-------|------|-------------|
| `api_prefix` | string | HTTP prefix your module serves e.g. `"/v1/camera/"` |
| `tools` | array | PicoClaw tool names your module exposes |
| `requires` | array | Hardware requirements e.g. `["camera_hardware"]` |
| `web_app` | object | Mini web app served in the dashboard (see below) |

---

## Required API Endpoint

Every module **must** expose a health endpoint:

```
GET /v1/{id}/status
→ { "ok": true, "version": "1.0.0" }
```

ClawbotCore calls this to verify the module is running.

---

## Port Allocation

| Range | Usage |
|-------|-------|
| 8080 | PicoClaw (reserved) |
| 8089 | clawbot-status-api (reserved) |
| 8090 | ClawbotCore (reserved) |
| 8091–8199 | Community modules |

Pick an available port in `8091–8199` and document it in your README.

---

## Mini Web App (store panel)

A module can provide a mini web app displayed as a panel in the ClawbotOS dashboard and touchscreen interface. Declare it in `manifest.json`:

```json
"web_app": {
  "entry": "app.html",
  "icon": "🎥",
  "label": "Camera"
}
```

`app.html` is a self-contained HTML file served by your module at:
```
GET /v1/{id}/app
```

It is embedded in an `<iframe>` inside the ClawbotOS dashboard. It communicates with your module via the `/v1/{id}/` API (accessible at `window.location.origin`).

**Guidelines for mini apps:**
- Fully responsive — must work at 320×240 minimum (mobile) and 840×480 (SmartPad)
- No external CDN dependencies — bundle everything or use vanilla JS
- Dark background preferred (`#111` or similar) to match ClawbotOS theme
- Under 50KB recommended

---

## install.sh

Runs as root during installation. Keep it fast and idempotent.

```bash
#!/usr/bin/env bash
set -e
apt-get install -y some-package
pip3 install some-lib --break-system-packages
cp clawbot-my-module.service /etc/systemd/system/
systemctl daemon-reload
```

---

## Submitting to the Store

1. Create your module repo (`github.com/yourname/clawbot-my-module`)
2. Test it manually: `POST /core/modules/my-module/install {"repo": "https://github.com/yourname/clawbot-my-module"}`
3. Open a Pull Request on [Yumi-Lab/clawbot-core](https://github.com/Yumi-Lab/clawbot-core) adding your module to `store/index.json`
4. A Yumi Lab maintainer will review and merge

---

## Example: Hello World Module

See `module-template/` in this repository for a working starting point.
