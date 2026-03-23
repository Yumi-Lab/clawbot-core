"""
ClawbotCore — Tool Registry

Centralized definitions for all built-in tools, agent memory tools,
and the dispatch table that routes tool calls to their handlers.

Tool naming convention:  {module_id}__{tool_suffix}  (double underscore)
Built-in modules:        system, files, documents, web, exec, email, git, vault, memory
Aliases:                 agents → system, scheduler → system, network → web

External module response contract (HTTP JSON):
    Success: {"result": "..."} or {"output": "..."}
    Error:   {"error": "..."}
    Plain text is also accepted as a fallback.
"""

# ── SYSTEM tools ────────────────────────────────────────────────────────────────

_SYSTEM_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "system__bash",
            "description": "Execute a bash shell command on the Pi and return stdout + stderr. Use for system tasks, file operations, service management, etc.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "The bash command to execute"},
                    "timeout": {"type": "integer", "description": "Timeout in seconds (default 30)", "default": 30},
                },
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "system__python",
            "description": "Execute a Python script on the Pi and return its output. Write the full script content, it will be saved to a temp file and run.",
            "parameters": {
                "type": "object",
                "properties": {
                    "code": {"type": "string", "description": "Complete Python script to execute"},
                    "timeout": {"type": "integer", "description": "Timeout in seconds (default 30)", "default": 30},
                },
                "required": ["code"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "system__write_file",
            "description": "Write content to a file on the Pi filesystem.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Absolute path of the file to write"},
                    "content": {"type": "string", "description": "Content to write"},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "system__read_file",
            "description": "Read the content of a file on the Pi filesystem.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Absolute path of the file to read"},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "system__ssh",
            "description": "Execute a command on a remote server via SSH with password authentication. Use for server audits, remote administration, checking services on external hosts. sshpass must be installed on the Pi.",
            "parameters": {
                "type": "object",
                "properties": {
                    "host": {"type": "string", "description": "Remote hostname or IP address"},
                    "user": {"type": "string", "description": "SSH username"},
                    "password": {"type": "string", "description": "SSH password"},
                    "command": {"type": "string", "description": "Shell command to execute on the remote host"},
                    "port": {"type": "integer", "description": "SSH port (default 22)", "default": 22},
                    "timeout": {"type": "integer", "description": "Timeout in seconds (default 30)", "default": 30},
                },
                "required": ["host", "user", "password", "command"],
            },
        },
    },
    # Web search tools are injected dynamically via _build_web_search_tools() based on search_mode
    # _TOOL_WEB_SEARCH, _TOOL_WEB_SEARCH_KIMI, _TOOL_WEB_SEARCH_CLAUDE defined in orchestrator.py
    {
        "type": "function",
        "function": {
            "name": "system__search_engines_list",
            "description": "List all search engines in the pool with their reliability scores and supported regions. Use to audit the search engine pool.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "system__search_engine_add",
            "description": "Add a new search engine to the pool. Provide name, URL template (use {query} placeholder), regions, language, and optional headers/patterns. After adding, use system__search_engine_test to validate.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Unique engine identifier (e.g. 'google', 'yandex', 'naver')"},
                    "url": {"type": "string", "description": "Search URL with {query} placeholder (e.g. https://www.google.com/search?q={query})"},
                    "regions": {"type": "array", "items": {"type": "string"}, "description": "Country codes where this engine works (e.g. ['FR','US'] or ['OTHER'] for global)"},
                    "language": {"type": "string", "description": "Primary language: 'any', 'zh', 'fr', 'en', 'ru', 'ja', 'ko'"},
                    "headers": {"type": "object", "description": "HTTP headers (User-Agent, Accept-Language, etc.)"},
                    "patterns": {"type": "object", "description": "Regex patterns — if unknown, leave empty and run test to auto-generate"},
                },
                "required": ["name", "url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "system__search_engine_test",
            "description": "Test a search engine and auto-fix its patterns via AI if it returns 0 results. Updates reliability score. Run after adding a new engine.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Engine name to test"},
                    "query": {"type": "string", "description": "Test query (default: 'test search engine')"},
                },
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "system__spawn_agents",
            "description": (
                "Run multiple sub-agents in parallel on independent tasks and collect their results. "
                "Use when a complex request can be decomposed into independent subtasks handled by different specialists. "
                "Each agent works autonomously — do NOT use for sequential tasks that depend on each other. "
                "Returns the combined output of all agents."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "tasks": {
                        "type": "array",
                        "description": "List of independent tasks to run in parallel",
                        "items": {
                            "type": "object",
                            "properties": {
                                "agent": {"type": "string", "description": "Agent ID to assign this task to"},
                                "task": {"type": "string", "description": "Full task description for this agent — be specific and self-contained"},
                            },
                            "required": ["agent", "task"],
                        },
                    },
                    "timeout": {"type": "integer", "description": "Max seconds to wait per agent (default 120)", "default": 120},
                },
                "required": ["tasks"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "system__handoff",
            "description": (
                "Transfer a specific sub-task to a specialist agent. Use ONLY when the task requires "
                "expertise outside your specialization. Write a COMPACT, self-contained brief — "
                "the target agent has NO access to the conversation history. "
                "Rules: (1) context = only what the target agent strictly needs to know, as short as possible. "
                "(2) task = precise actionable instruction. (3) expected_output = exact format you need back. "
                "Do NOT use for tasks you can handle yourself."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "agent_id": {"type": "string", "description": "ID of the target agent (e.g. 'julien', 'marc', 'sophie', 'thierry')"},
                    "task": {"type": "string", "description": "Precise, actionable instruction — self-contained, no assumed context"},
                    "context": {"type": "string", "description": "Minimal background the agent needs. Summarize only what is strictly necessary."},
                    "expected_output": {"type": "string", "description": "Exact format or content you need back (e.g. 'the file content as text', 'a list of IPs', 'a working Python function')"},
                },
                "required": ["agent_id", "task"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "system__schedule_task",
            "description": (
                "Schedule a task to run automatically at a specified time or recurrence. "
                "Use when the user asks to schedule, automate, plan, or set a reminder for a recurring or one-time action. "
                "Supports: once (exact datetime), daily (every day at HH:MM), weekly (day + time), "
                "hourly (every hour at :MM), interval (every N minutes)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Short human-readable name for the task"},
                    "instruction": {"type": "string", "description": "Full instruction to execute when the task runs (as if the user typed it)"},
                    "schedule_type": {
                        "type": "string",
                        "enum": ["once", "daily", "weekly", "hourly", "interval"],
                        "description": "Type of schedule",
                    },
                    "datetime": {"type": "string", "description": "ISO 8601 datetime for 'once' type (e.g. '2025-12-25T09:00:00')"},
                    "time": {"type": "string", "description": "Time HH:MM for 'daily' or 'weekly' (e.g. '09:30')"},
                    "day_of_week": {"type": "string", "description": "Day of week for 'weekly' in English or French (e.g. 'monday', 'lundi')"},
                    "minute": {"type": "integer", "description": "Minute of the hour for 'hourly' (0-59)"},
                    "interval_minutes": {"type": "integer", "description": "Interval in minutes for 'interval' type"},
                },
                "required": ["name", "instruction", "schedule_type"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "system__list_tasks",
            "description": "List all scheduled tasks with their status, next run time, and recent execution history.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "system__cancel_task",
            "description": "Cancel (delete) or pause a scheduled task by its ID.",
            "parameters": {
                "type": "object",
                "properties": {
                    "task_id": {"type": "string", "description": "The task ID to cancel (8-char string)"},
                    "action": {
                        "type": "string",
                        "enum": ["delete", "pause"],
                        "description": "delete = permanent removal, pause = temporary suspension. Default: delete",
                        "default": "delete",
                    },
                },
                "required": ["task_id"],
            },
        },
    },
    # ── SYSTEM extended ─────────────────────────────────────────────────────────
    {"type": "function", "function": {"name": "system__get_system_info", "description": "Get system information about the Pi: CPU usage, RAM, disk, temperature, hostname, IP addresses.", "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {"name": "system__ssh_execute", "description": "Execute a command on a remote server via SSH with password authentication.", "parameters": {"type": "object", "properties": {"host": {"type": "string"}, "user": {"type": "string"}, "password": {"type": "string"}, "command": {"type": "string"}, "port": {"type": "integer", "default": 22}, "timeout": {"type": "integer", "default": 30}}, "required": ["host", "user", "password", "command"]}}},
    {"type": "function", "function": {"name": "system__disk", "description": "Get disk usage for all mounted filesystems on the Pi (df -h output).", "parameters": {"type": "object", "properties": {"path": {"type": "string", "description": "Optional specific path to check (default: '/')", "default": "/"}}}}},
]

# ── FILES tools ─────────────────────────────────────────────────────────────────

_FILES_TOOLS = [
    {"type": "function", "function": {"name": "files__read", "description": "Read the content of a file on the Pi filesystem.", "parameters": {"type": "object", "properties": {"path": {"type": "string", "description": "Absolute path of the file to read"}}, "required": ["path"]}}},
    {"type": "function", "function": {"name": "files__write", "description": "Write content to a file atomically (write .tmp then rename). Creates parent directories if needed.", "parameters": {"type": "object", "properties": {"path": {"type": "string", "description": "Absolute path of the file to write"}, "content": {"type": "string", "description": "Content to write"}, "force": {"type": "boolean", "description": "Allow writing to system paths like /etc/ (default false)", "default": False}}, "required": ["path", "content"]}}},
    {"type": "function", "function": {"name": "files__list", "description": "List files and directories at a given path with size and modification time.", "parameters": {"type": "object", "properties": {"path": {"type": "string", "description": "Directory path to list"}, "recursive": {"type": "boolean", "description": "List recursively (default false)", "default": False}}, "required": ["path"]}}},
    {"type": "function", "function": {"name": "files__move", "description": "Move or rename a file or directory.", "parameters": {"type": "object", "properties": {"src": {"type": "string", "description": "Source path"}, "dst": {"type": "string", "description": "Destination path"}}, "required": ["src", "dst"]}}},
    {"type": "function", "function": {"name": "files__delete", "description": "Delete a file or empty directory.", "parameters": {"type": "object", "properties": {"path": {"type": "string", "description": "Path to delete"}, "recursive": {"type": "boolean", "description": "Delete directory recursively (default false)", "default": False}}, "required": ["path"]}}},
    {"type": "function", "function": {"name": "files__dir_create", "description": "Create a directory and all parent directories.", "parameters": {"type": "object", "properties": {"path": {"type": "string", "description": "Directory path to create"}}, "required": ["path"]}}},
    {"type": "function", "function": {"name": "files__mkdir", "description": "Create a directory and all parent directories (alias for files__dir_create).", "parameters": {"type": "object", "properties": {"path": {"type": "string", "description": "Directory path to create"}}, "required": ["path"]}}},
]

# ── DOCUMENTS tools ─────────────────────────────────────────────────────────────

_DOCUMENTS_TOOLS = [
    {"type": "function", "function": {"name": "documents__pdf_to_text", "description": "Extract plain text from a PDF file using pdftotext (poppler).", "parameters": {"type": "object", "properties": {"path": {"type": "string", "description": "Absolute path to the PDF file"}}, "required": ["path"]}}},
    {"type": "function", "function": {"name": "documents__csv_parse", "description": "Parse a CSV file and return its content as a JSON array of objects.", "parameters": {"type": "object", "properties": {"path": {"type": "string", "description": "Absolute path to the CSV file"}, "delimiter": {"type": "string", "description": "Field delimiter (default ',')", "default": ","}, "max_rows": {"type": "integer", "description": "Maximum rows to return (default 100)", "default": 100}}, "required": ["path"]}}},
    {"type": "function", "function": {"name": "documents__csv_write", "description": "Write a JSON array of objects to a CSV file.", "parameters": {"type": "object", "properties": {"path": {"type": "string", "description": "Absolute path to write the CSV file"}, "data": {"type": "array", "description": "Array of objects to write as CSV rows", "items": {"type": "object"}}, "delimiter": {"type": "string", "description": "Field delimiter (default ',')", "default": ","}}, "required": ["path", "data"]}}},
]

# ── WEB tools ───────────────────────────────────────────────────────────────────

_WEB_TOOLS = [
    {"type": "function", "function": {"name": "web__search", "description": "Search the web using Bing/DuckDuckGo and return top results with titles, URLs, and snippets.", "parameters": {"type": "object", "properties": {"query": {"type": "string", "description": "Search query"}, "max_results": {"type": "integer", "description": "Max results (default 5, max 10)", "default": 5}}, "required": ["query"]}}},
    {"type": "function", "function": {"name": "web__http_get", "description": "Perform an HTTP GET request and return the response body.", "parameters": {"type": "object", "properties": {"url": {"type": "string", "description": "Target URL"}, "headers": {"type": "object", "description": "Optional HTTP headers as key-value pairs"}, "timeout": {"type": "integer", "description": "Timeout in seconds (default 30)", "default": 30}}, "required": ["url"]}}},
    {"type": "function", "function": {"name": "web__http_post", "description": "Perform an HTTP POST request with a JSON body and return the response.", "parameters": {"type": "object", "properties": {"url": {"type": "string", "description": "Target URL"}, "body": {"type": "object", "description": "JSON body to send"}, "headers": {"type": "object", "description": "Optional HTTP headers"}, "timeout": {"type": "integer", "description": "Timeout in seconds (default 30)", "default": 30}}, "required": ["url", "body"]}}},
    {"type": "function", "function": {"name": "web__file_download", "description": "Download a file from a URL and save it to the Pi filesystem.", "parameters": {"type": "object", "properties": {"url": {"type": "string", "description": "URL to download"}, "dest": {"type": "string", "description": "Absolute destination path on the Pi"}, "timeout": {"type": "integer", "description": "Timeout in seconds (default 60)", "default": 60}}, "required": ["url", "dest"]}}},
]

# ── EXEC tools ──────────────────────────────────────────────────────────────────

_EXEC_TOOLS = [
    {"type": "function", "function": {"name": "exec__run_python", "description": "Execute a Python script in a sandboxed environment. Working directory: /tmp/clawbot-agent/. Use for agent-authored scripts that should not run as root.", "parameters": {"type": "object", "properties": {"code": {"type": "string", "description": "Complete Python script to execute"}, "timeout": {"type": "integer", "description": "Timeout in seconds (default 60)", "default": 60}}, "required": ["code"]}}},
    {"type": "function", "function": {"name": "exec__run_bash", "description": "Execute a bash script in a sandboxed environment. Working directory: /tmp/clawbot-agent/. Use for agent-authored scripts that should not run as root.", "parameters": {"type": "object", "properties": {"script": {"type": "string", "description": "Bash script to execute"}, "timeout": {"type": "integer", "description": "Timeout in seconds (default 60)", "default": 60}}, "required": ["script"]}}},
]

# ── EMAIL tools ─────────────────────────────────────────────────────────────────

_EMAIL_TOOLS = [
    {"type": "function", "function": {"name": "email__send", "description": "Send an email via SMTP. Config must be set in /home/pi/.openjarvis/email.json (smtp_host, smtp_port, user, password, from_name).", "parameters": {"type": "object", "properties": {"to": {"type": "string", "description": "Recipient email address"}, "subject": {"type": "string", "description": "Email subject"}, "body": {"type": "string", "description": "Email body (plain text)"}, "cc": {"type": "string", "description": "Optional CC address"}}, "required": ["to", "subject", "body"]}}},
]

# ── GIT tools ───────────────────────────────────────────────────────────────────

_GIT_TOOLS = [
    {"type": "function", "function": {"name": "git__status", "description": "Get the git status of a repository: current branch, staged and unstaged changes.", "parameters": {"type": "object", "properties": {"repo_path": {"type": "string", "description": "Absolute path to the git repository"}}, "required": ["repo_path"]}}},
    {"type": "function", "function": {"name": "git__commit", "description": "Stage all changes (git add -A) and create a commit in a git repository.", "parameters": {"type": "object", "properties": {"repo_path": {"type": "string", "description": "Absolute path to the git repository"}, "message": {"type": "string", "description": "Commit message"}}, "required": ["repo_path", "message"]}}},
    {"type": "function", "function": {"name": "git__push", "description": "Push commits to the remote repository. Optionally set remote and branch.", "parameters": {"type": "object", "properties": {"repo_path": {"type": "string", "description": "Absolute path to the git repository"}, "remote": {"type": "string", "description": "Remote name (default 'origin')", "default": "origin"}, "branch": {"type": "string", "description": "Branch name (default: current branch)"}}, "required": ["repo_path"]}}},
    {"type": "function", "function": {"name": "git__pull", "description": "Pull latest changes from remote into a git repository.", "parameters": {"type": "object", "properties": {"repo_path": {"type": "string", "description": "Absolute path to the git repository"}, "remote": {"type": "string", "description": "Remote name (default 'origin')", "default": "origin"}, "branch": {"type": "string", "description": "Branch to pull (default: current branch)"}}, "required": ["repo_path"]}}},
    {"type": "function", "function": {"name": "git__log", "description": "Show recent commit history of a git repository.", "parameters": {"type": "object", "properties": {"repo_path": {"type": "string", "description": "Absolute path to the git repository"}, "n": {"type": "integer", "description": "Number of commits to show (default 10)", "default": 10}}, "required": ["repo_path"]}}},
]

# ── AGENTS tools (aliases) ──────────────────────────────────────────────────────

_AGENTS_TOOLS = [
    {"type": "function", "function": {"name": "agents__delegate", "description": "Delegate a sub-task to a specialist agent (alias for system__handoff). Use when a task requires expertise outside your specialization.", "parameters": {"type": "object", "properties": {"agent_id": {"type": "string", "description": "ID of the target agent"}, "task": {"type": "string", "description": "Precise, actionable instruction — self-contained"}, "context": {"type": "string", "description": "Minimal background the agent needs"}, "expected_output": {"type": "string", "description": "Exact format or content you need back"}}, "required": ["agent_id", "task"]}}},
]

# ── SCHEDULER tools (aliases) ───────────────────────────────────────────────────

_SCHEDULER_TOOLS = [
    {"type": "function", "function": {"name": "scheduler__create", "description": "Schedule a task to run automatically at a specified time or recurrence (alias for system__schedule_task).", "parameters": {"type": "object", "properties": {"name": {"type": "string", "description": "Short human-readable name for the task"}, "instruction": {"type": "string", "description": "Full instruction to execute when the task runs"}, "schedule_type": {"type": "string", "enum": ["once", "daily", "weekly", "hourly", "interval"]}, "datetime": {"type": "string", "description": "ISO 8601 datetime for 'once' type"}, "time": {"type": "string", "description": "Time HH:MM for 'daily' or 'weekly'"}, "day_of_week": {"type": "string", "description": "Day of week for 'weekly'"}, "minute": {"type": "integer", "description": "Minute of the hour for 'hourly'"}, "interval_minutes": {"type": "integer", "description": "Interval in minutes for 'interval' type"}}, "required": ["name", "instruction", "schedule_type"]}}},
    {"type": "function", "function": {"name": "scheduler__list", "description": "List all scheduled tasks with their status and next run time.", "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {"name": "scheduler__cancel", "description": "Cancel (delete) or pause a scheduled task by its ID.", "parameters": {"type": "object", "properties": {"task_id": {"type": "string", "description": "Task ID to cancel"}, "action": {"type": "string", "enum": ["delete", "pause"], "default": "delete"}}, "required": ["task_id"]}}},
]

# ── VAULT tools ─────────────────────────────────────────────────────────────────

_VAULT_TOOLS = [
    {"type": "function", "function": {"name": "vault__store", "description": "Store a secret (API key, password, token) securely in the encrypted vault. Use this instead of writing credentials to config files.", "parameters": {"type": "object", "properties": {"name": {"type": "string", "description": "Unique identifier for the secret (e.g. 'ionos_smtp', 'anthropic_api')"}, "value": {"type": "string", "description": "The secret value to store (will be encrypted at rest)"}, "username": {"type": "string", "description": "Optional username/login associated with this secret (e.g. 'nicolas@3d-expert.fr')", "default": ""}, "category": {"type": "string", "description": "Optional category: 'llm', 'email', 'ssh', 'api', 'oauth', 'other'", "default": "other"}, "note": {"type": "string", "description": "Optional note (e.g. 'IONOS SMTP server smtp.ionos.com port 587')", "default": ""}}, "required": ["name", "value"]}}},
    {"type": "function", "function": {"name": "vault__get", "description": "Retrieve a secret from the encrypted vault by name. Returns the decrypted value.", "parameters": {"type": "object", "properties": {"name": {"type": "string", "description": "The name of the secret to retrieve"}}, "required": ["name"]}}},
    {"type": "function", "function": {"name": "vault__list", "description": "List all stored secrets by name and category. Does NOT reveal secret values for security.", "parameters": {"type": "object", "properties": {"category": {"type": "string", "description": "Filter by category (optional). Omit to list all."}}}}},
    {"type": "function", "function": {"name": "vault__delete", "description": "Delete a secret from the vault by name.", "parameters": {"type": "object", "properties": {"name": {"type": "string", "description": "The name of the secret to delete"}}, "required": ["name"]}}},
    {"type": "function", "function": {"name": "vault__flag_secret", "description": "Flag a secret in the conversation that is not yet protected. Call this when you see a raw password, API key, or credential. The system will protect, mask it, and learn the pattern for future detection. IMPORTANT: Always provide a pattern_hint regex if the secret has a recognizable format.", "parameters": {"type": "object", "properties": {"value": {"type": "string", "description": "The exact secret value"}, "suggested_name": {"type": "string", "description": "Name for the secret (e.g. 'replicate_api_key')"}, "category": {"type": "string", "description": "llm/email/ssh/api/other", "default": "other"}, "pattern_hint": {"type": "string", "description": "Regex pattern for this type of secret (e.g. 'r8_[a-zA-Z0-9]{30,}' for Replicate keys). Omit for arbitrary passwords with no recognizable format."}}, "required": ["value", "suggested_name"]}}},
    {"type": "function", "function": {"name": "vault__protect_pii", "description": "Protect personal data (name, address, phone...) from being sent to AI. The value will be replaced by an alias in all future messages.", "parameters": {"type": "object", "properties": {"name": {"type": "string", "description": "Identifier (e.g. 'my_name', 'home_address')"}, "value": {"type": "string", "description": "The personal data to protect"}, "category": {"type": "string", "description": "name/address/phone/email/other", "default": "other"}}, "required": ["name", "value"]}}},
    {"type": "function", "function": {"name": "vault__search", "description": "Search vault secrets by keyword. Matches against name, username, category and note. Use this FIRST to find the right credential before vault__get. Returns matching entries without values.", "parameters": {"type": "object", "properties": {"query": {"type": "string", "description": "Search keyword (e.g. 'ionos', 'smtp', 'nicolas', 'ssh')"}}, "required": ["query"]}}},
]

# ── VAULT TOTP tools ────────────────────────────────────────────────────────────

_VAULT_TOTP_TOOLS = [
    {"type": "function", "function": {"name": "vault__totp_add", "description": "Store a TOTP/2FA secret for generating time-based codes. Accepts a base32 secret or an otpauth:// URI (from QR code).", "parameters": {"type": "object", "properties": {"name": {"type": "string", "description": "Label for this TOTP entry (e.g. 'GitHub', 'AWS')"}, "secret": {"type": "string", "description": "Base32 secret OR otpauth:// URI"}, "issuer": {"type": "string", "description": "Service name (optional, extracted from URI if available)"}}, "required": ["name", "secret"]}}},
    {"type": "function", "function": {"name": "vault__totp_code", "description": "Generate the current TOTP 6-digit code for a stored entry. Code changes every 30 seconds.", "parameters": {"type": "object", "properties": {"name": {"type": "string", "description": "TOTP entry name"}}, "required": ["name"]}}},
    {"type": "function", "function": {"name": "vault__totp_list", "description": "List all stored TOTP entries (names and issuers only, no secrets).", "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {"name": "vault__totp_delete", "description": "Delete a stored TOTP entry.", "parameters": {"type": "object", "properties": {"name": {"type": "string", "description": "TOTP entry name to delete"}}, "required": ["name"]}}},
    {"type": "function", "function": {"name": "vault__totp_search", "description": "Search TOTP entries by keyword. Fuzzy matches on name and issuer. Use this to find the right entry before vault__totp_code.", "parameters": {"type": "object", "properties": {"query": {"type": "string", "description": "Search keyword (e.g. 'google', 'aws', 'github')"}}, "required": ["query"]}}},
    {"type": "function", "function": {"name": "vault__totp_verify", "description": "Verify a TOTP code against a stored entry. Allows ±1 time step for clock drift.", "parameters": {"type": "object", "properties": {"name": {"type": "string", "description": "TOTP entry name"}, "code": {"type": "string", "description": "6-digit code to verify"}}, "required": ["name", "code"]}}},
]


# ── Dispatch table ─────────────────────────────────────────────────────────────
# Maps module_id → handler metadata for _execute_tool() routing.
# Keys:
#   handler     — function name string (resolved to callable in orchestrator.py)
#   user_model  — if True, pass user_model kwarg to handler
#   agent_id    — if True, pass agent_id kwarg to handler
#   aliases     — dict mapping incoming tool_suffix → handler tool_suffix

DISPATCH_TABLE = {
    "system":    {"handler": "_execute_builtin",        "user_model": True},
    "files":     {"handler": "_execute_files_tool"},
    "documents": {"handler": "_execute_documents_tool"},
    "web":       {"handler": "_execute_web_tool",       "user_model": True},
    "exec":      {"handler": "_execute_exec_tool"},
    "email":     {"handler": "_execute_email_tool"},
    "git":       {"handler": "_execute_git_tool"},
    "vault":     {"handler": "_execute_vault_tool"},
    # Aliases — remap tool_suffix before calling target handler
    "agents":    {"handler": "_execute_builtin",        "aliases": {"delegate": "handoff"}},
    "scheduler": {"handler": "_execute_builtin",        "aliases": {"create": "schedule_task", "list": "list_tasks", "cancel": "cancel_task"}},
    "network":   {"handler": "_execute_web_tool",       "aliases": {"http_get": "http_get", "http_post": "http_post", "download": "file_download"}},
    # Memory — requires agent_id context
    "memory":    {"handler": "_execute_memory_tool",    "agent_id": True},
}


# ── Public API ──────────────────────────────────────────────────────────────────

def get_builtin_tools() -> list:
    """Return the full list of built-in tool schemas (OpenAI-compatible format).
    Web search tools are NOT included here — they are dynamically injected
    by orchestrator._build_web_search_tools() based on search_mode."""
    return (
        _SYSTEM_TOOLS
        + _FILES_TOOLS
        + _DOCUMENTS_TOOLS
        + _WEB_TOOLS
        + _EXEC_TOOLS
        + _EMAIL_TOOLS
        + _GIT_TOOLS
        + _AGENTS_TOOLS
        + _SCHEDULER_TOOLS
        + _VAULT_TOOLS
        + _VAULT_TOTP_TOOLS
    )


def get_builtin_tool_names() -> set:
    """Return the set of all built-in tool names for introspection."""
    return {t["function"]["name"] for t in get_builtin_tools()}


# ── Agent memory tools (only added when memory_enabled=true) ────────────────────

AGENT_MEMORY_TOOLS = [
    {"type": "function", "function": {
        "name": "memory__save",
        "description": "Save an important fact to your persistent memory. This survives across all sessions and all interfaces (dashboard, Cowork, mobile). Use when the user tells you something important: your name/identity, their preferences, project context, corrections.",
        "parameters": {"type": "object", "properties": {
            "key": {"type": "string", "description": "Short label for this fact (e.g. 'my_full_name', 'user_company', 'preferred_language')"},
            "value": {"type": "string", "description": "The fact to remember"}
        }, "required": ["key", "value"]}}},
    {"type": "function", "function": {
        "name": "memory__read",
        "description": "Read all your persistent memory facts.",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "memory__delete",
        "description": "Delete a specific fact from your persistent memory.",
        "parameters": {"type": "object", "properties": {
            "key": {"type": "string", "description": "The key to forget"}
        }, "required": ["key"]}}},
]
