"""
ClawbotCore — Encrypted Credential Vault
AES-256-CBC encryption via openssl subprocess, SQLite storage.
Stdlib-only — no pip dependencies.
"""
from __future__ import annotations

import base64
import glob
import hashlib
import json
import logging
import os
import re
import sqlite3
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timezone

log = logging.getLogger("vault")


class VaultError(Exception):
    pass


class Vault:
    VAULT_PATH = "/home/pi/.openjarvis/vault.db"

    def __init__(self, master_key: bytes | None = None, vault_path: str | None = None):
        if vault_path:
            self.VAULT_PATH = vault_path
        self._init_db()
        if master_key:
            self._key = master_key
        else:
            self._key = self._derive_key()
        # Auto-unlock at init if auto-unlock key is stored
        try:
            self.try_auto_unlock()
        except Exception:
            pass

    # ── Key derivation ───────────────────────────────────────────────────

    @staticmethod
    def _get_mac() -> str:
        """Read first non-lo MAC from /sys/class/net/*/address."""
        for path in sorted(glob.glob("/sys/class/net/*/address")):
            iface = path.split("/")[-2]
            if iface == "lo":
                continue
            try:
                with open(path) as f:
                    mac = f.read().strip()
                if mac and mac != "00:00:00:00:00:00":
                    return mac
            except OSError:
                continue
        raise VaultError("No network interface found for key derivation")

    def _derive_key(self, master_password: str | None = None) -> bytes:
        """Derive AES-256 key from device MAC + optional master password.
        If master password set in DB but not provided, uses MAC-only (legacy).
        If master password provided, uses PBKDF2(password + mac, salt, 100k)."""
        mac = self._get_mac()
        # Check if master password is stored and use it
        if master_password is None:
            master_password = self._get_stored_master_password_for_key()
        if master_password:
            material = (master_password + mac).encode()
        else:
            material = mac.encode()
        return hashlib.pbkdf2_hmac(
            "sha256",
            material,
            b"clawbot-vault-v1",
            100_000,
        )

    def _get_stored_master_password_for_key(self) -> str | None:
        """Internal: returns None — master password is never stored in clear.
        Key derivation uses MAC-only until unlock() is called with password."""
        return getattr(self, '_unlocked_password', None)

    # ── Master password ──────────────────────────────────────────────────

    def has_master_password(self) -> bool:
        """Check if a master password has been configured."""
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT master_hash FROM vault_meta WHERE id = 1"
            ).fetchone()
            return row is not None and bool(row[0])
        except sqlite3.Error:
            return False
        finally:
            conn.close()

    def verify_master_password(self, password: str) -> bool:
        """Verify a master password against stored hash."""
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT master_hash, master_salt FROM vault_meta WHERE id = 1"
            ).fetchone()
            if not row or not row[0]:
                return False
            stored_hash, salt_hex = row[0], row[1]
            salt = bytes.fromhex(salt_hex)
            computed = hashlib.pbkdf2_hmac(
                "sha256", password.encode(), salt, 100_000
            ).hex()
            return computed == stored_hash
        except sqlite3.Error:
            return False
        finally:
            conn.close()

    def set_master_password(self, password: str) -> bool:
        """Set or change master password. Re-encrypts all secrets with new key.
        The password is NEVER stored — only its PBKDF2 hash for verification."""
        if len(password) < 8:
            raise VaultError("Master password must be at least 8 characters")

        # 1. Decrypt all existing secrets with current key
        conn = self._connect()
        try:
            secrets = conn.execute("SELECT name, value, category FROM secrets").fetchall()
            protected = conn.execute("SELECT name, value, alias, kind, category FROM protected").fetchall()
        finally:
            conn.close()

        plaintext_secrets = []
        for name, enc_val, cat in secrets:
            try:
                plain = self._decrypt(enc_val)
                plaintext_secrets.append((name, plain, cat))
            except VaultError:
                log.warning("Could not decrypt secret %s during re-key, skipping", name)

        plaintext_protected = []
        for name, enc_val, alias, kind, cat in protected:
            try:
                plain = self._decrypt(enc_val)
                plaintext_protected.append((name, plain, alias, kind, cat))
            except VaultError:
                log.warning("Could not decrypt protected %s during re-key, skipping", name)

        # 2. Store password hash (PBKDF2, separate salt)
        salt = os.urandom(32)
        pw_hash = hashlib.pbkdf2_hmac(
            "sha256", password.encode(), salt, 100_000
        ).hex()
        conn = self._connect()
        try:
            conn.execute(
                """INSERT INTO vault_meta (id, master_hash, master_salt)
                   VALUES (1, ?, ?)
                   ON CONFLICT(id) DO UPDATE SET
                       master_hash = excluded.master_hash,
                       master_salt = excluded.master_salt""",
                (pw_hash, salt.hex()),
            )
            conn.commit()
        finally:
            conn.close()

        # 3. Derive new key with master password
        self._unlocked_password = password
        new_key = self._derive_key(master_password=password)
        old_key = self._key
        self._key = new_key

        # 4. Re-encrypt all secrets with new key
        now = datetime.now(timezone.utc).isoformat()
        conn = self._connect()
        try:
            for name, plain, cat in plaintext_secrets:
                encrypted = self._encrypt(plain)
                conn.execute(
                    "UPDATE secrets SET value = ?, updated_at = ? WHERE name = ?",
                    (encrypted, now, name),
                )
            for name, plain, alias, kind, cat in plaintext_protected:
                encrypted = self._encrypt(plain)
                conn.execute(
                    "UPDATE protected SET value = ?, updated_at = ? WHERE name = ?",
                    (encrypted, now, name),
                )
            conn.commit()
            self._invalidate_subs_cache()
            log.info("Master password set, %d secrets + %d protected re-encrypted",
                     len(plaintext_secrets), len(plaintext_protected))
            return True
        except Exception:
            # Rollback to old key on failure
            self._key = old_key
            log.error("Re-encryption failed, rolling back to old key")
            raise
        finally:
            conn.close()

    def unlock(self, password: str) -> bool:
        """Unlock vault with master password. Re-derives key with password.
        Returns True on success, False if password wrong."""
        if not self.verify_master_password(password):
            return False
        self._unlocked_password = password
        self._key = self._derive_key(master_password=password)
        self._invalidate_subs_cache()
        log.info("Vault unlocked with master password")
        # Auto-enable auto-unlock on every successful unlock
        self.enable_auto_unlock()
        return True

    def is_locked(self) -> bool:
        """Check if vault requires unlock (has master password but not unlocked)."""
        if not self.has_master_password():
            return False
        return not hasattr(self, '_unlocked_password') or self._unlocked_password is None

    # ── Auto-unlock (boot) ──────────────────────────────────────────────

    def _mac_only_key(self) -> bytes:
        """Derive a key from MAC only (no master password) for auto-unlock storage."""
        mac = self._get_mac()
        return hashlib.pbkdf2_hmac("sha256", mac.encode(), b"clawbot-auto-unlock-v1", 100_000)

    def enable_auto_unlock(self) -> bool:
        """Store master password encrypted with MAC-only key for auto-unlock at boot.
        Must be called while vault is unlocked."""
        pw = getattr(self, '_unlocked_password', None)
        if not pw:
            return False
        # Encrypt master password with MAC-only key
        mac_key = self._mac_only_key()
        proc = subprocess.run(
            ["openssl", "enc", "-aes-256-cbc", "-pbkdf2", "-salt", "-base64",
             "-pass", f"pass:{mac_key.hex()}"],
            input=pw.encode(), capture_output=True, timeout=10,
        )
        if proc.returncode != 0:
            return False
        encrypted_pw = proc.stdout.decode().strip()
        conn = self._connect()
        try:
            conn.execute(
                """INSERT INTO vault_meta (id, auto_unlock_key) VALUES (1, ?)
                   ON CONFLICT(id) DO UPDATE SET auto_unlock_key = excluded.auto_unlock_key""",
                (encrypted_pw,),
            )
            conn.commit()
            log.info("Auto-unlock enabled")
            return True
        except sqlite3.Error as e:
            log.error("Failed to enable auto-unlock: %s", e)
            return False
        finally:
            conn.close()

    def disable_auto_unlock(self) -> bool:
        """Remove auto-unlock key."""
        conn = self._connect()
        try:
            conn.execute("UPDATE vault_meta SET auto_unlock_key = NULL WHERE id = 1")
            conn.commit()
            log.info("Auto-unlock disabled")
            return True
        except sqlite3.Error:
            return False
        finally:
            conn.close()

    def try_auto_unlock(self) -> bool:
        """Try to auto-unlock vault at boot using stored auto-unlock key.
        Returns True if unlocked, False if no key or failed."""
        if not self.has_master_password() or not self.is_locked():
            return False
        conn = self._connect()
        try:
            row = conn.execute("SELECT auto_unlock_key FROM vault_meta WHERE id = 1").fetchone()
            if not row or not row[0]:
                return False
            encrypted_pw = row[0]
        except sqlite3.Error:
            return False
        finally:
            conn.close()
        # Decrypt with MAC-only key
        mac_key = self._mac_only_key()
        proc = subprocess.run(
            ["openssl", "enc", "-aes-256-cbc", "-pbkdf2", "-d", "-base64",
             "-pass", f"pass:{mac_key.hex()}"],
            input=(encrypted_pw + "\n").encode(), capture_output=True, timeout=10,
        )
        if proc.returncode != 0:
            log.warning("Auto-unlock failed — key invalid or device changed")
            return False
        password = proc.stdout.decode().strip()
        if self.unlock(password):
            log.info("Vault auto-unlocked at boot")
            return True
        log.warning("Auto-unlock: decrypted key but unlock failed")
        return False

    # ── Export / import encrypted ─────────────────────────────────────────

    def export_encrypted(self, password: str) -> bytes:
        """Export entire vault as AES-256 encrypted blob.
        Uses the provided password for encryption (independent of vault key).
        Returns raw encrypted bytes."""
        if self.is_locked():
            raise VaultError("Vault is locked — unlock first")

        # Collect all data
        conn = self._connect()
        try:
            secrets = []
            for row in conn.execute("SELECT name, value, category, created_at, updated_at FROM secrets").fetchall():
                try:
                    plain = self._decrypt(row[1])
                    secrets.append({"name": row[0], "value": plain, "category": row[2],
                                    "created_at": row[3], "updated_at": row[4]})
                except VaultError:
                    continue

            protected_items = []
            for row in conn.execute("SELECT name, value, alias, kind, category, created_at, updated_at FROM protected").fetchall():
                try:
                    plain = self._decrypt(row[1])
                    protected_items.append({"name": row[0], "value": plain, "alias": row[2],
                                            "kind": row[3], "category": row[4],
                                            "created_at": row[5], "updated_at": row[6]})
                except VaultError:
                    continue

            patterns = []
            for row in conn.execute("SELECT name, pattern, category, source, hits, created_at FROM learned_patterns").fetchall():
                patterns.append({"name": row[0], "pattern": row[1], "category": row[2],
                                 "source": row[3], "hits": row[4], "created_at": row[5]})
        finally:
            conn.close()

        payload = json.dumps({
            "version": 1,
            "exported_at": datetime.now(timezone.utc).isoformat(),
            "secrets": secrets,
            "protected": protected_items,
            "learned_patterns": patterns,
        }, ensure_ascii=False)

        # Encrypt with openssl using the provided password
        try:
            proc = subprocess.run(
                ["openssl", "enc", "-aes-256-cbc", "-pbkdf2", "-salt",
                 "-pass", f"pass:{password}"],
                input=payload.encode("utf-8"),
                capture_output=True, timeout=30,
            )
        except subprocess.TimeoutExpired as e:
            raise VaultError("Export encryption timed out") from e
        if proc.returncode != 0:
            raise VaultError(f"Export encryption failed: {proc.stderr.decode().strip()}")
        return proc.stdout

    def import_encrypted(self, data: bytes, password: str) -> dict:
        """Import vault from encrypted backup. Decrypts with password, merges into vault.
        Returns {'imported': N, 'skipped': N, 'errors': []}."""
        result = {"imported": 0, "skipped": 0, "errors": []}

        # Decrypt
        try:
            proc = subprocess.run(
                ["openssl", "enc", "-aes-256-cbc", "-pbkdf2", "-d",
                 "-pass", f"pass:{password}"],
                input=data,
                capture_output=True, timeout=30,
            )
        except subprocess.TimeoutExpired:
            result["errors"].append("Decryption timed out")
            return result
        if proc.returncode != 0:
            result["errors"].append("Wrong password or corrupted backup")
            return result

        try:
            payload = json.loads(proc.stdout.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            result["errors"].append(f"Invalid backup format: {e}")
            return result

        if payload.get("version") != 1:
            result["errors"].append(f"Unsupported backup version: {payload.get('version')}")
            return result

        # Import secrets
        for s in payload.get("secrets", []):
            try:
                self.store(s["name"], s["value"], s.get("category", ""))
                result["imported"] += 1
            except Exception as e:
                result["errors"].append(f"secret {s['name']}: {e}")

        # Import protected
        for p in payload.get("protected", []):
            try:
                self.protect(p["name"], p["value"],
                             kind=p.get("kind", "pii"),
                             category=p.get("category", ""))
                result["imported"] += 1
            except Exception as e:
                result["errors"].append(f"protected {p['name']}: {e}")

        # Import learned patterns
        for lp in payload.get("learned_patterns", []):
            try:
                self.learn_pattern(lp["name"], lp["pattern"], lp.get("category", ""))
                result["imported"] += 1
            except Exception as e:
                result["errors"].append(f"pattern {lp['name']}: {e}")

        self._invalidate_subs_cache()
        log.info("Vault import: %d imported, %d skipped, %d errors",
                 result["imported"], result["skipped"], len(result["errors"]))
        return result

    # ── USB detection ─────────────────────────────────────────────────────

    @staticmethod
    def detect_usb_drives() -> list[dict]:
        """Detect mounted USB drives. Returns list of {path, label, backups: []}."""
        drives = []
        search_dirs = ["/media/pi", "/media", "/mnt"]
        for base in search_dirs:
            if not os.path.isdir(base):
                continue
            for entry in os.listdir(base):
                mount_path = os.path.join(base, entry)
                if not os.path.isdir(mount_path):
                    continue
                # Skip system dirs
                if entry in ("cdrom", "floppy", "pi"):
                    if entry == "pi":
                        # /media/pi is the parent, list its children
                        for sub in os.listdir(mount_path):
                            sub_path = os.path.join(mount_path, sub)
                            if os.path.isdir(sub_path):
                                backups = Vault._list_backup_files(sub_path)
                                drives.append({"path": sub_path, "label": sub, "backups": backups})
                        continue
                    continue
                # Check if it looks like a USB mount
                backups = Vault._list_backup_files(mount_path)
                drives.append({"path": mount_path, "label": entry, "backups": backups})
        return drives

    @staticmethod
    def _list_backup_files(directory: str) -> list[str]:
        """List .enc backup files in a directory."""
        files = []
        try:
            for f in sorted(os.listdir(directory), reverse=True):
                if f.startswith("clawbot-vault-backup-") and f.endswith(".enc"):
                    files.append(f)
        except OSError:
            pass
        return files

    def backup_to_usb(self, password: str, usb_path: str) -> str:
        """Export encrypted vault to USB drive. Returns the backup filename.
        If password is '__auto__', uses the unlocked master password."""
        if not os.path.isdir(usb_path):
            raise VaultError(f"USB path not found: {usb_path}")
        if password == "__auto__":
            pw = getattr(self, '_unlocked_password', None)
            if not pw:
                raise VaultError("Vault not unlocked — cannot auto-backup")
            password = pw
        elif self.has_master_password() and not self.verify_master_password(password):
            raise VaultError("Invalid master password")
        data = self.export_encrypted(password)
        filename = f"clawbot-vault-backup-{datetime.now().strftime('%Y%m%d-%H%M%S')}.enc"
        filepath = os.path.join(usb_path, filename)
        with open(filepath, "wb") as f:
            f.write(data)
        log.info("Vault backup saved to %s (%d bytes)", filepath, len(data))
        return filename

    def restore_from_usb(self, password: str, backup_path: str) -> dict:
        """Restore vault from USB backup file."""
        if not os.path.isfile(backup_path):
            raise VaultError(f"Backup file not found: {backup_path}")
        with open(backup_path, "rb") as f:
            data = f.read()
        return self.import_encrypted(data, password)

    # ── Encryption / decryption via openssl ──────────────────────────────

    def _encrypt(self, plaintext: str) -> str:
        """Encrypt with openssl aes-256-cbc, return base64 string."""
        hex_key = self._key.hex()
        try:
            proc = subprocess.run(
                [
                    "openssl", "enc", "-aes-256-cbc",
                    "-pbkdf2", "-salt", "-base64",
                    "-pass", f"pass:{hex_key}",
                ],
                input=plaintext.encode(),
                capture_output=True,
                timeout=10,
            )
        except subprocess.TimeoutExpired as e:
            raise VaultError("openssl encrypt timed out") from e
        if proc.returncode != 0:
            raise VaultError(f"openssl encrypt failed: {proc.stderr.decode().strip()}")
        return proc.stdout.decode().strip()

    def _decrypt(self, ciphertext: str) -> str:
        """Decrypt base64 ciphertext with openssl aes-256-cbc."""
        hex_key = self._key.hex()
        try:
            proc = subprocess.run(
                [
                    "openssl", "enc", "-aes-256-cbc",
                    "-pbkdf2", "-d", "-base64",
                    "-pass", f"pass:{hex_key}",
                ],
                input=(ciphertext + "\n").encode(),
                capture_output=True,
                timeout=10,
            )
        except subprocess.TimeoutExpired as e:
            raise VaultError("openssl decrypt timed out") from e
        if proc.returncode != 0:
            raise VaultError(f"openssl decrypt failed: {proc.stderr.decode().strip()}")
        return proc.stdout.decode().strip()

    # ── Database ─────────────────────────────────────────────────────────

    def _init_db(self):
        """Create vault database and table if needed."""
        db_dir = os.path.dirname(self.VAULT_PATH)
        if db_dir:
            os.makedirs(db_dir, exist_ok=True)
        conn = sqlite3.connect(self.VAULT_PATH)
        conn.execute(
            """CREATE TABLE IF NOT EXISTS secrets (
                name       TEXT PRIMARY KEY,
                value      TEXT NOT NULL,
                username   TEXT DEFAULT '',
                category   TEXT DEFAULT '',
                note       TEXT DEFAULT '',
                created_at TEXT,
                updated_at TEXT
            )"""
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS protected (
                name       TEXT PRIMARY KEY,
                value      TEXT NOT NULL,
                alias      TEXT NOT NULL,
                kind       TEXT DEFAULT 'pii',
                category   TEXT DEFAULT '',
                created_at TEXT,
                updated_at TEXT
            )"""
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS learned_patterns (
                name       TEXT PRIMARY KEY,
                pattern    TEXT NOT NULL,
                category   TEXT DEFAULT '',
                source     TEXT DEFAULT 'llm',
                hits       INTEGER DEFAULT 0,
                created_at TEXT
            )"""
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS vault_meta (
                id           INTEGER PRIMARY KEY DEFAULT 1,
                master_hash  TEXT,
                master_salt  TEXT,
                auto_unlock_key TEXT
            )"""
        )
        # Migrate: add auto_unlock_key column if missing (existing DBs)
        try:
            conn.execute("SELECT auto_unlock_key FROM vault_meta LIMIT 0")
        except sqlite3.OperationalError:
            conn.execute("ALTER TABLE vault_meta ADD COLUMN auto_unlock_key TEXT")
        # Migrate: add note column to secrets if missing
        try:
            conn.execute("SELECT note FROM secrets LIMIT 0")
        except sqlite3.OperationalError:
            conn.execute("ALTER TABLE secrets ADD COLUMN note TEXT DEFAULT ''")
        # Migrate: add username column to secrets if missing
        try:
            conn.execute("SELECT username FROM secrets LIMIT 0")
        except sqlite3.OperationalError:
            conn.execute("ALTER TABLE secrets ADD COLUMN username TEXT DEFAULT ''")
        conn.commit()
        conn.close()
        try:
            os.chmod(self.VAULT_PATH, 0o600)
        except OSError:
            log.warning("Could not set vault permissions to 600")

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.VAULT_PATH)

    # ── CRUD ─────────────────────────────────────────────────────────────

    def store(self, name: str, value: str, category: str = "", note: str = "", username: str = "") -> bool:
        """Encrypt and store a secret. Returns True on success."""
        encrypted = self._encrypt(value)
        now = datetime.now(timezone.utc).isoformat()
        conn = self._connect()
        try:
            conn.execute(
                """INSERT INTO secrets (name, value, username, category, note, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(name) DO UPDATE SET
                       value = excluded.value,
                       username = excluded.username,
                       category = excluded.category,
                       note = excluded.note,
                       updated_at = excluded.updated_at""",
                (name, encrypted, username, category, note, now, now),
            )
            conn.commit()
            self._invalidate_subs_cache()
            return True
        except sqlite3.Error as e:
            log.error("vault store error: %s", e)
            return False
        finally:
            conn.close()

    def get(self, name: str) -> str | None:
        """Retrieve and decrypt a secret by name."""
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT value FROM secrets WHERE name = ?", (name,)
            ).fetchone()
            if not row:
                return None
            return self._decrypt(row[0])
        except (sqlite3.Error, VaultError) as e:
            log.error("vault get error: %s", e)
            return None
        finally:
            conn.close()

    def list(self, category: str | None = None) -> list[dict]:
        """List secrets metadata (names + categories + notes, NEVER values)."""
        conn = self._connect()
        try:
            if category:
                rows = conn.execute(
                    "SELECT name, username, category, note, created_at, updated_at FROM secrets WHERE category = ? ORDER BY name",
                    (category,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT name, username, category, note, created_at, updated_at FROM secrets ORDER BY name"
                ).fetchall()
            return [
                {"name": r[0], "username": r[1] or "", "category": r[2], "note": r[3] or "", "created_at": r[4], "updated_at": r[5]}
                for r in rows
            ]
        except sqlite3.Error as e:
            log.error("vault list error: %s", e)
            return []
        finally:
            conn.close()

    def delete(self, name: str) -> bool:
        """Delete a secret by name. Returns True if it existed."""
        conn = self._connect()
        try:
            cursor = conn.execute("DELETE FROM secrets WHERE name = ?", (name,))
            conn.commit()
            self._invalidate_subs_cache()
            return cursor.rowcount > 0
        except sqlite3.Error as e:
            log.error("vault delete error: %s", e)
            return False
        finally:
            conn.close()

    def exists(self, name: str) -> bool:
        """Check if a secret exists without decrypting."""
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT 1 FROM secrets WHERE name = ?", (name,)
            ).fetchone()
            return row is not None
        except sqlite3.Error:
            return False
        finally:
            conn.close()

    # ── Protected table CRUD ──────────────────────────────────────────────

    def store_protected(self, name: str, value: str, kind: str = "pii", category: str = "") -> bool:
        """Encrypt and store a protected word. Auto-generates alias."""
        encrypted = self._encrypt(value)
        alias = f"__vault_{name}__"
        now = datetime.now(timezone.utc).isoformat()
        conn = self._connect()
        try:
            conn.execute(
                """INSERT INTO protected (name, value, alias, kind, category, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(name) DO UPDATE SET
                       value = excluded.value,
                       alias = excluded.alias,
                       kind = excluded.kind,
                       category = excluded.category,
                       updated_at = excluded.updated_at""",
                (name, encrypted, alias, kind, category, now, now),
            )
            conn.commit()
            self._invalidate_subs_cache()
            return True
        except sqlite3.Error as e:
            log.error("vault store_protected error: %s", e)
            return False
        finally:
            conn.close()

    def get_protected(self, name: str) -> str | None:
        """Retrieve and decrypt a protected word by name."""
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT value FROM protected WHERE name = ?", (name,)
            ).fetchone()
            if not row:
                return None
            return self._decrypt(row[0])
        except (sqlite3.Error, VaultError) as e:
            log.error("vault get_protected error: %s", e)
            return None
        finally:
            conn.close()

    def list_protected(self, kind: str | None = None, reveal: bool = False) -> list[dict]:
        """List protected words. If reveal=True and vault is unlocked, show clear values."""
        conn = self._connect()
        try:
            if kind:
                rows = conn.execute(
                    "SELECT name, value, alias, kind, category, created_at, updated_at FROM protected WHERE kind = ? ORDER BY name",
                    (kind,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT name, value, alias, kind, category, created_at, updated_at FROM protected ORDER BY name"
                ).fetchall()
            result = []
            for r in rows:
                try:
                    real_val = self._decrypt(r[1])
                    display = real_val if reveal else self.mask_value(real_val)
                except Exception:
                    display = "***"
                result.append({
                    "name": r[0], "masked_value": display, "alias": r[2],
                    "kind": r[3], "category": r[4],
                    "created_at": r[5], "updated_at": r[6]
                })
            return result
        except sqlite3.Error as e:
            log.error("vault list_protected error: %s", e)
            return []
        finally:
            conn.close()

    def delete_protected(self, name: str) -> bool:
        """Delete a protected word by name. Returns True if existed."""
        conn = self._connect()
        try:
            cursor = conn.execute("DELETE FROM protected WHERE name = ?", (name,))
            conn.commit()
            self._invalidate_subs_cache()
            return cursor.rowcount > 0
        except sqlite3.Error as e:
            log.error("vault delete_protected error: %s", e)
            return False
        finally:
            conn.close()

    # ── Helpers ────────────────────────────────────────────────────────────

    # ── Legacy migration ──────────────────────────────────────────────────

    def migrate_legacy_credentials(self) -> list[str]:
        """Migrate credentials from legacy config files into vault.
        Only migrates keys that don't already exist in the vault.
        Returns list of migrated key names."""
        import json as _json
        migrated = []

        # 1. config.json → llm_api_key
        if not self.exists("llm_api_key"):
            try:
                with open("/home/pi/.openjarvis/config.json") as f:
                    cfg = _json.load(f)
                models = cfg.get("model_list", [])
                if models:
                    key = models[0].get("api_key", "").strip()
                    if key:
                        self.store("llm_api_key", key, "llm")
                        migrated.append("llm_api_key")
                        log.info("Migrated llm_api_key from config.json")
            except Exception as e:
                log.warning("Could not migrate llm_api_key: %s", e)

        # 2. email.json → smtp_password + smtp_user + smtp_host
        if not self.exists("smtp_password"):
            try:
                with open("/home/pi/.openjarvis/email.json") as f:
                    ecfg = _json.load(f)
                pwd = ecfg.get("password", "").strip()
                if pwd:
                    self.store("smtp_password", pwd, "email")
                    migrated.append("smtp_password")
                    log.info("Migrated smtp_password from email.json")
            except Exception as e:
                log.warning("Could not migrate smtp_password: %s", e)

        # 3. core-prompts.json → brave_api_key
        if not self.exists("brave_api_key"):
            try:
                with open("/home/pi/.openjarvis/core-prompts.json") as f:
                    cpfg = _json.load(f)
                bkey = cpfg.get("brave_api_key", "").strip()
                if bkey:
                    self.store("brave_api_key", bkey, "api")
                    migrated.append("brave_api_key")
                    log.info("Migrated brave_api_key from core-prompts.json")
            except Exception as e:
                log.warning("Could not migrate brave_api_key: %s", e)

        if migrated:
            log.info("Vault migration complete: %s", ", ".join(migrated))
        else:
            log.info("Vault migration: nothing to migrate (already done or no credentials)")
        return migrated

    @staticmethod
    def mask_value(value: str) -> str:
        """Partially mask a secret value: first 3 chars + *** + last 3 chars."""
        if not value:
            return ""
        if len(value) <= 6:
            return value[:1] + "***" + value[-1:]
        return value[:3] + "***" + value[-3:]

    # ── VaultProxy: protect / unprotect / mask / unmask ────────────────────

    def protect(self, name: str, value: str, kind: str = "secret", category: str = "") -> str:
        """Register a protected value. Returns the generated alias."""
        alias = f"__vault_{name}__"
        encrypted = self._encrypt(value)
        now = datetime.now(timezone.utc).isoformat()
        conn = self._connect()
        try:
            conn.execute(
                """INSERT INTO protected (name, value, alias, kind, category, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(name) DO UPDATE SET
                       value=excluded.value, alias=excluded.alias,
                       kind=excluded.kind, category=excluded.category,
                       updated_at=excluded.updated_at""",
                (name, encrypted, alias, kind, category, now, now))
            conn.commit()
            self._invalidate_subs_cache()
            return alias
        finally:
            conn.close()

    def unprotect(self, name: str) -> bool:
        """Remove a protected value."""
        conn = self._connect()
        try:
            cur = conn.execute("DELETE FROM protected WHERE name = ?", (name,))
            conn.commit()
            self._invalidate_subs_cache()
            return cur.rowcount > 0
        finally:
            conn.close()

    # ── Substitution cache ─────────────────────────────────────────────────

    _subs_cache: list | None = None
    _subs_ts: float = 0

    def _invalidate_subs_cache(self):
        self._subs_cache = None
        self._subs_ts = 0

    def _load_substitutions(self) -> list[tuple[str, str]]:
        """Load all (real_value, alias) pairs for mask/unmask.
        Sorted by descending value length to avoid partial replacements.
        Cached for 5 seconds."""
        now = time.time()
        if self._subs_cache is not None and now - self._subs_ts < 5:
            return self._subs_cache
        conn = self._connect()
        try:
            pairs = []
            # Protected table
            rows = conn.execute("SELECT value, alias FROM protected").fetchall()
            for encrypted_val, alias in rows:
                try:
                    real_val = self._decrypt(encrypted_val)
                    if real_val:
                        pairs.append((real_val, alias))
                except Exception:
                    continue
            # Secrets table (backward compat — auto-alias)
            rows2 = conn.execute("SELECT name, value FROM secrets").fetchall()
            for name, encrypted_val in rows2:
                try:
                    real_val = self._decrypt(encrypted_val)
                    alias = f"__vault_{name}__"
                    if real_val:
                        pairs.append((real_val, alias))
                except Exception:
                    continue
            # Sort by length descending to avoid partial replacements
            pairs.sort(key=lambda p: len(p[0]), reverse=True)
            self._subs_cache = pairs
            self._subs_ts = now
            return pairs
        finally:
            conn.close()

    def mask(self, text: str) -> str:
        """Replace all protected values by their aliases."""
        if not text:
            return text
        for real_val, alias in self._load_substitutions():
            text = text.replace(real_val, alias)
        return text

    def unmask(self, text: str) -> str:
        """Replace all aliases by real values (for tool execution)."""
        if not text:
            return text
        for real_val, alias in self._load_substitutions():
            text = text.replace(alias, real_val)
        return text

    # ── Import from password managers ───────────────────────────────────────

    # Header signatures for auto-detection
    _HEADER_SIGNATURES = {
        "1password":  {"title", "username", "password", "url"},
        "lastpass":   {"url", "username", "password", "name", "grouping"},
        "bitwarden":  {"name", "login_username", "login_password", "login_uri"},
        "keepass":    {"title", "username", "password", "url"},
        "chrome":     {"name", "url", "username", "password"},
        "firefox":    {"url", "username", "password"},
        "apple":      {"title", "url", "username", "password"},
        "safari":     {"title", "url", "username", "password"},
    }

    # Column mapping: source -> (name_col, username_col, password_col, url_col, notes_col, category_col)
    _COL_MAP = {
        "1password":  ("title", "username", "password", "url", "notes", None),
        "lastpass":   ("name", "username", "password", "url", "notes", "grouping"),
        "bitwarden":  ("name", "login_username", "login_password", "login_uri", "notes", "folder"),
        "keepass":    ("title", "username", "password", "url", "notes", "group"),
        "chrome":     ("name", "username", "password", "url", None, None),
        "firefox":    (None, "username", "password", "url", None, None),
        "apple":      ("title", "username", "password", "url", "notes", None),
        "safari":     ("title", "username", "password", "url", None, None),
    }

    @staticmethod
    def _slugify(text: str, max_len: int = 50) -> str:
        """Convert text to vault-friendly slug: lowercase, no accents, underscores."""
        import unicodedata
        text = unicodedata.normalize("NFKD", text)
        text = "".join(c for c in text if not unicodedata.combining(c))
        text = text.lower().strip()
        text = re.sub(r"[^a-z0-9]+", "_", text)
        text = text.strip("_")
        return text[:max_len] if text else "unnamed"

    def _unique_name(self, base: str, used: set) -> str:
        """Generate unique vault name with _1, _2 suffix if needed."""
        name = base
        i = 1
        while name in used or self._name_exists_any(name):
            name = f"{base}_{i}"
            i += 1
        return name

    def _detect_source(self, headers: list[str]) -> str | None:
        """Auto-detect password manager from CSV headers."""
        lower_headers = {h.lower().strip() for h in headers}
        best_match = None
        best_score = 0
        for source, sig in self._HEADER_SIGNATURES.items():
            score = len(sig & lower_headers)
            if score > best_score:
                best_score = score
                best_match = source
        return best_match if best_score >= 2 else None

    def _parse_csv_row(self, row: dict, source: str) -> dict | None:
        """Parse a CSV row into a universal entry dict."""
        name_col, user_col, pw_col, url_col, notes_col, cat_col = self._COL_MAP.get(source, (None,) * 6)
        # Case-insensitive column lookup
        lower_row = {k.lower().strip(): v for k, v in row.items()}

        password = lower_row.get(pw_col.lower() if pw_col else "", "").strip() if pw_col else ""
        if not password:
            # Fallback: try "password" directly
            password = lower_row.get("password", "").strip()
        if not password:
            return None

        title = lower_row.get(name_col.lower() if name_col else "", "").strip() if name_col else ""
        if not title:
            title = lower_row.get("name", lower_row.get("title", "")).strip()
        username = lower_row.get(user_col.lower() if user_col else "", "").strip() if user_col else ""
        url = lower_row.get(url_col.lower() if url_col else "", "").strip() if url_col else ""
        notes = lower_row.get(notes_col.lower() if notes_col else "", "").strip() if notes_col else ""
        category = lower_row.get(cat_col.lower() if cat_col else "", "").strip() if cat_col else ""

        # Use URL domain as fallback name
        if not title and url:
            try:
                from urllib.parse import urlparse as _urlparse
                domain = _urlparse(url).netloc
                title = domain.split(".")[-2] if "." in domain else domain
            except Exception:
                title = url[:30]
        if not title:
            title = "unnamed"

        return {
            "name": title,
            "username": username,
            "password": password,
            "url": url,
            "notes": notes,
            "category": category or "imported",
        }

    def _parse_bitwarden_json(self, data: dict) -> list[dict]:
        """Parse Bitwarden JSON export into universal entries."""
        entries = []
        items = data.get("items", [])
        for item in items:
            login = item.get("login") or {}
            password = (login.get("password") or "").strip()
            if not password:
                continue
            uris = login.get("uris") or []
            url = uris[0].get("uri", "") if uris else ""
            entries.append({
                "name": item.get("name", "unnamed"),
                "username": (login.get("username") or "").strip(),
                "password": password,
                "url": url,
                "notes": (item.get("notes") or "").strip(),
                "category": (item.get("folderId") or "imported"),
            })
        return entries

    def import_file(self, filepath: str, source: str = "auto") -> dict:
        """Import a CSV/JSON password manager export into the vault.
        source: '1password', 'lastpass', 'bitwarden', 'keepass', 'chrome',
                'firefox', 'apple', 'safari', 'auto' (auto-detect).
        Returns {'imported': N, 'skipped': N, 'errors': []}.
        """
        import csv as _csv
        import json as _json

        result = {"imported": 0, "skipped": 0, "errors": []}
        entries = []

        try:
            ext = os.path.splitext(filepath)[1].lower()

            if ext == ".json":
                # JSON — likely Bitwarden
                with open(filepath, "r", encoding="utf-8") as f:
                    data = _json.load(f)
                if source == "auto":
                    source = "bitwarden"
                entries = self._parse_bitwarden_json(data)

            elif ext == ".csv":
                with open(filepath, "r", encoding="utf-8-sig") as f:
                    reader = _csv.DictReader(f)
                    if not reader.fieldnames:
                        result["errors"].append("Empty CSV or no headers")
                        return result
                    if source == "auto":
                        source = self._detect_source(reader.fieldnames) or "chrome"
                    for i, row in enumerate(reader):
                        parsed = self._parse_csv_row(row, source)
                        if parsed:
                            entries.append(parsed)
                        else:
                            result["skipped"] += 1
            else:
                result["errors"].append(f"Unsupported file extension: {ext}")
                return result
        except Exception as e:
            result["errors"].append(f"Parse error: {e}")
            return result

        # Import entries into vault
        used_names: set = set()
        for entry in entries:
            try:
                slug = self._slugify(entry["name"])
                name = self._unique_name(slug, used_names)
                used_names.add(name)

                # Store password as secret
                cat = entry["category"] if entry["category"] != "imported" else "imported"
                self.store(name, entry["password"], cat)

                # Also protect the password (VaultProxy)
                self.protect(name, entry["password"], kind="secret", category=cat)

                result["imported"] += 1
            except Exception as e:
                result["errors"].append(f"{entry['name']}: {e}")

        # Delete source file immediately (contains plaintext passwords)
        try:
            os.unlink(filepath)
            log.info("Deleted import file: %s", filepath)
        except OSError as e:
            log.warning("Could not delete import file %s: %s", filepath, e)

        log.info("Vault import complete: %d imported, %d skipped, %d errors",
                 result["imported"], result["skipped"], len(result["errors"]))
        return result

    # ── Auto-detection of secrets / PII ────────────────────────────────────

    @staticmethod
    def _extract_context(text: str, start: int, end: int, matched_value: str = "") -> list[str]:
        """Extract meaningful service/context keywords around a match.
        Excludes parts of the matched value itself to avoid leaking PII into names.
        Returns lowercase words, max 2 keywords."""
        # Grab ~60 chars before and after
        window_start = max(0, start - 60)
        window_end = min(len(text), end + 60)
        window = text[window_start:start] + " " + text[end:window_end]
        # Words from the matched value to exclude (avoid leaking PII)
        _value_words = set(re.findall(r'[a-zA-Z0-9]{2,}', matched_value.lower()))
        # Noise words (articles, pronouns, filler)
        _noise = {
            "le", "la", "les", "un", "une", "des", "de", "du", "mon", "ma",
            "mes", "ton", "ta", "tes", "son", "sa", "ses", "ce", "cette",
            "et", "ou", "en", "au", "aux", "est", "c'est", "cest", "voila",
            "voici", "chez", "pour", "sur", "dans", "avec", "que", "qui",
            "the", "a", "an", "is", "it", "my", "your", "for", "and", "of",
            "to", "on", "in", "at", "by", "from", "salut", "hello", "hi",
            "bonjour", "ok", "oui", "non", "yes", "no", "merci", "thanks",
            "acces", "accès", "compte", "account", "identifiant", "login",
            "voila", "c'est", "password", "mot", "passe", "mdp", "secret",
            "mail", "email", "e-mail", "courriel", "tel", "telephone",
            "mobile", "portable", "carte", "card", "token", "key", "cle", "clé",
            "ssh", "api", "code", "numero", "number",
        }
        words = re.findall(r'[a-zA-Z0-9._-]{2,}', window.lower())
        meaningful = [
            w for w in words
            if w not in _noise and w not in _value_words and len(w) >= 2
        ]
        # Deduplicate preserving order
        seen = set()
        unique = []
        for w in meaningful:
            if w not in seen:
                seen.add(w)
                unique.append(w)
        return unique[:2]

    def auto_detect(self, text: str) -> list[AutoDetected]:
        """Detect secrets/PII unknown to the vault — hardcoded + learned patterns."""
        found = []
        # 1. Hardcoded patterns
        for pattern, name, cat in _auto_compiled:
            for match in pattern.finditer(text):
                val = match.group(1) if match.lastindex else match.group(0)
                ctx = self._extract_context(text, match.start(), match.end(), val)
                found.append(AutoDetected(value=val, pattern_name=name, category=cat, context_words=ctx))
        # 2. Learned patterns from LLM
        for pattern, name, cat in self._load_learned_patterns():
            for match in pattern.finditer(text):
                val = match.group(0)
                ctx = self._extract_context(text, match.start(), match.end(), val)
                found.append(AutoDetected(value=val, pattern_name=name, category=cat, context_words=ctx))
                self.increment_pattern_hits(name)
        return found

    def auto_protect(self, text: str) -> tuple[str, list[str]]:
        """Detect + auto-protect. Returns (masked_text, protected_names)."""
        detected = self.auto_detect(text)
        if not detected:
            return text, []
        names = []
        for d in detected:
            # Build name: type_prefix + context keywords
            # e.g. "email_ionos", "password_smtp_ovh", "phone_whatsapp"
            # Simplify pattern_name to short type prefix
            _type_map = {
                "email_address": "email", "phone_number": "phone",
                "detected_password": "password", "detected_token": "token",
                "visa_card": "card_visa", "mastercard": "card_mc",
                "amex_card": "card_amex", "discover_card": "card_discover",
                "unionpay_card": "card_unionpay", "mir_card": "card_mir",
                "diners_card": "card_diners", "jcb_card": "card_jcb",
                "mastercard_2series": "card_mc", "cvv_code": "cvv",
                "card_expiry": "card_expiry", "iban": "iban",
                "swift_bic": "swift", "rib_france": "rib",
                "ssh_private_key": "ssh_key",
            }
            type_prefix = _type_map.get(d.pattern_name, d.pattern_name)
            ctx = d.context_words or []
            if ctx:
                base = type_prefix + "_" + "_".join(ctx)
            else:
                base = type_prefix
            # Sanitize: keep only alnum and underscore
            base = re.sub(r'[^a-z0-9_]', '_', base.lower()).strip('_')
            if not base:
                base = d.pattern_name
            name = base
            i = 1
            # Avoid name collisions
            while self._name_exists_any(name) or name in names:
                name = f"{base}_{i}"
                i += 1
            kind = "pii" if d.category == "pii" else "secret"
            self.protect(name, d.value, kind=kind, category=d.category)
            names.append(name)
        return self.mask(text), names

    def _name_exists_any(self, name: str) -> bool:
        """Check if name exists in secrets or protected tables."""
        conn = self._connect()
        try:
            r1 = conn.execute("SELECT 1 FROM secrets WHERE name = ?", (name,)).fetchone()
            if r1:
                return True
            r2 = conn.execute("SELECT 1 FROM protected WHERE name = ?", (name,)).fetchone()
            return r2 is not None
        except sqlite3.Error:
            return False
        finally:
            conn.close()

    # ── Learned patterns ───────────────────────────────────────────────────

    _learned_cache: list | None = None

    def learn_pattern(self, name: str, pattern: str, category: str = "") -> bool:
        """Register a new detection pattern learned from the LLM."""
        try:
            re.compile(pattern)
        except re.error:
            log.warning("Invalid learned pattern rejected: %s", pattern)
            return False
        now = datetime.now(timezone.utc).isoformat()
        conn = self._connect()
        try:
            conn.execute(
                """INSERT INTO learned_patterns (name, pattern, category, source, hits, created_at)
                   VALUES (?, ?, ?, 'llm', 0, ?)
                   ON CONFLICT(name) DO UPDATE SET
                       pattern=excluded.pattern, category=excluded.category""",
                (name, pattern, category, now))
            conn.commit()
            self._learned_cache = None
            return True
        finally:
            conn.close()

    def _load_learned_patterns(self) -> list[tuple]:
        """Load learned patterns, compiled. Cached in memory."""
        if self._learned_cache is not None:
            return self._learned_cache
        conn = self._connect()
        try:
            rows = conn.execute("SELECT name, pattern, category FROM learned_patterns").fetchall()
            compiled = []
            for name, pattern, cat in rows:
                try:
                    compiled.append((re.compile(pattern, re.IGNORECASE), name, cat or "api"))
                except re.error:
                    continue
            self._learned_cache = compiled
            return compiled
        finally:
            conn.close()

    def increment_pattern_hits(self, name: str):
        """Increment hit counter when a learned pattern detects a secret."""
        conn = self._connect()
        try:
            conn.execute("UPDATE learned_patterns SET hits = hits + 1 WHERE name = ?", (name,))
            conn.commit()
        except Exception:
            pass
        finally:
            conn.close()


# ── Auto-detect patterns (module-level, compiled once) ─────────────────────

@dataclass
class AutoDetected:
    value: str
    pattern_name: str
    category: str
    context_words: list = None  # keywords extracted from surrounding text


AUTO_DETECT_PATTERNS = [
    # ── API KEYS (distinctive prefixes) ──
    (r'sk-ant-api03-[A-Za-z0-9_-]{80,}', 'anthropic_api_key', 'llm'),
    (r'sk-[A-Za-z0-9]{32,}', 'openai_api_key', 'llm'),
    (r'sk-or-v1-[a-f0-9]{64}', 'openrouter_api_key', 'llm'),
    (r'ghp_[A-Za-z0-9]{36,}', 'github_pat', 'api'),
    (r'gho_[A-Za-z0-9]{36,}', 'github_oauth', 'api'),
    (r'glpat-[A-Za-z0-9_-]{20,}', 'gitlab_pat', 'api'),
    (r'xoxb-[0-9]{10,}-[0-9A-Za-z-]+', 'slack_bot_token', 'api'),
    (r'xoxp-[0-9]{10,}-[0-9A-Za-z-]+', 'slack_user_token', 'api'),
    (r'AKIA[0-9A-Z]{16}', 'aws_access_key', 'api'),
    (r'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----', 'ssh_private_key', 'ssh'),
    # ── CARDS ──
    (r'(?<!\d)4[0-9]{3}[\s-]?[0-9]{4}[\s-]?[0-9]{4}[\s-]?[0-9]{4}(?!\d)', 'visa_card', 'financial'),
    (r'(?<!\d)5[1-5][0-9]{2}[\s-]?[0-9]{4}[\s-]?[0-9]{4}[\s-]?[0-9]{4}(?!\d)', 'mastercard', 'financial'),
    (r'(?<!\d)2[2-7][0-9]{2}[\s-]?[0-9]{4}[\s-]?[0-9]{4}[\s-]?[0-9]{4}(?!\d)', 'mastercard_2series', 'financial'),
    (r'(?<!\d)3[47][0-9]{2}[\s-]?[0-9]{6}[\s-]?[0-9]{5}(?!\d)', 'amex_card', 'financial'),
    (r'(?<!\d)6(?:011|5[0-9]{2})[\s-]?[0-9]{4}[\s-]?[0-9]{4}[\s-]?[0-9]{4}(?!\d)', 'discover_card', 'financial'),
    (r'(?<!\d)3(?:0[0-5]|[68][0-9])[0-9][\s-]?[0-9]{4}[\s-]?[0-9]{4}[\s-]?[0-9]{2}(?!\d)', 'diners_card', 'financial'),
    (r'(?<!\d)(?:2131|1800|35[0-9]{3})[0-9]{11,12}(?!\d)', 'jcb_card', 'financial'),
    (r'(?<!\d)62[0-9]{2}[\s-]?[0-9]{4}[\s-]?[0-9]{4}[\s-]?[0-9]{2,6}(?!\d)', 'unionpay_card', 'financial'),
    (r'(?<!\d)220[0-4][\s-]?[0-9]{4}[\s-]?[0-9]{4}[\s-]?[0-9]{4}(?!\d)', 'mir_card', 'financial'),
    # ── BANKING ──
    (r'\b[A-Z]{2}[0-9]{2}[\s]?[A-Z0-9]{4}[\s]?(?:[A-Z0-9]{4}[\s]?){1,7}[A-Z0-9]{1,4}\b', 'iban', 'financial'),
    (r'(?:swift|bic|swift[\s/]?bic|code[\s_]?banque?)[\s:=]+([A-Z]{4}[A-Z]{2}[A-Z0-9]{2}(?:[A-Z0-9]{3})?)', 'swift_bic', 'financial'),
    (r'\b[0-9]{5}[\s-]?[0-9]{5}[\s-]?[0-9A-Z]{11}[\s-]?[0-9]{2}\b', 'rib_france', 'financial'),
    # ── USA ──
    (r'(?<!\d)[0-9]{3}-[0-9]{2}-[0-9]{4}(?!\d)', 'ssn_usa', 'pii'),
    # ── BRAZIL ──
    (r'(?<!\d)[0-9]{3}\.[0-9]{3}\.[0-9]{3}-[0-9]{2}(?!\d)', 'cpf_brazil', 'pii'),
    (r'(?<!\d)[0-9]{2}\.[0-9]{3}\.[0-9]{3}/[0-9]{4}-[0-9]{2}(?!\d)', 'cnpj_brazil', 'financial'),
    # ── INDIA ──
    (r'(?:pan|pan\s?card|pan\s?number)[\s:=]+[A-Z]{5}[0-9]{4}[A-Z]', 'pan_india', 'financial'),
    (r'(?<!\d)[2-9][0-9]{3}\s[0-9]{4}\s[0-9]{4}(?!\d)', 'aadhaar_india', 'pii'),
    # ── CHINA ──
    (r'(?<!\d)[1-9][0-9]{5}(?:19|20)[0-9]{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12][0-9]|3[01])[0-9]{3}[0-9X](?!\d)', 'national_id_china', 'pii'),
    # ── UK ──
    (r'\b[A-CEGHJ-PR-TW-Z]{2}\s?[0-9]{2}\s?[0-9]{2}\s?[0-9]{2}\s?[A-D]\b', 'nino_uk', 'pii'),
    # ── CANADA ──
    (r'(?<!\d)[0-9]{3}-[0-9]{3}-[0-9]{3}(?!\d)', 'sin_canada', 'pii'),
    # ── AUSTRALIA ──
    (r'(?<!\d)[0-9]{3}\s[0-9]{3}\s[0-9]{3}(?!\d)', 'tfn_australia', 'pii'),
    # ── MEXICO ──
    (r'\b[A-Z]{4}[0-9]{6}[HM][A-Z]{5}[A-Z0-9]{2}\b', 'curp_mexico', 'pii'),
    # ── SOUTH KOREA ──
    (r'(?<!\d)[0-9]{6}-[1-4][0-9]{6}(?!\d)', 'rrn_korea', 'pii'),
    # ── JAPAN ──
    (r'(?:マイナンバー|my\s?number)[\s:：]+[0-9]{4}[\s-]?[0-9]{4}[\s-]?[0-9]{4}', 'my_number_japan', 'pii'),
    # ── CARD METADATA (keyword context) ──
    (r'(?:cvv|cvc|cvv2|cvc2|csv)[\s:=]+[0-9]{3,4}\b', 'cvv_code', 'financial'),
    (r'(?:expir[ey]?|exp\.?|validit[eéy]|valide)[\s:=]+(?:0[1-9]|1[0-2])[/\-](?:[0-9]{2}|20[2-9][0-9])', 'card_expiry', 'financial'),
    # ── PASSWORDS (keyword context) ──
    (r'(?:mot de passe|password|mdp|passwd|pwd|pass)[\s:=]+(\S+)', 'detected_password', 'email'),
    (r'(?:secret|token|api[_ ]?key|clé|cle)[\s:=]+(\S+)', 'detected_token', 'api'),
    # ── CONTACT PII (keyword context) ──
    (r'\b[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}\b', 'email_address', 'pii'),
    (r'(?:tel|phone|mobile|portable|numero|whatsapp)[\s:=]+\+?[0-9][\s.-]?(?:[0-9][\s.-]?){7,14}', 'phone_number', 'pii'),
]

_auto_compiled = [(re.compile(p, re.IGNORECASE), n, c) for p, n, c in AUTO_DETECT_PATTERNS]
