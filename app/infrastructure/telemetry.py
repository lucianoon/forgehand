from __future__ import annotations

from collections import defaultdict


class HttpMetrics:
    """Métricas HTTP de baixa cardinalidade para exposição Prometheus."""

    def __init__(self) -> None:
        self._requests: dict[tuple[str, str, int], int] = defaultdict(int)
        self._duration_seconds: dict[tuple[str, str], float] = defaultdict(float)

    def record(self, method: str, route: str, status_code: int, seconds: float) -> None:
        self._requests[(method, route, status_code)] += 1
        self._duration_seconds[(method, route)] += seconds

    def render(self, runtime: dict[str, object]) -> str:
        lines = [
            "# HELP forgehand_http_requests_total Total de requests HTTP.",
            "# TYPE forgehand_http_requests_total counter",
        ]
        for (method, route, status_code), value in sorted(self._requests.items()):
            labels = (
                f'method="{_escape(method)}",route="{_escape(route)}",'
                f'status="{status_code}"'
            )
            lines.append(f"forgehand_http_requests_total{{{labels}}} {value}")
        lines.extend(
            [
                "# HELP forgehand_http_request_duration_seconds_total Tempo HTTP acumulado.",
                "# TYPE forgehand_http_request_duration_seconds_total counter",
            ]
        )
        for (method, route), duration in sorted(self._duration_seconds.items()):
            labels = f'method="{_escape(method)}",route="{_escape(route)}"'
            lines.append(
                f"forgehand_http_request_duration_seconds_total{{{labels}}} {duration:.6f}"
            )
        lines.extend(_runtime_lines(runtime))
        return "\n".join(lines) + "\n"


def _runtime_lines(runtime: dict[str, object]) -> list[str]:
    workers = runtime.get("workers", {})
    queue = runtime.get("queue", {})
    workflow_runtime = runtime.get("workflow_runtime", {})
    if not isinstance(workers, dict) or not isinstance(queue, dict):
        return []
    if not isinstance(workflow_runtime, dict):
        workflow_runtime = {}
    values = {
        "forgehand_workers_running": workers.get("running", 0),
        "forgehand_workers_busy": workers.get("busy", 0),
        "forgehand_workers_registered": workers.get("registered", 0),
        "forgehand_queue_queued": queue.get("queued", 0),
        "forgehand_queue_processing": queue.get("processing", 0),
        "forgehand_queue_failed": queue.get("failed", 0),
        "forgehand_workflows_active": workflow_runtime.get("active_workflows", 0),
    }
    lines: list[str] = []
    for name, value in values.items():
        lines.extend([f"# TYPE {name} gauge", f"{name} {value}"])
    return lines


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
