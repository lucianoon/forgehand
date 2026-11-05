"""Developer CLI submits factory orders and follows PRs without duplicate dispatch."""
from __future__ import annotations

import json
from uuid import UUID

import httpx
import pytest

from app.cli import main
from app.factory.intake import DirectWorkOrderInput, GitHubIssueWorkOrderInput

BASE = ["deliver", "--project", "backend", "--repository", "team/service",
        "--request", "Corrija o cálculo de desconto", "--criterion", "Desconto mantém centavos",
        "--budget-usd", "0.75"]
SHA = "a" * 40
READY = {
    "workflow_id": "wf-1", "status": "ready_for_human_review",
    "current_stage": "ready_for_human_review", "iteration": 1,
    "usage": {"cost_usd": 0.0234}, "tasks": [], "pending_decision": None,
    "final_output": "Desconto corrigido e testado.",
    "delivery": {"url": "https://github.com/team/service/pull/42",
                 "commit_sha": SHA, "ci_state": "success"},
}


def invoke(argv, handler):
    return main(["--url", "http://api", "--api-key", "secret-fixture", *argv],
                transport=httpx.MockTransport(handler))


def no_network(request):
    pytest.fail("invalid input or dry-run accessed the API")


def changed_args(option, value):
    argv = list(BASE)
    if option in argv:
        argv[argv.index(option) + 1] = value
    else:
        argv += [option, value]
    return argv


def test_direct_delivery_posts_valid_order_then_prints_verified_pr(capsys):
    requests = []

    def handle(request):
        requests.append(request)
        if request.method == "POST":
            assert request.headers["X-API-Key"] == "secret-fixture"
            assert request.url.path == "/workflows"
            body = json.loads(request.content)
            assert set(body) == {"project_id", "work_order"}
            order = DirectWorkOrderInput.model_validate(body["work_order"])
            assert body["project_id"] == "backend"
            assert order.repository == "team/service"
            assert order.base_ref == "release/1"
            assert order.expected_base_sha == SHA
            assert order.build_profile == "python-service"
            assert order.idempotency_key == "delivery-123"
            assert order.acceptance_criteria == ["Desconto mantém centavos", "Sem desconto negativo"]
            assert order.limits.model_dump() == {
                "max_cost_usd": .75, "max_tokens": 10000,
                "max_iterations": 2, "max_wall_clock_seconds": 600,
            }
            assert order.delivery_policy.create_pull_request
            assert order.delivery_policy.wait_for_checks
            assert order.delivery_policy.require_human_merge
            return httpx.Response(202, json={"workflow_id": "wf-1"})
        assert request.method == "GET" and request.url.path == "/workflows/wf-1"
        return httpx.Response(200, json=READY)

    code = invoke(BASE + ["--base-ref", "release/1", "--expected-base-sha", SHA,
                         "--build-profile", "python-service", "--idempotency-key", "delivery-123",
                         "--criterion", "Sem desconto negativo", "--max-tokens", "10000",
                         "--max-iterations", "2", "--max-wall-clock-seconds", "600"], handle)
    out, err = capsys.readouterr()
    assert code == 0 and len(requests) == 2
    assert READY["delivery"]["url"] in out and SHA in out and "CI: success" in out
    assert "merge é uma decisão humana" in out
    assert "US$ 0.0234" in err and "delivery-123" in err
    assert "secret-fixture" not in out + err


def test_issue_delivery_and_no_wait_do_not_fetch_issue_on_client(capsys):
    calls = []

    def handle(request):
        calls.append(request)
        assert request.method == "POST" and request.url.path == "/workflows"
        body = json.loads(request.content)
        order = GitHubIssueWorkOrderInput.model_validate(body["work_order"])
        assert order.issue_url == "https://github.com/team/service/issues/8"
        assert order.build_profile == "node-service"
        UUID(order.idempotency_key)
        return httpx.Response(202, json={"workflow_id": "wf-issue"})

    code = invoke(["deliver", "--project", "backend", "--issue",
                   "https://github.com/team/service/issues/8", "--criterion", "CI passa",
                   "--budget-usd", "1", "--build-profile", "node-service", "--no-wait"], handle)
    out, err = capsys.readouterr()
    assert code == 0 and out.strip() == "wf-issue" and len(calls) == 1
    assert "Chave de idempotência:" in err


@pytest.mark.parametrize("issue", [False, True])
def test_dry_run_prints_valid_request_without_network(issue, capsys):
    argv = BASE + ["--dry-run"]
    if issue:
        argv = ["deliver", "--project", "backend", "--issue",
                "https://github.com/team/service/issues/8", "--criterion", "CI passa",
                "--budget-usd", "1", "--dry-run"]
    assert invoke(argv, no_network) == 0
    out, err = capsys.readouterr()
    body = json.loads(out)
    (GitHubIssueWorkOrderInput if issue else DirectWorkOrderInput).model_validate(body["work_order"])
    assert UUID(body["work_order"]["idempotency_key"])
    assert body["project_id"] == "backend"
    assert "secret-fixture" not in out + err


@pytest.mark.parametrize(("option", "value"), [
    ("--project", "  "), ("--repository", " "), ("--repository", "team/service/extra"),
    ("--repository", "team/.."), ("--repository", "team?/service"),
    ("--request", "  "), ("--request", "short"), ("--criterion", " \n "),
    ("--budget-usd", "nan"), ("--budget-usd", "inf"), ("--budget-usd", "0"), ("--budget-usd", "-1"),
    ("--budget-usd", "abc"), ("--max-tokens", "0"), ("--max-iterations", "-1"),
    ("--max-wall-clock-seconds", "0"), ("--base-ref", " "), ("--base-ref", "main..backup"),
    ("--base-ref", "main:other"), ("--base-ref", "main branch"), ("--base-ref", "refs/.private"),
    ("--base-ref", "branch.lock"), ("--base-ref", "main\\other"),
    ("--expected-base-sha", "invalid"), ("--build-profile", " "), ("--idempotency-key", " "),
    ("--idempotency-key", "x" * 256), ("--poll-seconds", "nan"), ("--poll-seconds", "-1"),
    ("--timeout", "inf"), ("--timeout", "0"),
])
def test_invalid_delivery_is_rejected_before_http(option, value, capsys):
    with pytest.raises(SystemExit) as error:
        invoke(changed_args(option, value), no_network)
    assert error.value.code == 2
    assert "secret-fixture" not in capsys.readouterr().err


@pytest.mark.parametrize("option", ["--project", "--repository", "--request", "--criterion", "--budget-usd"])
def test_required_delivery_fields_are_rejected_before_http(option):
    argv = list(BASE)
    index = argv.index(option)
    del argv[index:index + 2]
    with pytest.raises(SystemExit) as error:
        invoke(argv, no_network)
    assert error.value.code == 2


@pytest.mark.parametrize("url", [
    "http://github.com/team/service/issues/8", "https://example.com/team/service/issues/8",
    "https://github.com/team/service/pull/8", "https://github.com/team/service/issues/0",
    "https://github.com/team/service/issues/8?token=anything",
    "https://user:password@github.com/team/service/issues/8",
    "https://github.com:443/team/service/issues/8",
    "https://github.com/../service/issues/8", "https://github.com/team/../issues/8",
])
def test_invalid_issue_url_is_rejected_before_http(url):
    with pytest.raises(SystemExit) as error:
        invoke(["deliver", "--project", "p", "--issue", url,
                "--criterion", "CI passa", "--budget-usd", "1"], no_network)
    assert error.value.code == 2


@pytest.mark.parametrize("extra", [
    ["--repository", "team/service"], ["--request", "Não substituir a issue"],
    ["--expected-base-sha", SHA],
])
def test_issue_rejects_conflicting_source_options(extra):
    with pytest.raises(SystemExit) as error:
        invoke(["deliver", "--project", "p", "--issue", "https://github.com/team/service/issues/8",
                "--criterion", "CI passa", "--budget-usd", "1", *extra], no_network)
    assert error.value.code == 2


@pytest.mark.parametrize(("state", "expected"), [
    ("ready_for_human_review", 0), ("completed", 0), ("failed", 1),
    ("cancelled", 1), ("awaiting_decision", 1),
])
def test_wait_only_reads_and_stops_at_review_failures_or_decisions(state, expected, capsys):
    calls = []
    payload = {**READY, "status": state}
    if state == "awaiting_decision":
        payload["pending_decision"] = {"reason": "CI vermelho", "options": ["retry", "abort"]}

    def handle(request):
        calls.append(request)
        assert request.method == "GET" and request.url.path == "/workflows/wf-1"
        return httpx.Response(200, json=payload)

    assert invoke(["--json", "wait", "wf-1"], handle) == expected
    assert json.loads(capsys.readouterr().out) == payload
    assert len(calls) == 1


def test_wait_tracks_progress_then_stops_at_review(capsys):
    calls = []

    def handle(request):
        calls.append(request)
        assert request.method == "GET"
        return httpx.Response(200, json=READY if len(calls) == 2 else {**READY, "status": "running"})

    assert invoke(["wait", "wf-1", "--poll-seconds", "0"], handle) == 0
    assert len(calls) == 2
    assert "ready_for_human_review" in capsys.readouterr().err


def test_wait_timeout_does_not_cancel_and_prints_resume_id(capsys):
    calls = []

    def handle(request):
        calls.append(request)
        return httpx.Response(200, json={**READY, "status": "running"})

    assert invoke(["wait", "wf-1", "--timeout", "0.01", "--poll-seconds", "1"], handle) == 3
    assert calls and all(request.method == "GET" for request in calls)
    err = capsys.readouterr().err
    assert "não foi cancelada" in err and "forgehand wait wf-1" in err


def test_uncertain_submission_prints_key_without_retry_or_secrets(capsys):
    calls = []

    def handle(request):
        calls.append(request)
        raise httpx.ReadTimeout("leaked secret-fixture in exception", request=request)

    assert invoke(BASE + ["--idempotency-key", "same-delivery"], handle) == 2
    out, err = capsys.readouterr()
    assert len(calls) == 1 and calls[0].method == "POST"
    assert "same-delivery" in err and "mesma --idempotency-key" in err
    assert "secret-fixture" not in out + err and "Traceback" not in out + err


def test_wait_transport_error_preserves_workflow_and_sanitizes_failure(capsys):
    calls = []

    def handle(request):
        calls.append(request)
        raise httpx.ConnectError("secret-fixture", request=request)

    assert invoke(["wait", "wf-1"], handle) == 2
    out, err = capsys.readouterr()
    assert len(calls) == 1 and calls[0].method == "GET"
    assert "forgehand wait wf-1" in err and "secret-fixture" not in out + err


def test_delivery_http_rejection_does_not_poll_or_retry(capsys):
    calls = []

    def handle(request):
        calls.append(request)
        return httpx.Response(403, json={"detail": "Papel approver necessário"})

    assert invoke(BASE, handle) == 2
    err = capsys.readouterr().err
    assert len(calls) == 1 and "erro 403" in err
    assert "O envio pode ter sido recebido" not in err


@pytest.mark.parametrize("ref", ["release+fix", "ação", "branch!", "_private"])
def test_delivery_rejects_refs_not_supported_by_factory(ref):
    with pytest.raises(SystemExit) as error:
        invoke(changed_args("--base-ref", ref), no_network)
    assert error.value.code == 2


@pytest.mark.parametrize("ref", ["main", "release/1.2-fix", "refs/heads/feature_name"])
def test_delivery_accepts_factory_refs(ref, capsys):
    assert invoke(changed_args("--base-ref", ref) + ["--dry-run"], no_network) == 0
    assert json.loads(capsys.readouterr().out)["work_order"]["base_ref"] == ref


@pytest.mark.parametrize("response_body", [
    "<html>secret-response</html>", "[]", "null", "{}",
    '{"workflow_id": null}', '{"workflow_id": 42}',
    '{"workflow_id": "../secret-response"}', '{"workflow_id": ""}',
])
def test_malformed_creation_preserves_key_without_retry(response_body, capsys):
    calls = []

    def handle(request):
        calls.append(request)
        return httpx.Response(202, text=response_body)

    assert invoke(BASE + ["--idempotency-key", "same-delivery"], handle) == 2
    out, err = capsys.readouterr()
    assert len(calls) == 1 and calls[0].method == "POST"
    assert "same-delivery" in err and "mesma --idempotency-key" in err
    assert not out and "secret-response" not in err and "Traceback" not in err


@pytest.mark.parametrize("body", [
    "<html>secret-response</html>", "[]", "null", "{}",
    json.dumps({**READY, "workflow_id": "wf-other"}),
    json.dumps({**READY, "status": []}),
    json.dumps({**READY, "status": "unrecognized"}),
    json.dumps({**READY, "current_stage": {"secret-response": 1}}),
    json.dumps({**READY, "usage": []}),
    json.dumps({**READY, "usage": {"cost_usd": "secret-response"}}),
    json.dumps({**READY, "usage": {"cost_usd": True}}),
    '{"workflow_id":"wf-1","status":"completed","usage":{"cost_usd":NaN}}',
    json.dumps({**READY, "tasks": 42}),
    json.dumps({**READY, "pending_decision": "secret-response"}),
    json.dumps({**READY, "pending_decision": {"options": [42]}}),
    json.dumps({**READY, "pending_decision": {"reason": []}}),
    json.dumps({**READY, "delivery": []}),
    json.dumps({**READY, "delivery": {"url": {"secret-response": 1}}}),
    json.dumps({**READY, "delivery": {"commit_sha": []}}),
    json.dumps({**READY, "delivery": {"ci_state": []}}),
    json.dumps({**READY, "final_output": {"secret-response": 1}}),
])
@pytest.mark.parametrize("command", ["wait", "status"])
def test_malformed_status_is_sanitized_and_retains_workflow(body, command, capsys):
    calls = []

    def handle(request):
        calls.append(request)
        return httpx.Response(200, text=body)

    assert invoke([command, "wf-1"], handle) == 2
    out, err = capsys.readouterr()
    assert len(calls) == 1 and calls[0].method == "GET"
    assert "forgehand wait wf-1" in err
    assert not out and "secret-response" not in err and "Traceback" not in err


@pytest.mark.parametrize("response_body", [
    '<html>secret-response</html>', '["secret-response"]',
    '{"detail":"secret-response"}', '{"detail":{"token":"secret-response"}}',
])
def test_http_errors_never_echo_arbitrary_response_bodies(response_body, capsys):
    calls = []

    def handle(request):
        calls.append(request)
        return httpx.Response(502, text=response_body)

    assert invoke(BASE, handle) == 2
    out, err = capsys.readouterr()
    assert len(calls) == 1 and calls[0].method == "POST"
    assert "erro 502" in err and "secret-response" not in out + err


@pytest.mark.parametrize("status_code", [502, 408])
def test_server_or_request_timeout_preserves_uncertain_submission(status_code, capsys):
    calls = []

    def handle(request):
        calls.append(request)
        return httpx.Response(status_code, json={"detail": "secret-response"})

    assert invoke(BASE + ["--idempotency-key", "same-delivery"], handle) == 2
    out, err = capsys.readouterr()
    assert len(calls) == 1 and calls[0].method == "POST"
    assert "same-delivery" in err and "mesma --idempotency-key" in err
    assert "secret-response" not in out + err
