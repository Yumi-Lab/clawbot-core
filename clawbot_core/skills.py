"""
ClawbotCore — Skill System
Loads SKILL.md files, matches them to user messages, injects instructions
into system prompts. Compatible with Core mode and Agent mode.

Format: YAML frontmatter (---) + Markdown body
Stdlib-only — no PyYAML or external dependencies.
"""

import json
import logging
import os
import re

log = logging.getLogger(__name__)

SKILLS_DIR = "/home/pi/.clawbot/skills"
DEFAULT_SKILLS_DIR = os.path.join(os.path.dirname(__file__), "default_skills")


# ── YAML frontmatter parser (stdlib-only) ────────────────────────────────────

def _parse_frontmatter(content: str) -> tuple:
    """
    Parse YAML-like frontmatter from a markdown skill file.
    Returns (meta: dict, body: str).
    Handles: string, int, bool, null, list (- item), quoted strings.
    """
    if not content.startswith("---"):
        return {}, content.strip()

    end = content.find("\n---", 3)
    if end == -1:
        return {}, content.strip()

    fm_text = content[3:end].strip()
    body = content[end + 4:].strip()

    meta = {}
    current_key = None
    current_list = None

    for line in fm_text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue

        # List item
        if re.match(r"^\s{2,}- ", line) or stripped.startswith("- "):
            if current_list is not None:
                val = stripped.lstrip("- ").strip()
                current_list.append(val)
            continue

        if ":" in stripped:
            key, _, raw = stripped.partition(":")
            key = key.strip()
            raw = raw.strip()

            if not raw:
                # Start of a list block
                meta[key] = []
                current_list = meta[key]
                current_key = key
            else:
                current_list = None
                current_key = key
                # Parse value type
                if raw.startswith('"') and raw.endswith('"'):
                    meta[key] = raw[1:-1]
                elif raw.startswith("'") and raw.endswith("'"):
                    meta[key] = raw[1:-1]
                elif raw.lower() == "null" or raw.lower() == "~":
                    meta[key] = None
                elif raw.lower() == "true":
                    meta[key] = True
                elif raw.lower() == "false":
                    meta[key] = False
                elif re.match(r"^-?\d+$", raw):
                    meta[key] = int(raw)
                else:
                    meta[key] = raw

    return meta, body


# ── Skill loading ─────────────────────────────────────────────────────────────

def _load_skill_file(path: str) -> dict | None:
    """Load and parse a single SKILL.md file. Returns skill dict or None on error."""
    try:
        with open(path, encoding="utf-8") as f:
            content = f.read()
        meta, body = _parse_frontmatter(content)
        skill_id = meta.get("id") or os.path.splitext(os.path.basename(path))[0]
        return {
            "id": skill_id,
            "name": meta.get("name", skill_id),
            "description": meta.get("description", ""),
            "version": meta.get("version", "1.0.0"),
            "author": meta.get("author", ""),
            "triggers": meta.get("triggers") or [],
            "tools": meta.get("tools") or [],
            "model": meta.get("model"),
            "enabled": meta.get("enabled", True),
            "builtin": meta.get("builtin", False),
            "instructions": body,
            "_path": path,
        }
    except Exception as e:
        log.warning("Failed to load skill %s: %s", path, e)
        return None


def _install_default_skills():
    """Copy bundled default skills to SKILLS_DIR if not already present."""
    if not os.path.isdir(DEFAULT_SKILLS_DIR):
        return
    os.makedirs(SKILLS_DIR, exist_ok=True)
    for fname in os.listdir(DEFAULT_SKILLS_DIR):
        if not fname.endswith(".md"):
            continue
        dest = os.path.join(SKILLS_DIR, fname)
        if not os.path.exists(dest):
            src = os.path.join(DEFAULT_SKILLS_DIR, fname)
            try:
                with open(src, encoding="utf-8") as f:
                    data = f.read()
                with open(dest, "w", encoding="utf-8") as f:
                    f.write(data)
                log.info("Installed default skill: %s", fname)
            except Exception as e:
                log.warning("Failed to install default skill %s: %s", fname, e)


def load_skills(include_disabled: bool = False) -> dict:
    """
    Load all skills from SKILLS_DIR.
    Returns dict keyed by skill id.
    """
    _install_default_skills()
    skills = {}
    if not os.path.isdir(SKILLS_DIR):
        return skills
    for fname in sorted(os.listdir(SKILLS_DIR)):
        if not fname.endswith(".md"):
            continue
        skill = _load_skill_file(os.path.join(SKILLS_DIR, fname))
        if skill and (include_disabled or skill.get("enabled", True)):
            skills[skill["id"]] = skill
    return skills


def save_skill(skill: dict) -> None:
    """Persist a skill to disk (YAML frontmatter + body)."""
    os.makedirs(SKILLS_DIR, exist_ok=True)
    skill_id = skill["id"]
    path = skill.get("_path") or os.path.join(SKILLS_DIR, skill_id + ".md")

    triggers = "\n".join(f"  - {t}" for t in (skill.get("triggers") or []))
    tools = "\n".join(f"  - {t}" for t in (skill.get("tools") or []))
    model_val = skill.get("model") or "null"
    enabled_val = "true" if skill.get("enabled", True) else "false"
    builtin_val = "true" if skill.get("builtin", False) else "false"

    frontmatter = f"""---
id: {skill_id}
name: {skill.get('name', skill_id)}
description: {skill.get('description', '')}
version: {skill.get('version', '1.0.0')}
author: {skill.get('author', '')}
enabled: {enabled_val}
builtin: {builtin_val}
model: {model_val}
triggers:
{triggers or '  []'}
tools:
{tools or '  []'}
---

{skill.get('instructions', '')}
"""
    with open(path, "w", encoding="utf-8") as f:
        f.write(frontmatter)


def delete_skill(skill_id: str) -> bool:
    """Delete a skill file. Returns True if deleted."""
    path = os.path.join(SKILLS_DIR, skill_id + ".md")
    if os.path.isfile(path):
        # Prevent deletion of builtin skills
        skill = _load_skill_file(path)
        if skill and skill.get("builtin"):
            return False
        os.remove(path)
        return True
    return False


# ── Skill matching ────────────────────────────────────────────────────────────

def match_skills(user_message: str, skills: dict = None) -> list:
    """
    Match user message to relevant skills by trigger keywords.
    Returns list of skill dicts sorted by match score (highest first).
    Also respects explicit !skillname invocations.
    """
    if skills is None:
        skills = load_skills()

    msg_lower = user_message.lower()
    scored = []

    # Explicit invocation: !skill-id anywhere in message
    explicit = re.findall(r"!([a-z0-9_-]+)", user_message)
    for skill_id in explicit:
        if skill_id in skills:
            scored.append((100, skills[skill_id]))

    # Trigger-based matching
    for skill in skills.values():
        if any(s["id"] == skill["id"] for _, s in scored):
            continue  # already added via explicit invocation
        score = 0
        for trigger in skill.get("triggers") or []:
            if trigger.lower() in msg_lower:
                score += 1
        if score > 0:
            scored.append((score, skill))

    scored.sort(key=lambda x: -x[0])
    return [s for _, s in scored]


# ── Prompt injection ──────────────────────────────────────────────────────────

def build_skill_prompt(matched_skills: list) -> str:
    """
    Build a system prompt section from matched skills.
    Each skill's instructions are injected under a labeled section.
    """
    if not matched_skills:
        return ""

    parts = ["## Active Skills\n"]
    for skill in matched_skills:
        name = skill.get("name", skill["id"])
        instructions = (skill.get("instructions") or "").strip()
        if instructions:
            parts.append(f"### {name}\n{instructions}\n")

    return "\n".join(parts)


def get_skill_tools(matched_skills: list) -> list:
    """
    Collect required tool names from matched skills.
    Returns deduplicated list of tool names.
    """
    tools = []
    seen = set()
    for skill in matched_skills:
        for tool in skill.get("tools") or []:
            if tool not in seen:
                tools.append(tool)
                seen.add(tool)
    return tools
