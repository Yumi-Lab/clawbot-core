# ClawbotCore

The AI orchestrator for [ClawbotOS](https://github.com/Yumi-Lab/ClawBot-OS) — multi-provider LLM routing, tool execution loop, credential vault, messaging channels, and module system.

ClawbotCore is the middleware that connects LLM providers to system tools, messaging channels, and installable modules on the device.

## Features

- **Multi-provider LLM routing** — Kimi (default), Qwen, Claude, DeepSeek, OpenAI, Ollama
- **Tool execution loop** — up to 15 rounds of function calling per request
- **49 built-in tools** — bash, python, read/write files, web search (DuckDuckGo), SSH
- **Tool registry** — standardized tool dispatch with module integration
- **Credential vault** — AES-256 encrypted secret storage with TOTP support
- **Messaging channels** — Web (SSE), WhatsApp (Baileys 7), Telegram, WeCom
- **Mid-stream injection** — inject follow-up messages into active tool loops
- **Context compaction** — automatic conversation summarization at token thresholds
- **Cloud tunnel** — WebSocket connection to [openjarvis.io](https://openjarvis.io) for remote access
- **Module system** — installable extensions with their own tools and services
- **stdlib-only** — no pip dependencies required (except websockets for cloud)

## Architecture

```
┌──────────────────────────────────────────────────┐
│                ClawbotCore  :8090                 │
│                                                  │
│  Orchestrator ─── LLM Proxy ─── Tool Registry    │
│       │              │               │            │
│  Session Mgr    Multi-Provider   49 built-in      │
│  Compaction     Kimi/Qwen/Claude + module tools   │
│  Injection      + cloud proxy                     │
│       │                                           │
│  Channels ─── Vault ─── Module Registry           │
│  Web│WA│TG    AES-256   Install/enable/disable    │
└──────┬───────────────────────────┬────────────────┘
       │                           │
  LLM APIs                    modules/
  Kimi, Qwen, Claude          telegram, whatsapp,
  DeepSeek, OpenAI             voice, screen...
```

## API

### Chat

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/v1/chat/completions` | OpenAI-compatible chat (SSE streaming) |
| POST | `/v1/chat/inject` | Inject follow-up into active tool loop |
| POST | `/v1/chat/agents` | Agent mode with extended tool access |
| GET | `/v1/models` | List available models |

### Modules

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/core/modules` | List all modules (installed + store) |
| POST | `/core/modules/{id}/install` | Install from GitHub |
| POST | `/core/modules/{id}/enable` | Start module service |
| POST | `/core/modules/{id}/disable` | Stop module service |

### Channels

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/v1/channels/whatsapp/inbound` | WhatsApp message webhook |
| GET | `/v1/channels/whatsapp/status` | WhatsApp bridge status |
| POST | `/v1/channels/wecom/inbound` | WeCom message webhook |

### System

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/core/health` | Health check |
| POST | `/config` | Update LLM config |
| GET | `/sessions` | List chat sessions |
| POST | `/vault/*` | Credential vault operations |

## Configuration

Config file: `/home/pi/.clawbot/config.json`

INI config: `/etc/clawbot/clawbot.cfg` + `/etc/clawbot/conf.d/*.cfg`

### Constants

| Constant | Value | Description |
|----------|-------|-------------|
| `MAX_TOOL_ROUNDS` | 15 | Max tool call iterations per request |
| `LLM_TIMEOUT` | 240s | LLM API timeout |
| `TOOL_TIMEOUT` | 10s | Individual tool execution timeout |
| `TOOL_RESULT_MAX_CHARS` | 6000 | Truncation limit for tool results |
| `COMPACT_THRESHOLD` | 15000 | Token count triggering compaction |

## Messaging Channels

| Channel | Transport | Port | Status |
|---------|-----------|------|--------|
| **Web** | SSE (Server-Sent Events) | 8090 | Active |
| **WhatsApp** | Baileys 7 (Node.js) | 3100 | Active |
| **Telegram** | Bot API | — | Available |
| **WeCom** | Tencent SDK (WebSocket) | 3101 | Available |

## Module System

Modules extend ClawbotCore with new tools and services. Each module provides:
- `manifest.json` — tool definitions, port, service name
- `install.sh` — setup script
- systemd service unit
- HTTP API at `http://127.0.0.1:{port}/v1/{id}/execute`

Tool naming convention: `{module_id}__{tool_name}` (double underscore).

## Official Modules

| Module | Description | Status |
|--------|-------------|--------|
| `telegram` | Telegram Bot bridge | Available |
| `whatsapp-bridge` | WhatsApp via Baileys 7 | Active |
| `wecom-bridge` | WeCom enterprise bridge | Available |
| `voice` | Speech recognition + TTS | Coming soon |
| `screen` | SmartPad touchscreen UI | Coming soon |
| `camera` | Camera vision | Coming soon |
| `mqtt` | Home automation (MQTT) | Coming soon |

## Installation

ClawbotCore is pre-installed on [ClawbotOS](https://github.com/Yumi-Lab/ClawBot-OS) images.

To install manually on any Debian/Armbian device:

```bash
# One-liner (as root)
curl -fsSL https://raw.githubusercontent.com/Yumi-Lab/clawbot-core/main/install.sh | bash

# Or after git clone
git clone https://github.com/Yumi-Lab/clawbot-core.git
cd clawbot-core
sudo bash install.sh
```

### What install.sh does

1. Installs system packages: `python3`, `python3-venv`, `git`, `curl`, `sshpass`, `jq`
2. Installs Node.js 20 (for WhatsApp bridge)
3. Clones the repo to `/usr/local/lib/clawbot-core`
4. Installs `websockets` Python package (only external dependency)
5. Installs WhatsApp bridge npm dependencies (`@whiskeysockets/baileys`, `express`, `pino`, `qrcode`)
6. Creates config at `/etc/clawbot/clawbot.cfg`
7. Creates data dir at `/home/pi/.clawbot/`
8. Sets up systemd services: `clawbot-core`, `whatsapp-bridge`

### Dependencies

| Type | Packages |
|------|----------|
| **System** | python3, python3-venv, git, curl, wget, sshpass, jq |
| **Python** | stdlib only + `websockets` (cloud tunnel) |
| **Node.js** | Node.js >= 18 (installed automatically) |
| **npm** | @whiskeysockets/baileys, express, pino, qrcode |

## Related Repositories

| Repo | Description |
|------|-------------|
| [Yumi-Lab/ClawBot-OS](https://github.com/Yumi-Lab/ClawBot-OS) | ClawbotOS — full OS image build |
| [Yumi-Lab/ClawbotCore-WebUI](https://github.com/Yumi-Lab/ClawbotCore-WebUI) | Web dashboard |
| [Yumi-Lab/clawbot-cloud](https://github.com/Yumi-Lab/clawbot-cloud) | Cloud API |

## License

GPL-3.0
