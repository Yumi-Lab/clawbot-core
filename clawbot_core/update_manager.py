"""
ClawbotCore — Update Manager
Moonraker-style update manager.

Each repo drops its own .cfg in /etc/clawbot/conf.d/
The main /etc/clawbot/clawbot.cfg can also declare sections directly.

Drop-in pattern (each repo's install.sh writes this file):
  /etc/clawbot/conf.d/clawbot-interface.cfg:
    [update_manager clawbot-interface]
    type: git_repo
    path: /home/pi/clawbot-dashboard
    origin: https://github.com/Yumi-Lab/clawbot-interface.git
    managed_services: nginx.service

API:
  GET  /core/updates               — list all managed repos + update status
  POST /core/updates/{name}/update — git pull + restart managed_services
"""

import configparser
import logging
import os
import subprocess

CONFIG_MAIN = "/etc/clawbot/clawbot.cfg"
CONFIG_DIR  = "/etc/clawbot/conf.d"
log = logging.getLogger(__name__)


def _run(cmd, cwd=None, timeout=60):
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, cwd=cwd)
    return r.returncode == 0, (r.stdout + r.stderr).strip()


def _parse_all_configs() -> dict:
    """Parse main config + all conf.d drop-ins. Returns {name: section_dict}."""
    cfg = configparser.RawConfigParser()
    if os.path.isfile(CONFIG_MAIN):
        cfg.read(CONFIG_MAIN)
    if os.path.isdir(CONFIG_DIR):
        for fname in sorted(os.listdir(CONFIG_DIR)):
            if fname.endswith(".cfg"):
                cfg.read(os.path.join(CONFIG_DIR, fname))

    managers = {}
    for section in cfg.sections():
        if section.lower().startswith("update_manager "):
            name = section[len("update_manager "):].strip()
            managers[name] = {
                "name": name,
                "type": cfg.get(section, "type", fallback="git_repo"),
                "path": cfg.get(section, "path", fallback=""),
                "origin": cfg.get(section, "origin", fallback=""),
                "primary_branch": cfg.get(section, "primary_branch", fallback="main"),
                "managed_services": [
                    s.strip()
                    for s in cfg.get(section, "managed_services", fallback="").split()
                    if s.strip()
                ],
            }
    return managers


def _git_status(path: str) -> dict:
    if not os.path.isdir(os.path.join(path, ".git")):
        return {"installed": False, "current": None, "remote": None,
                "commits_behind": 0, "update_available": False}

    ok, current = _run(["git", "rev-parse", "--short", "HEAD"], cwd=path)
    current = current if ok else "unknown"

    _run(["git", "fetch", "--quiet", "origin"], cwd=path, timeout=20)

    ok, remote = _run(["git", "rev-parse", "--short", "origin/HEAD"], cwd=path)
    if not ok:
        _, branch = _run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=path)
        branch = branch.strip() or "main"
        ok, remote = _run(["git", "rev-parse", "--short", f"origin/{branch}"], cwd=path)
    remote = remote if ok else "unknown"

    ok, behind = _run(["git", "rev-list", "--count", "HEAD..origin/HEAD"], cwd=path)
    try:
        commits_behind = int(behind) if ok else 0
    except ValueError:
        commits_behind = 0

    return {
        "installed": True,
        "current": current,
        "remote": remote,
        "commits_behind": commits_behind,
        "update_available": commits_behind > 0,
    }


def list_updates() -> list:
    managers = _parse_all_configs()
    return [{**mgr, **_git_status(mgr["path"])} for mgr in managers.values()]


def do_update(name: str) -> tuple[bool, str]:
    managers = _parse_all_configs()
    if name not in managers:
        return False, f"Unknown update_manager section: '{name}'"
    mgr = managers[name]
    path = mgr["path"]
    if not os.path.isdir(os.path.join(path, ".git")):
        return False, f"Not a git repo (no .git): {path}"
    log.info("Updating %s at %s", name, path)
    ok, out = _run(["git", "pull", "--ff-only"], cwd=path, timeout=60)
    if not ok:
        return False, f"git pull failed: {out}"
    for svc in mgr.get("managed_services", []):
        log.info("Restarting %s", svc)
        _run(["systemctl", "restart", svc], timeout=30)
    return True, f"Updated {name}: {out[:200]}"
