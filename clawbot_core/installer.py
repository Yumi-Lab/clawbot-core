"""
ClawbotCore — Module Installer
Handles install / enable / disable of community modules.
"""

import json
import os
import subprocess
import shutil

MODULES_DIR = "/home/pi/.openjarvis/modules"
SYSTEMD_DIR = "/etc/systemd/system"


def _run(cmd, timeout=60):
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    return result.returncode == 0, result.stdout + result.stderr


def install(module_id, repo_url):
    """
    Clone repo and run its install.sh.
    Returns (ok, message).
    """
    dest = os.path.join(MODULES_DIR, module_id)
    if os.path.exists(dest):
        return False, f"Module '{module_id}' already installed at {dest}"

    os.makedirs(MODULES_DIR, exist_ok=True)

    # Clone
    ok, out = _run(["git", "clone", "--depth=1", repo_url, dest], timeout=60)
    if not ok:
        shutil.rmtree(dest, ignore_errors=True)
        return False, f"git clone failed: {out}"

    # Validate manifest
    manifest_path = os.path.join(dest, "manifest.json")
    if not os.path.exists(manifest_path):
        shutil.rmtree(dest, ignore_errors=True)
        return False, "Missing manifest.json in module repo"

    try:
        with open(manifest_path) as f:
            manifest = json.load(f)
        if manifest.get("id") != module_id:
            shutil.rmtree(dest, ignore_errors=True)
            return False, f"manifest.json id mismatch (expected '{module_id}')"
    except Exception as e:
        shutil.rmtree(dest, ignore_errors=True)
        return False, f"Invalid manifest.json: {e}"

    # Run install.sh if present
    install_script = os.path.join(dest, "install.sh")
    if os.path.exists(install_script):
        os.chmod(install_script, 0o755)
        ok, out = _run(["bash", install_script], timeout=180)
        if not ok:
            shutil.rmtree(dest, ignore_errors=True)
            return False, f"install.sh failed: {out[:500]}"

    # Install systemd service if present
    service_name = manifest.get("service", "")
    service_file = os.path.join(dest, service_name)
    if service_name and os.path.exists(service_file):
        shutil.copy(service_file, os.path.join(SYSTEMD_DIR, service_name))
        _run(["systemctl", "daemon-reload"])

    return True, f"Module '{module_id}' installed successfully"


def uninstall(module_id):
    dest = os.path.join(MODULES_DIR, module_id)
    if not os.path.exists(dest):
        return False, f"Module '{module_id}' not installed"

    # Read manifest to get service name
    manifest_path = os.path.join(dest, "manifest.json")
    try:
        with open(manifest_path) as f:
            manifest = json.load(f)
        service = manifest.get("service", "")
        if service:
            _run(["systemctl", "stop", service])
            _run(["systemctl", "disable", service])
            svc_path = os.path.join(SYSTEMD_DIR, service)
            if os.path.exists(svc_path):
                os.remove(svc_path)
            _run(["systemctl", "daemon-reload"])
    except Exception:
        pass

    shutil.rmtree(dest, ignore_errors=True)
    return True, f"Module '{module_id}' uninstalled"


def enable(module_id):
    service = _get_service(module_id)
    if not service:
        return False, "Module not installed or no service defined"
    ok, out = _run(["systemctl", "enable", "--now", service])
    return ok, out.strip() or ("enabled" if ok else "failed")


def disable(module_id):
    service = _get_service(module_id)
    if not service:
        return False, "Module not installed or no service defined"
    ok, out = _run(["systemctl", "disable", "--now", service])
    return ok, out.strip() or ("disabled" if ok else "failed")


def _get_service(module_id):
    manifest_path = os.path.join(MODULES_DIR, module_id, "manifest.json")
    try:
        with open(manifest_path) as f:
            return json.load(f).get("service", "")
    except Exception:
        return ""
