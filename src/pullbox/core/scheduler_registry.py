"""Scheduler task registration helpers."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, TypeVar

F = TypeVar("F", bound=Callable[..., Any])


@dataclass
class RegisteredTask:
    """Metadata for a task collected by the ``@scheduled_task`` decorator."""

    func: Callable[..., Any]
    task_id: str
    trigger: str
    trigger_kwargs: dict[str, Any]
    display_name: str = ""
    exclusive: bool = False
    available: Callable[[], bool] | None = None


task_registry: list[RegisteredTask] = []


def scheduled_task(
    *,
    task_id: str,
    trigger: str,
    display_name: str = "",
    exclusive: bool = False,
    available: Callable[[], bool] | None = None,
    **trigger_kwargs: Any,
) -> Callable[[F], F]:
    """Register an async function as a scheduled background task."""

    def decorator(func: F) -> F:
        task_registry.append(
            RegisteredTask(
                func=func,
                task_id=task_id,
                trigger=trigger,
                trigger_kwargs=dict(trigger_kwargs),
                display_name=display_name or task_id.replace("_", " ").title(),
                exclusive=exclusive,
                available=available,
            )
        )
        return func

    return decorator


def get_registered_tasks() -> list[RegisteredTask]:
    """Return a copy of all tasks registered via ``@scheduled_task``."""
    return list(task_registry)
