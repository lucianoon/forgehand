from app.infrastructure.telemetry import HttpMetrics


def test_prometheus_metrics_use_route_templates_and_runtime_gauges():
    metrics = HttpMetrics()
    metrics.record("GET", "/workflows/{workflow_id}", 200, 0.125)
    rendered = metrics.render(
        {
            "workers": {"running": 2, "busy": 1},
            "queue": {"queued": 3, "processing": 1, "failed": 0},
            "workflow_runtime": {"active_workflows": 1},
        }
    )

    assert 'route="/workflows/{workflow_id}"' in rendered
    assert "forgehand_http_requests_total" in rendered
    assert "forgehand_queue_queued 3" in rendered
    assert "forgehand_workers_busy 1" in rendered
