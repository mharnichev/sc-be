"""Process-local successful iteration probes; fail closed in unverified processes."""
import asyncio
import time

_successful_iterations: dict[str, float] = {}


def worker_iteration_succeeded(name: str) -> None:
    _successful_iterations[name] = time.monotonic()


def worker_healthy(name: str, max_age_seconds: float) -> bool:
    last = _successful_iterations.get(name)
    if last is None or time.monotonic() - last > max_age_seconds:
        return False
    return any(task.get_name() == name and not task.done() for task in asyncio.all_tasks())
