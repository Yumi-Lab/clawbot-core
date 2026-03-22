# Architecture — Tool Sandboxing (Phase 4)

**Version : 1.0**
**Date : 2026-03-20**
**Projet : clawbot-core**

---

## 1. Vue d'ensemble

Le systeme de sandboxing ajoute une couche de permissions entre l'appel d'un tool par le LLM et son execution reelle. Chaque tool passe par un **permission check** qui decide : executer directement (ALLOW), demander l'approbation utilisateur (ASK), ou bloquer (DENY).

```
LLM tool_call
     │
     v
┌─────────────────────────────┐
│  SandboxManager.evaluate()  │
│  ┌────────────────────────┐ │
│  │ 1. ToolPolicy.check()  │ │  ← regles par defaut + plan
│  │ 2. ApprovalStore.get() │ │  ← decisions persistees
│  │ 3. Safe bins check     │ │  ← commandes stdin-only safe
│  │ 4. Arg-level analysis  │ │  ← rm -rf → force DENY
│  └────────────────────────┘ │
└──────────┬──────────────────┘
           │
     ┌─────┼─────┐
     v     v     v
   ALLOW  ASK   DENY
     │     │     │
     │     │     └→ {"error": "Tool denied: {reason}"}
     │     │
     │     └→ SSE tool_approval_request
     │        ← POST /v1/tool-approval (decision)
     │        ├→ allow → execute
     │        └→ deny  → {"error": "..."}
     │
     └→ _execute_tool() normal
```

---

## 2. Composants

### 2.1 ToolPermission (enum)

```python
class ToolPermission(Enum):
    ALLOW = "allow"   # execution immediate
    ASK   = "ask"     # approbation utilisateur requise
    DENY  = "deny"    # bloque, jamais execute
```

Hierarchie : DENY > ASK > ALLOW (le plus restrictif gagne).

### 2.2 ToolPolicy

Regles par defaut par tool, modulees par le plan utilisateur.

| Permission | Tools |
|-----------|-------|
| **ALLOW** | `system__read_file`, `files__read`, `files__list`, `web__search`, `system__get_system_info`, `system__disk`, `documents__*` |
| **ASK** | `system__bash`, `system__python`, `system__write_file`, `files__write`, `files__delete`, `files__move`, `git__commit`, `git__push`, `email__send` |
| **DENY** | `system__ssh` (sauf plan Pro) |

**Plan-based overrides :**
- Plan Free : `system__ssh` → DENY
- Plan Pro : `system__ssh` → ASK (pas ALLOW — toujours confirmation)
- Modules tiers : ASK par defaut (sauf si allowliste)

**Methode principale :**
```python
def check(tool_name: str, args: dict = None, plan: str = "free") -> ToolPermission
```

### 2.3 Safe Bins (sous-module de ToolPolicy)

Inspiré d'OpenClaw. Si une commande bash est un binaire "safe" utilise en mode stdin-only, elle est ALLOW sans approval meme si `system__bash` est ASK.

**Profils :**

| Binaire | max_positional | denied_flags |
|---------|---------------|-------------|
| grep | 1 (pattern only) | -r, -R, -f, --include, --exclude |
| head | 0 | tout sauf -n |
| tail | 0 | tout sauf -n |
| cut | 0 | — |
| sort | 0 | -o |
| uniq | 0 | — |
| tr | 2 | — |
| wc | 0 | — |
| jq | 1 (filter) | --argfile, -f, --rawfile |

**Validation :**
```python
def validate_safe_bin(argv: list[str]) -> bool
```
Parse argv, identifie le binaire, verifie le profil. Retourne `True` si la commande est safe.

### 2.4 Arg-level Analysis

Certaines combinaisons d'arguments forcent DENY meme si le tool est ASK :

| Tool | Pattern | Decision |
|------|---------|----------|
| `system__bash` | `rm -rf /` ou `rm -rf /*` | DENY |
| `system__bash` | `chmod 777 /` | DENY |
| `system__bash` | `dd if=... of=/dev/` | DENY |
| `system__bash` | inline shell (`bash -c "..."`) | ASK (jamais allow-always) |
| `system__write_file` | path dans _PROTECTED_PATHS | DENY |
| `files__delete` | path dans _PROTECTED_PATHS | DENY |

**Shell unwrapping :** `bash -c "rm -rf /"` → analyse `rm -rf /` → DENY.

### 2.5 Env Sanitization

Avant toute execution en sandbox, les variables d'environnement dangereuses sont nettoyees :

**Variables bloquees :**
- `PATH`, `LD_PRELOAD`, `LD_LIBRARY_PATH`
- `SSH_AUTH_SOCK`, `KUBECONFIG`, `GIT_ASKPASS`

**Prefixes bloques :**
- `ANSIBLE_*`, `KUBE_*`, `AWS_*`, `GCP_*`, `AZURE_*`

```python
def sanitize_exec_env(overrides: dict = None) -> dict
```

### 2.6 ApprovalStore

Fichier JSON persistant : `/home/pi/.openjarvis/approvals.json`

**Format :**
```json
{
  "version": 1,
  "tools": {
    "system__bash": {
      "permission": "allow",
      "pattern": null,
      "expires": null,
      "last_used_at": "2026-03-20T14:30:00",
      "last_command": "ls -la /home/pi"
    }
  },
  "session_approvals": {
    "sess_abc123": {
      "system__bash": {
        "permission": "allow",
        "granted_at": "2026-03-20T14:30:00"
      }
    }
  }
}
```

**Methodes :**
- `get(tool_name, session_id=None) -> ToolPermission | None`
- `set(tool_name, permission, remember="session"|"always"|"never", session_id=None)`
- `is_approved(tool_name, args, session_id) -> bool`
- `clear_session(session_id)` — nettoie les approvals de session
- `load()` / `save()` — lecture/ecriture fichier avec file lock

**Thread safety :** `threading.Lock()` pour les operations read/write.

### 2.7 SandboxManager (facade)

Singleton qui combine ToolPolicy + ApprovalStore.

```python
class SandboxManager:
    _instance = None

    def evaluate(self, tool_name: str, args: dict, session_id: str,
                 plan: str = "free") -> tuple[ToolPermission, str]:
        """
        Returns (permission, reason).

        Flow:
        1. policy.check(tool_name, args, plan) → base permission
        2. Si DENY → return (DENY, reason) immediatement
        3. Si ALLOW → return (ALLOW, "")
        4. Si ASK → check approval_store.is_approved(tool_name, args, session_id)
           - Si approuve → return (ALLOW, "pre-approved")
           - Sinon → return (ASK, reason)
        """
```

---

## 3. Integration dans orchestrator.py

### 3.1 Points d'injection

```python
# orchestrator.py — _execute_tool() (line ~2774)

from clawbot_core.sandbox.manager import SandboxManager

def _execute_tool(tool_name, arguments_raw, user_model=None, agent_id=None):
    args = json.loads(arguments_raw) if arguments_raw else {}

    # === SANDBOX CHECK (nouveau) ===
    sandbox = SandboxManager.get_instance()
    permission, reason = sandbox.evaluate(tool_name, args, _current_session_id)

    if permission == ToolPermission.DENY:
        return json.dumps({"error": f"Tool denied: {reason}"})

    if permission == ToolPermission.ASK:
        # Signal au streaming loop qu'on attend une approbation
        raise ToolApprovalRequired(tool_name, args, reason)

    # permission == ALLOW → continue normalement
    # ... dispatch existant ...
```

### 3.2 Streaming loop (chat_with_tools_stream)

```python
# Dans la boucle tool execution de chat_with_tools_stream()

try:
    result = _execute_tool(tool_name, arguments_raw, ...)
except ToolApprovalRequired as e:
    # Emettre SSE event
    yield {"type": "tool_approval_request", "content": {
        "call_id": tool_call_id,
        "tool": e.tool_name,
        "args": e.args,
        "reason": e.reason
    }}
    # Attendre decision (blocking avec timeout)
    decision = _wait_for_approval(tool_call_id, timeout=120)
    if decision == "allow":
        result = _execute_tool_bypass(tool_name, arguments_raw, ...)
    else:
        result = json.dumps({"error": "Tool refused by user"})
        yield {"type": "tool_denied", "content": {"call_id": tool_call_id}}
```

### 3.3 Endpoint d'approbation

```
POST /v1/tool-approval
Content-Type: application/json

{
    "call_id": "call_abc123",
    "decision": "allow" | "deny",
    "remember": "session" | "always" | "never"
}

Response: 200 {"status": "ok"}
```

**Mecanisme d'attente :** `threading.Event()` par call_id, stocke dans un dict global `_pending_approvals`. Le streaming loop fait `event.wait(timeout=120)`. L'endpoint POST fait `event.set()`.

### 3.4 Nouveaux SSE events

| Event | Data | Quand |
|-------|------|-------|
| `tool_approval_request` | `{"call_id", "tool", "args", "reason"}` | Tool ASK, en attente |
| `tool_denied` | `{"call_id", "tool", "reason"}` | Tool refuse (par policy ou user) |

### 3.5 Migration _is_dangerous_command

L'existant `_is_dangerous_command()` (line 1313) reste en place comme **fallback**. Le sandbox ajoute une couche au-dessus. Order :

1. SandboxManager.evaluate() — nouvelle couche
2. _is_dangerous_command() — toujours appele dans _execute_builtin() comme filet de securite

A terme (Phase future), `_is_dangerous_command()` sera absorbe dans ToolPolicy.

---

## 4. Dashboard — Approval UI

### 4.1 SSE handler (index.html)

Nouveau case dans le handler SSE existant (~line 3816) :

```javascript
case 'tool_approval_request':
    showToolApproval(parsed);
    break;
case 'tool_denied':
    showToolDenied(parsed);
    break;
```

### 4.2 Inline approval dans le timeline

```
┌──────────────────────────────────────────┐
│ ⊙ system__bash                    pending│
│   Command: rm -rf /tmp/old               │
│   Reason: bash commands require approval │
│                                          │
│   [Autoriser] [Refuser] [Toujours ✓]    │
└──────────────────────────────────────────┘
```

- **Autoriser** (vert) → POST decision=allow, remember=session
- **Refuser** (rouge) → POST decision=deny, remember=never
- **Toujours** (bleu) → POST decision=allow, remember=always

### 4.3 Post-decision

- Approve → icon `✓` vert, row reprend le style normal
- Deny → icon `✗` rouge, message "Tool refused by user"
- Transition CSS 0.3s

---

## 5. Fichiers concernes

### Nouveau module sandbox

| Fichier | Contenu | Lignes estimees |
|---------|---------|----------------|
| `clawbot_core/sandbox/__init__.py` | Exports | ~15 |
| `clawbot_core/sandbox/permissions.py` | ToolPermission, ToolPolicy, safe bins, env sanitize | ~150 |
| `clawbot_core/sandbox/approvals.py` | ApprovalStore | ~80 |
| `clawbot_core/sandbox/manager.py` | SandboxManager facade | ~60 |

### Modifications existantes

| Fichier | Modification |
|---------|-------------|
| `clawbot_core/orchestrator.py` | Import sandbox, inject dans _execute_tool, ajout ToolApprovalRequired, _wait_for_approval |
| `clawbot_core/main.py` | Route POST /v1/tool-approval |
| `index.html` (WebUI) | SSE handler + showToolApproval() + CSS |

### Tests

| Fichier | Contenu |
|---------|---------|
| `tests/test_sandbox.py` | Unitaires permissions, approvals, manager |
| `tests/test_sandbox_integration.py` | Integration orchestrator |

---

## 6. Contraintes respectees

- **stdlib-only** : aucune dependance externe (json, enum, threading, uuid, os, pathlib)
- **ARM H3 1GB** : pas de Docker, pas de process lourd, overhead < 1ms par check
- **Thread-safe** : Lock sur ApprovalStore, Event pour approval wait
- **Backward-compatible** : `_is_dangerous_command()` reste en fallback
- **Single device** : pas de multi-agent policies, pas de channel routing

---

## 7. Matrice Tools x Plans

| Tool | Free | Perso | Pro |
|------|------|-------|-----|
| `system__read_file` | ALLOW | ALLOW | ALLOW |
| `files__read` | ALLOW | ALLOW | ALLOW |
| `files__list` | ALLOW | ALLOW | ALLOW |
| `web__search` | ALLOW | ALLOW | ALLOW |
| `documents__*` | ALLOW | ALLOW | ALLOW |
| `system__bash` | ASK | ASK | ASK |
| `system__python` | ASK | ASK | ASK |
| `system__write_file` | ASK | ASK | ASK |
| `files__write` | ASK | ASK | ASK |
| `files__delete` | ASK | ASK | ASK |
| `git__commit` | ASK | ASK | ASK |
| `git__push` | ASK | ASK | ASK |
| `email__send` | DENY | ASK | ASK |
| `system__ssh` | DENY | DENY | ASK |
| `exec__*` | DENY | ASK | ASK |
| Modules tiers | ASK | ASK | ASK |
