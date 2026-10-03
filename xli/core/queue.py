#!/usr/bin/env python3
"""
XLI Queue v4 — Background tasks, priority, status tracking
"""

import asyncio
import json
import uuid
from typing import Any
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum

from xli.paths import xli_path
from xli.core.logger import StructuredLogger

logger = StructuredLogger("xli.queue")

QUEUE_DIR = xli_path("queue")


class TaskPriority(Enum):
    """Task priority levels"""
    CRITICAL = 0
    HIGH = 1
    NORMAL = 2
    LOW = 3


class TaskStatus(Enum):
    """Task status"""
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class Task:
    """Background task"""
    id: str
    task_type: str
    payload: dict[str, Any]
    priority: TaskPriority
    status: TaskStatus = TaskStatus.PENDING
    created: datetime = field(default_factory=datetime.now)
    started: datetime | None = None
    completed: datetime | None = None
    result: Any | None = None
    error: str | None = None


class TaskQueue:
    """Priority task queue with persistence"""

    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized:
            return
        self._initialized = True

        QUEUE_DIR.mkdir(parents=True, exist_ok=True)
        self.tasks: dict[str, Task] = {}
        self._handlers: dict[str, Callable] = {}
        self._processing = False
        self._task_file = QUEUE_DIR / "tasks.json"

        self._load_tasks()
        logger.log_structured("INFO", "queue",
                             f"TaskQueue initialized, {len(self.tasks)} tasks")

    def _load_tasks(self):
        """Load persisted tasks"""
        if self._task_file.exists():
            try:
                with open(self._task_file) as f:
                    data = json.load(f)

                for task_id, task_data in data.items():
                    self.tasks[task_id] = Task(
                        id=task_data["id"],
                        task_type=task_data["task_type"],
                        payload=task_data["payload"],
                        priority=TaskPriority(task_data["priority"]),
                        status=TaskStatus(task_data["status"]),
                        created=datetime.fromisoformat(task_data["created"]),
                        started=datetime.fromisoformat(task_data["started"]) if task_data.get("started") else None,
                        completed=datetime.fromisoformat(task_data["completed"]) if task_data.get("completed") else None,
                        result=task_data.get("result"),
                        error=task_data.get("error")
                    )
            except Exception as e:
                logger.log_error("queue", "Load failed", exc=e)

    def _save_tasks(self):
        """Persist tasks"""
        try:
            data = {}
            for task_id, task in self.tasks.items():
                data[task_id] = {
                    "id": task.id,
                    "task_type": task.task_type,
                    "payload": task.payload,
                    "priority": task.priority.value,
                    "status": task.status.value,
                    "created": task.created.isoformat(),
                    "started": task.started.isoformat() if task.started else None,
                    "completed": task.completed.isoformat() if task.completed else None,
                    "result": task.result,
                    "error": task.error
                }

            with open(self._task_file, "w") as f:
                json.dump(data, f, indent=2)

        except Exception as e:
            logger.log_error("queue", "Save failed", exc=e)

    def register_handler(self, task_type: str, handler: Callable):
        """Register task handler"""
        self._handlers[task_type] = handler
        logger.log_structured("DEBUG", "queue",
                             f"Handler registered: {task_type}")

    def enqueue(self, task_type: str, payload: dict[str, Any],
                priority: TaskPriority = TaskPriority.NORMAL) -> str:
        """Add task to queue"""
        task_id = str(uuid.uuid4())[:8]

        task = Task(
            id=task_id,
            task_type=task_type,
            payload=payload,
            priority=priority
        )

        self.tasks[task_id] = task
        self._save_tasks()

        logger.log_structured("INFO", "queue",
                             f"Enqueued: {task_id} ({task_type})",
                             {"priority": priority.name})

        # Start processing if not running
        if not self._processing:
            asyncio.create_task(self.process_queue())

        return task_id

    def dequeue(self) -> Task | None:
        """Get highest priority pending task"""
        pending = [
            t for t in self.tasks.values()
            if t.status == TaskStatus.PENDING
        ]

        if not pending:
            return None

        # Sort by priority, then by creation time
        pending.sort(key=lambda t: (t.priority.value, t.created))
        return pending[0]

    def get_status(self, task_id: str) -> dict | None:
        """Get task status"""
        if task_id not in self.tasks:
            return None

        task = self.tasks[task_id]
        return {
            "id": task.id,
            "type": task.task_type,
            "status": task.status.value,
            "priority": task.priority.name,
            "created": task.created.isoformat(),
            "started": task.started.isoformat() if task.started else None,
            "completed": task.completed.isoformat() if task.completed else None,
            "result": task.result,
            "error": task.error
        }

    def get_results(self, task_id: str) -> Any | None:
        """Get task results"""
        if task_id not in self.tasks:
            return None

        task = self.tasks[task_id]
        if task.status == TaskStatus.COMPLETED:
            return task.result
        return None

    def cancel(self, task_id: str) -> bool:
        """Cancel pending task"""
        if task_id not in self.tasks:
            return False

        task = self.tasks[task_id]
        if task.status == TaskStatus.PENDING:
            task.status = TaskStatus.CANCELLED
            self._save_tasks()
            logger.log_structured("INFO", "queue", f"Cancelled: {task_id}")
            return True

        return False

    async def process_queue(self):
        """Process queue in background"""
        if self._processing:
            return

        self._processing = True
        logger.log_structured("INFO", "queue", "Queue processing started")

        try:
            while True:
                task = self.dequeue()

                if not task:
                    break

                # Process task
                task.status = TaskStatus.RUNNING
                task.started = datetime.now()
                self._save_tasks()

                handler = self._handlers.get(task.task_type)

                if handler:
                    try:
                        if asyncio.iscoroutinefunction(handler):
                            result = await handler(task.payload)
                        else:
                            result = handler(task.payload)

                        task.status = TaskStatus.COMPLETED
                        task.result = result
                        logger.log_structured("INFO", "queue",
                                             f"Completed: {task.id}")

                    except Exception as e:
                        task.status = TaskStatus.FAILED
                        task.error = str(e)
                        logger.log_error("queue",
                                        f"Failed: {task.id}", exc=e)
                else:
                    task.status = TaskStatus.FAILED
                    task.error = f"No handler for {task.task_type}"
                    logger.log_structured("ERROR", "queue",
                                         f"No handler: {task.task_type}")

                task.completed = datetime.now()
                self._save_tasks()

                # Brief pause between tasks
                await asyncio.sleep(0.1)

        finally:
            self._processing = False
            logger.log_structured("INFO", "queue", "Queue processing stopped")

    def list_tasks(self, status: TaskStatus | None = None) -> list[dict]:
        """List tasks"""
        tasks = self.tasks.values()

        if status:
            tasks = [t for t in tasks if t.status == status]

        return [
            {
                "id": t.id,
                "type": t.task_type,
                "status": t.status.value,
                "priority": t.priority.name,
                "created": t.created.isoformat()
            }
            for t in sorted(tasks, key=lambda x: x.created, reverse=True)
        ]


def get_queue() -> TaskQueue:
    """Get singleton TaskQueue"""
    return TaskQueue()

