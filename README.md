# ClawbotCore

The middleware layer for [ClawbotOS](https://github.com/Yumi-Lab/clawbot) — module registry, lifecycle management, and mini-app store.

ClawbotCore is to ClawbotOS what Moonraker is to Klipper: the AI orchestrator that connects LLM providers to all additional capabilities (modules).

## Architecture

```
┌────────────────────────────────────────────┐
│         ClawbotCore  :8090                 │
│                                            │
│  • Module registry (local + store)         │
│  • Install / enable / disable modules      │
│  • Mini web app hosting                    │
└──────┬─────────────┬──────────────┬────────┘
       │             │              │
  LLM APIs      status-api      modules/
  (cloud)        :8089          voice, screen,
                                camera, mqtt...
```

Each module is an independent service with its own port, managed by ClawbotCore and proxied through nginx.

## API

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/core/health` | Health check |
| GET | `/core/modules` | List all modules (installed + store) |
| GET | `/core/modules/{id}` | Module details |
| POST | `/core/modules/{id}/install` | Install from GitHub `{"repo": "https://..."}` |
| POST | `/core/modules/{id}/enable` | Start module service |
| POST | `/core/modules/{id}/disable` | Stop module service |
| POST | `/core/modules/{id}/uninstall` | Remove module |

## Module Store

Available modules are listed in [`store/index.json`](store/index.json).

Community members can submit modules by opening a Pull Request adding an entry to `store/index.json`. See [docs/module-spec.md](docs/module-spec.md) for the full specification.

## Creating a Module

1. Fork or copy [`module-template/`](module-template/)
2. Implement `manifest.json`, `install.sh`, systemd service, and your Python/any-language service
3. Provide `GET /v1/{id}/status` → `{"ok": true}`
4. Optionally provide `app.html` as a mini web app panel
5. Submit a PR to add your module to the store

See [docs/module-spec.md](docs/module-spec.md) for full details.

## Official Modules

| Module | Description | Status |
|--------|-------------|--------|
| `telegram` | Telegram Bot bridge | ✅ Available |
| `voice` | Speech recognition + TTS | 🚧 Coming soon |
| `screen` | SmartPad touchscreen UI | 🚧 Coming soon |
| `camera` | Camera vision | 🚧 Coming soon |
| `mqtt` | Home automation (MQTT) | 🚧 Coming soon |

## License

GPL-3.0
