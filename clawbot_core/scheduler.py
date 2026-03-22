"""
ClawbotOS Task Scheduler
Scheduled tasks stored in /home/pi/.openjarvis/scheduled-tasks.json
Background thread checks every 30s and executes due tasks via ClawbotCore tool loop.
"""
import json
import logging
import os
import threading
import time
import uuid
from datetime import datetime, timedelta

TASKS_PATH = "/home/pi/.openjarvis/scheduled-tasks.json"
log = logging.getLogger(__name__)


def _load_tasks() -> list:
    try:
        with open(TASKS_PATH) as f:
            return json.load(f)
    except FileNotFoundError:
        return []
    except Exception as e:
        log.error("Failed to load tasks: %s", e)
        return []


def _save_tasks(tasks: list):
    os.makedirs(os.path.dirname(TASKS_PATH), exist_ok=True)
    with open(TASKS_PATH, "w") as f:
        json.dump(tasks, f, indent=2, default=str)


def _calc_next_run(task: dict, from_dt: datetime = None) -> datetime | None:
    """Calculate next run datetime for a task from a given point in time."""
    if from_dt is None:
        from_dt = datetime.now()

    stype = task.get("schedule_type", "once")

    if stype == "once":
        dt_str = task.get("datetime", "")
        try:
            return datetime.fromisoformat(dt_str)
        except Exception:
            return None

    elif stype == "daily":
        time_str = task.get("time", "12:00")
        try:
            h, m = map(int, time_str.split(":"))
            candidate = from_dt.replace(hour=h, minute=m, second=0, microsecond=0)
            if candidate <= from_dt:
                candidate += timedelta(days=1)
            return candidate
        except Exception:
            return None

    elif stype == "weekly":
        time_str = task.get("time", "12:00")
        day_str = task.get("day_of_week", "monday").lower()
        days_map = {
            "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
            "friday": 4, "saturday": 5, "sunday": 6,
            "lundi": 0, "mardi": 1, "mercredi": 2, "jeudi": 3,
            "vendredi": 4, "samedi": 5, "dimanche": 6,
        }
        target_day = days_map.get(day_str, 0)
        try:
            h, m = map(int, time_str.split(":"))
            days_ahead = target_day - from_dt.weekday()
            current_minutes = from_dt.hour * 60 + from_dt.minute
            target_minutes = h * 60 + m
            if days_ahead < 0 or (days_ahead == 0 and current_minutes >= target_minutes):
                days_ahead += 7
            candidate = (from_dt + timedelta(days=days_ahead)).replace(
                hour=h, minute=m, second=0, microsecond=0
            )
            return candidate
        except Exception:
            return None

    elif stype == "hourly":
        minute = int(task.get("minute", 0))
        candidate = from_dt.replace(minute=minute, second=0, microsecond=0)
        if candidate <= from_dt:
            candidate += timedelta(hours=1)
        return candidate

    elif stype == "interval":
        interval_minutes = int(task.get("interval_minutes", 60))
        return from_dt + timedelta(minutes=interval_minutes)

    return None


def create_task(name: str, instruction: str, schedule_type: str, **kwargs) -> dict:
    """Create and persist a new scheduled task. Returns the task dict."""
    task = {
        "id": str(uuid.uuid4())[:8],
        "name": name,
        "instruction": instruction,
        "schedule_type": schedule_type,
        "status": "active",
        "created_at": datetime.now().isoformat(),
        "last_run": None,
        "next_run": None,
        "runs": [],
        **kwargs,
    }
    next_dt = _calc_next_run(task)
    task["next_run"] = next_dt.isoformat() if next_dt else None

    tasks = _load_tasks()
    tasks.append(task)
    _save_tasks(tasks)
    log.info("Task created: %s (%s) — next run: %s", name, task["id"], task["next_run"])
    return task


def list_tasks() -> list:
    return _load_tasks()


def delete_task(task_id: str) -> bool:
    tasks = _load_tasks()
    new_tasks = [t for t in tasks if t["id"] != task_id]
    if len(new_tasks) == len(tasks):
        return False
    _save_tasks(new_tasks)
    return True


def pause_task(task_id: str) -> bool:
    tasks = _load_tasks()
    for t in tasks:
        if t["id"] == task_id:
            t["status"] = "paused"
            _save_tasks(tasks)
            return True
    return False


def resume_task(task_id: str) -> bool:
    tasks = _load_tasks()
    for t in tasks:
        if t["id"] == task_id:
            t["status"] = "active"
            next_dt = _calc_next_run(t)
            t["next_run"] = next_dt.isoformat() if next_dt else None
            _save_tasks(tasks)
            return True
    return False


def _execute_task(task: dict) -> str:
    """Execute a task by calling the ClawbotCore tool loop (non-streaming)."""
    from orchestrator import chat_with_tools
    log.info("Executing scheduled task: %s (%s)", task["name"], task["id"])
    try:
        body = {"messages": [{"role": "user", "content": task["instruction"]}]}
        response = chat_with_tools(body)
        content = (
            response.get("choices", [{}])[0]
            .get("message", {})
            .get("content", "")
            or ""
        )
        return content.strip()[:1000] or "(completed with no output)"
    except Exception as e:
        log.error("Task %s execution error: %s", task["id"], e)
        return f"[error] {e}"


def _scheduler_loop():
    """Background thread: poll every 30s, execute due tasks."""
    log.info("Scheduler thread started")
    while True:
        try:
            now = datetime.now()
            tasks = _load_tasks()
            changed = False

            for task in tasks:
                if task.get("status") != "active":
                    continue
                next_run_str = task.get("next_run")
                if not next_run_str:
                    continue
                try:
                    next_run_dt = datetime.fromisoformat(next_run_str)
                except Exception:
                    continue

                if next_run_dt <= now:
                    result = _execute_task(task)
                    run_record = {"at": now.isoformat(), "result": result}
                    task["last_run"] = now.isoformat()
                    task["runs"] = (task.get("runs", []) + [run_record])[-20:]

                    if task.get("schedule_type") == "once":
                        task["status"] = "completed"
                        task["next_run"] = None
                    else:
                        next_dt = _calc_next_run(task, from_dt=now + timedelta(seconds=1))
                        task["next_run"] = next_dt.isoformat() if next_dt else None

                    changed = True
                    log.info(
                        "Task %s executed. Next: %s", task["id"], task.get("next_run")
                    )

            if changed:
                _save_tasks(tasks)

        except Exception as e:
            log.error("Scheduler loop error: %s", e)

        time.sleep(30)


_scheduler_thread = None


def start_scheduler():
    """Start the background scheduler thread (idempotent)."""
    global _scheduler_thread
    if _scheduler_thread and _scheduler_thread.is_alive():
        return _scheduler_thread
    _scheduler_thread = threading.Thread(
        target=_scheduler_loop, daemon=True, name="clawbot-scheduler"
    )
    _scheduler_thread.start()
    return _scheduler_thread
