import asyncio
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from uuid import UUID, uuid4

import httpx
import pytest

from app.agents.product import ProductStudio
from app.api.service import WorkflowNotFound, WorkflowService
from app.factory.product_delivery import IncrementalDelivery, context_capsule
from app.infrastructure.audit import InMemoryAuditLog
from app.infrastructure.product_delivery_store import ProductDeliveryStore
from app.infrastructure.product_store import ProductConflict, ProductStore
from app.infrastructure.scm import GitHubSCMClient, SCMError
from app.infrastructure.settings import Settings
from app.infrastructure.workflow_queue import (
    InMemoryWorkflowQueue,
    WorkflowAccessContext,
)
from app.models.product_delivery import (
    AppendDelivery,
    MergeReceipt,
    ProductDeliveryPlan,
    StartDelivery,
)

SHA = "a" * 40
MERGE = "b" * 40
BASE = "c" * 40


def feature(title="Disponibilidade"):
    return {
        "title": title,
        "description": "Implementar disponibilidade dos profissionais",
        "acceptance_criteria": ["Impedir dois agendamentos conflitantes"],
    }


def definition():
    return ProductDeliveryPlan(
        repository="acme/agenda",
        features=[feature(), feature("Notificações")],
        decisions=["Usar transações no banco"],
        preservation_constraints=["Preservar agendamentos existentes"],
    )


def setup(tmp_path):
    products = ProductStore(str(tmp_path / "studio.db"))
    product, _ = products.create(
        "owner", {"project_id": "p", "idempotency_key": "test-create"}, "fingerprint"
    )
    product.update(
        status="ready_for_preview",
        brief={"name": "Agenda", "out_of_scope": ["Demo sem backend"]},
    )
    products.transition(product, "drafting")
    store = ProductDeliveryStore(products)
    plan = store.create(product, "owner", definition())
    return product, store, plan


class Workflows:
    def __init__(self):
        self.calls = []
        self.states = {}
        self.fail = False
        self.health = {
            "ready": True,
            "queue_ready": True,
            "embedded_workers_enabled": True,
        }

    async def readiness(self):
        return self.health

    async def dispatch_scope(self):
        return "test-queue"

    async def start(self, **kwargs):
        self.calls.append(kwargs)
        if self.fail:
            raise RuntimeError("secret provider body")
        self.states[kwargs["workflow_id"]] = {
            "phase": "executing",
            "pending_decision": None,
        }
        return kwargs["workflow_id"]

    async def get_access_context(self, workflow_id):
        if workflow_id not in self.states:
            raise WorkflowNotFound(workflow_id)
        return WorkflowAccessContext(workflow_id, "p", "owner")

    async def get(self, workflow_id):
        return self.states[workflow_id]


class SCM:
    def __init__(self):
        self.merged = False
        self.bases = []
        self.reject = False
        self.verifications = []

    async def delivery_base(self, repository, branch, ancestor=None):
        self.bases.append((repository, branch, ancestor))
        if self.reject:
            raise SCMError("rewritten history")
        return BASE

    async def verified_delivery_merge(self, repository, branch, number, head):
        self.verifications.append((repository, branch, number, head))
        return (
            MergeReceipt(
                pull_request_number=number,
                commit_sha=head,
                merge_commit_sha=MERGE,
                base_sha=BASE,
            )
            if self.merged
            else None
        )


def approved(revision):
    return StartDelivery(revision=revision, approved=True, max_cost_usd=0.2)


def build_profiles():
    return json.dumps(
        {
            "python-checks": {
                "ecosystem": "python",
                "image": "python@sha256:" + "a" * 64,
                "auto_detect": True,
                "phases": [
                    {"name": "test", "argv": ["/usr/local/bin/python", "-m", "pytest"]}
                ],
            }
        }
    )


def review_state(ci="success"):
    return {
        "phase": "ready_for_human_review",
        "pending_decision": None,
        "delivery": {"ci_state": ci, "pull_request_number": 7, "commit_sha": SHA},
    }


@pytest.mark.parametrize(
    "change",
    [
        {"repository": "../agenda"},
        {"repository": "acme/.."},
        {"repository": "https://github.com/acme/repo"},
        {"base_ref": "main?token=secret"},
        {"base_ref": "../main"},
        {"base_ref": "a//b"},
        {"base_ref": "a/.hidden"},
        {"base_ref": "a.lock"},
        {"command": "rm -rf"},
        {"preservation_constraints": []},
        {"features": [feature()] * 21},
    ],
)
def test_invalid_contracts_rejected(change):
    with pytest.raises(ValueError):
        ProductDeliveryPlan.model_validate({**definition().model_dump(), **change})


def test_persistent_plan_append_only_and_owner_scope(tmp_path):
    product, store, plan = setup(tmp_path)
    restored = ProductDeliveryStore(ProductStore(store.products.path))
    assert restored.get(product["id"], "owner") == plan
    assert restored.create(product, "owner", definition()) == plan
    with pytest.raises(KeyError):
        restored.get(product["id"], "other")
    with pytest.raises(ProductConflict):
        restored.create(
            product,
            "owner",
            definition().model_copy(update={"repository": "other/repo"}),
        )
    changed = restored.append(
        product["id"],
        "owner",
        AppendDelivery(
            revision=1, features=[feature("Equipes")], decisions=["Sem apagar dados"]
        ),
    )
    assert len(changed["features"]) == 3
    assert changed["features"][:2] == plan["features"]
    assert changed["original_brief"] == product["brief"]
    with pytest.raises(ProductConflict):
        restored.append(
            product["id"], "owner", AppendDelivery(revision=1, decisions=["Stale"])
        )


@pytest.mark.asyncio
async def test_concurrent_start_only_dispatches_once(tmp_path):
    product, store, _ = setup(tmp_path)
    workflows, scm = Workflows(), SCM()
    service = IncrementalDelivery(store, workflows)
    results = await asyncio.gather(
        *(service.start(product["id"], "owner", approved(1), scm) for _ in range(2)),
        return_exceptions=True,
    )
    assert len(workflows.calls) == 1
    assert sum(isinstance(value, ProductConflict) for value in results) == 1
    attempt = store.get(product["id"], "owner")["features"][0]["attempts"][0]
    sent = workflows.calls[0]
    assert attempt["workflow_id"] == sent["workflow_id"]
    assert sent["work_order"].repository.expected_base_sha == BASE
    assert sent["work_order"].limits.max_cost_usd == 0.2
    assert sent["work_order"].delivery_policy.require_human_merge
    capsule = store.context(product["id"], "owner", sent["workflow_id"])
    assert capsule["sha256"] == attempt["context_sha256"]
    assert "Preservar agendamentos existentes" in sent["request"]
    with pytest.raises(KeyError):
        store.context(product["id"], "other", sent["workflow_id"])


@pytest.mark.asyncio
async def test_restart_merge_gate_and_second_delivery_context(tmp_path):
    product, store, _ = setup(tmp_path)
    workflows, scm = Workflows(), SCM()
    service = IncrementalDelivery(store, workflows)
    started = await service.start(product["id"], "owner", approved(1), scm)
    first_id = workflows.calls[0]["workflow_id"]
    workflows.states[first_id] = review_state()
    review = await service.reconcile(product["id"], "owner", scm)
    assert review["features"][0]["status"] == "awaiting_review"
    with pytest.raises(ProductConflict):
        await service.start(product["id"], "owner", approved(review["revision"]), scm)
    scm.merged = True
    service = IncrementalDelivery(
        ProductDeliveryStore(ProductStore(store.products.path)), workflows
    )
    merged = await service.reconcile(product["id"], "owner", scm)
    assert merged["features"][0]["status"] == "merged"
    # Late running acknowledgement cannot overwrite the verified receipt.
    assert (
        store.update_attempt(product["id"], "owner", started["revision"], "running", {})
        == merged
    )
    await service.start(product["id"], "owner", approved(merged["revision"]), scm)
    assert scm.bases[-1] == ("acme/agenda", "main", MERGE)
    second_id = workflows.calls[-1]["workflow_id"]
    context = store.context(product["id"], "owner", second_id)["context"]
    assert context["current_feature"]["title"] == "Notificações"
    assert context["merged_features"][0]["receipt"]["merge_commit_sha"] == MERGE
    assert context["approved_decisions"] == ["Usar transações no banco"]
    assert context["preservation_constraints"] == ["Preservar agendamentos existentes"]


@pytest.mark.asyncio
async def test_uncertain_dispatch_never_retries_automatically(tmp_path):
    product, store, _ = setup(tmp_path)
    workflows, scm = Workflows(), SCM()
    workflows.fail = True
    service = IncrementalDelivery(store, workflows)
    unknown = await service.start(product["id"], "owner", approved(1), scm)
    assert unknown["features"][0]["status"] == "dispatch_unknown"
    assert "secret" not in json.dumps(unknown)
    restored = IncrementalDelivery(
        ProductDeliveryStore(ProductStore(store.products.path)), workflows
    )
    reconciled = await restored.reconcile(product["id"], "owner", scm)
    assert reconciled == unknown
    with pytest.raises(ProductConflict):
        await restored.start(product["id"], "owner", approved(unknown["revision"]), scm)
    assert len(workflows.calls) == 1


@pytest.mark.asyncio
async def test_explicit_failed_attempt_retry_preserves_original_context(tmp_path):
    product, store, _ = setup(tmp_path)
    workflows, scm = Workflows(), SCM()
    service = IncrementalDelivery(store, workflows)
    await service.start(product["id"], "owner", approved(1), scm)
    first = workflows.calls[0]["workflow_id"]
    original = store.context(product["id"], "owner", first)
    workflows.states[first] = {"phase": "failed"}
    failed = await service.reconcile(product["id"], "owner", scm)
    retried = await service.start(
        product["id"], "owner", approved(failed["revision"]), scm
    )
    assert len(retried["features"][0]["attempts"]) == 2
    assert workflows.calls[1]["workflow_id"] != first
    assert store.context(product["id"], "owner", first) == original


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "state,status",
    [
        (review_state("failure"), "awaiting_review"),
        (review_state("none"), "awaiting_review"),
        ({"phase": "completed"}, "blocked"),
        (
            {"phase": "executing", "pending_decision": {"reason": "review"}},
            "awaiting_decision",
        ),
    ],
)
async def test_model_completion_or_missing_ci_cannot_advance(tmp_path, state, status):
    product, store, _ = setup(tmp_path)
    workflows, scm = Workflows(), SCM()
    scm.merged = True
    service = IncrementalDelivery(store, workflows)
    await service.start(product["id"], "owner", approved(1), scm)
    workflows.states[workflows.calls[0]["workflow_id"]] = state
    result = await service.reconcile(product["id"], "owner", scm)
    assert result["features"][0]["status"] == status
    assert not scm.verifications


def test_capsule_overflow_fails_instead_of_truncating(tmp_path):
    _, _, plan = setup(tmp_path)
    plan["decisions"] = ["X" * 49_000]
    with pytest.raises(ProductConflict, match="48 KB"):
        context_capsule(plan)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "merged,head,branch,comparison,expected",
    [
        (False, SHA, "main", "ahead", "waiting"),
        (True, SHA, "main", "ahead", "merged"),
        (True, SHA, "main", "identical", "merged"),
        (True, BASE, "main", "ahead", "error"),
        (True, SHA, "other", "ahead", "error"),
        (True, SHA, "main", "diverged", "error"),
        (True, SHA, "main", "behind", "error"),
    ],
)
async def test_github_merge_verification_is_read_only_and_checks_evidence(
    merged, head, branch, comparison, expected
):
    requests = []

    def handler(request):
        requests.append(request)
        if "/pulls/" in request.url.path:
            payload = {
                "merged": merged,
                "head": {"sha": head},
                "base": {"ref": branch, "repo": {"full_name": "acme/agenda"}},
                "merge_commit_sha": MERGE,
            }
        elif "/compare/" in request.url.path:
            payload = {"status": comparison}
        else:
            payload = {"object": {"sha": BASE}}
        return httpx.Response(200, json=payload)

    scm = GitHubSCMClient(
        token="test",
        client=httpx.AsyncClient(
            base_url="https://api.github.com", transport=httpx.MockTransport(handler)
        ),
    )
    try:
        if expected == "error":
            with pytest.raises(SCMError):
                await scm.verified_delivery_merge("acme/agenda", "main", 7, SHA)
        else:
            receipt = await scm.verified_delivery_merge("acme/agenda", "main", 7, SHA)
            assert (receipt is None) == (expected == "waiting")
        assert all(
            r.method == "GET" and r.url.host == "api.github.com" for r in requests
        )
    finally:
        await scm.close()


@pytest.mark.asyncio
async def test_internal_preallocated_workflow_id_is_used_by_real_queue():
    queue = InMemoryWorkflowQueue()
    service = WorkflowService(None, Settings(_env_file=None), queue, False)
    identity = str(uuid4())
    assert (
        await service.start("p", "A test request", None, "owner", workflow_id=identity)
        == identity
    )
    access = await queue.get_access(identity)
    assert access.workflow_id == identity


@pytest.mark.asyncio
async def test_delivery_api_authorization_factory_gate_and_no_dispatch_on_get(
    tmp_path, monkeypatch
):
    from app.main import create_app
    import app.api.routes.product_deliveries as routes

    product, store, plan = setup(tmp_path)
    app = create_app()
    app.state.settings = Settings(
        _env_file=None,
        factory_build_profiles_json=build_profiles(),
        api_keys_json=json.dumps(
            {
                "owner-key": {"client_id": "owner", "role": "admin", "projects": ["p"]},
                "other-key": {"client_id": "other", "role": "admin", "projects": ["p"]},
                "viewer-key": {
                    "client_id": "owner",
                    "role": "viewer",
                    "projects": ["p"],
                },
                "wrong-project": {
                    "client_id": "owner",
                    "role": "admin",
                    "projects": ["q"],
                },
            }
        ),
    )
    workflows, scm = Workflows(), SCM()
    app.state.container = SimpleNamespace(
        product_studio=ProductStudio(None, store.products),
        workflow_service=workflows,
        audit_log=InMemoryAuditLog(),
    )

    @asynccontextmanager
    async def fake_scm(required=False):
        yield scm

    monkeypatch.setattr(routes, "scm_client", fake_scm)
    monkeypatch.setattr(
        "app.factory.preflight.github_credential_configured", lambda: True
    )
    path = "/products/" + product["id"] + "/delivery"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        headers = {"X-API-Key": "owner-key"}
        assert (await client.get(path)).status_code == 401
        assert (await client.get(path + "/preflight")).status_code == 401
        assert (
            await client.get(path + "/preflight", headers={"X-API-Key": "other-key"})
        ).status_code == 404
        assert (
            await client.get(path + "/preflight", headers={"X-API-Key": "viewer-key"})
        ).status_code == 403
        assert (
            await client.get(
                path + "/preflight", headers={"X-API-Key": "wrong-project"}
            )
        ).status_code == 403
        assert (
            await client.get(path, headers={"X-API-Key": "other-key"})
        ).status_code == 404
        assert (
            await client.get(path, headers={"X-API-Key": "wrong-project"})
        ).status_code == 403
        for route, body in [
            (path, definition().model_dump()),
            (path + "/append", {"revision": 1, "decisions": ["new"]}),
            (path + "/start", approved(1).model_dump()),
            (path + "/reconcile", {}),
        ]:
            response = await client.request(
                "PUT" if route == path else "POST",
                route,
                json=body,
                headers={"X-API-Key": "viewer-key"},
            )
            assert response.status_code == 403
        assert (await client.get(path, headers=headers)).json()["plan"] == plan
        assert not workflows.calls
        assert (
            await client.post(
                path + "/start", json=approved(1).model_dump(), headers=headers
            )
        ).status_code == 409
        app.state.settings = app.state.settings.model_copy(
            update={"factory_mode_enabled": True}
        )
        checked = await client.get(path + "/preflight", headers=headers)
        assert checked.status_code == 200
        assert checked.headers["cache-control"] == "no-store"
        assert checked.json()["can_start"] is True
        assert checked.json()["revision"] == 1
        assert store.get(product["id"], "owner") == plan
        assert not workflows.calls and not scm.bases
        workflows.health["ready"] = False
        blocked = await client.post(
            path + "/start", json=approved(1).model_dump(), headers=headers
        )
        assert blocked.status_code == 409
        assert blocked.json()["detail"]["code"] == "delivery_preflight_blocked"
        assert not workflows.calls and not scm.bases
        assert store.get(product["id"], "owner") == plan
        workflows.health["ready"] = True
        started = await client.post(
            path + "/start", json=approved(1).model_dump(), headers=headers
        )
        assert started.status_code == 202
        workflow_id = started.json()["plan"]["features"][0]["attempts"][0][
            "workflow_id"
        ]
        UUID(workflow_id)
        assert (
            await client.get(path + "/context/" + workflow_id, headers=headers)
        ).status_code == 200
        assert (
            await client.get(
                path + "/context/" + workflow_id, headers={"X-API-Key": "other-key"}
            )
        ).status_code == 404
        assert (
            await client.get(path + "/context/" + str(uuid4()), headers=headers)
        ).status_code == 404
        assert len(workflows.calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "updates,code",
    [
        ({"factory_mode_enabled": False}, "factory_enabled"),
        ({"factory_command_backend": "local"}, "sandbox_policy"),
        ({"factory_approved_scm_hosts_json": '["example.com"]'}, "scm_host"),
        ({"factory_build_profiles_json": "{}"}, "build_profile"),
        ({"factory_build_profiles_json": '{"broken": {}}'}, "build_profile"),
        (
            {"factory_repository_profiles_json": '{"acme/agenda": "unknown"}'},
            "build_profile",
        ),
    ],
)
async def test_preflight_configuration_blockers(tmp_path, monkeypatch, updates, code):
    from app.factory.preflight import delivery_preflight

    monkeypatch.setattr(
        "app.factory.preflight.github_credential_configured", lambda: True
    )
    product, store, plan = setup(tmp_path)
    settings = Settings(
        _env_file=None, factory_build_profiles_json=build_profiles()
    ).model_copy(update={"factory_mode_enabled": True, **updates})
    workflows = Workflows()
    report = await delivery_preflight(plan, settings, workflows)
    assert not report.can_start
    assert any(c.code == code and c.status == "block" for c in report.checks)
    assert store.get(product["id"], "owner") == plan
    assert not workflows.calls


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "hosts,expected",
    [
        (["github.com"], "pass"),
        ([" GITHUB.COM. "], "pass"),
        (["example.com", "github.com"], "pass"),
        (["github.com.attacker.example"], "block"),
        (["notgithub.com"], "block"),
        (["attacker.github.com"], "block"),
        ([], "block"),
    ],
)
async def test_preflight_requires_exact_approved_github_host(
    tmp_path, monkeypatch, hosts, expected
):
    from app.factory.preflight import delivery_preflight

    monkeypatch.setattr(
        "app.factory.preflight.github_credential_configured", lambda: True
    )
    product, store, plan = setup(tmp_path)
    settings = Settings(
        _env_file=None, factory_build_profiles_json=build_profiles()
    ).model_copy(update={
        "factory_mode_enabled": True,
        "factory_approved_scm_hosts_json": json.dumps(hosts),
    })
    workflows = Workflows()
    report = await delivery_preflight(plan, settings, workflows)
    host_check = next(check for check in report.checks if check.code == "scm_host")
    assert host_check.status == expected
    assert report.can_start is (expected == "pass")
    assert store.get(product["id"], "owner") == plan
    assert not workflows.calls


@pytest.mark.asyncio
async def test_preflight_explicit_mapping_autodetect_and_network(tmp_path, monkeypatch):
    from app.factory.preflight import delivery_preflight

    monkeypatch.setattr(
        "app.factory.preflight.github_credential_configured", lambda: True
    )
    _, _, plan = setup(tmp_path)
    settings = Settings(
        _env_file=None, factory_build_profiles_json=build_profiles()
    ).model_copy(update={"factory_mode_enabled": True})
    workflows = Workflows()
    report = await delivery_preflight(plan, settings, workflows)
    assert report.can_start
    assert (
        next(c for c in report.checks if c.code == "build_profile").status == "warning"
    )
    assert len(report.not_checked) == 3
    settings = settings.model_copy(
        update={"factory_repository_profiles_json": '{"ACME/Agenda": "python-checks"}'}
    )
    report = await delivery_preflight(plan, settings, workflows)
    assert report.can_start
    assert next(c for c in report.checks if c.code == "build_profile").status == "pass"
    plan["build_profile"] = "unknown"
    assert not (await delivery_preflight(plan, settings, workflows)).can_start
    plan["build_profile"] = "python-checks"
    profiles = json.loads(build_profiles())
    profiles["python-checks"]["phases"].insert(
        0,
        {
            "name": "prepare",
            "argv": ["/usr/local/bin/python", "-m", "pip", "install", "."],
            "network": "dependencies",
        },
    )
    settings = settings.model_copy(
        update={"factory_build_profiles_json": json.dumps(profiles)}
    )
    assert not (await delivery_preflight(plan, settings, workflows)).can_start
    settings = settings.model_copy(update={"factory_sandbox_network_enabled": True})
    assert (await delivery_preflight(plan, settings, workflows)).can_start


@pytest.mark.asyncio
async def test_preflight_runtime_failure_timeout_and_unsupported_workers(
    tmp_path, monkeypatch
):
    from app.factory.preflight import delivery_preflight

    monkeypatch.setattr(
        "app.factory.preflight.github_credential_configured", lambda: True
    )
    _, _, plan = setup(tmp_path)
    settings = Settings(
        _env_file=None, factory_build_profiles_json=build_profiles()
    ).model_copy(update={"factory_mode_enabled": True})
    workflows = Workflows()
    workflows.health["embedded_workers_enabled"] = False
    assert not (await delivery_preflight(plan, settings, workflows)).can_start
    settings = settings.model_copy(update={"workflow_queue_backend": "postgres"})
    assert (await delivery_preflight(plan, settings, workflows)).can_start

    async def fails():
        raise RuntimeError("secret-token-and-provider-response")

    workflows.readiness = fails
    report = await delivery_preflight(plan, settings, workflows)
    assert not report.can_start
    assert "secret-token" not in report.model_dump_json()

    async def slow():
        await asyncio.sleep(10)
        return {"ready": True}

    workflows.readiness = slow
    monkeypatch.setattr("app.factory.preflight.HEALTH_TIMEOUT_SECONDS", 0.01)
    report = await delivery_preflight(plan, settings, workflows)
    assert not report.can_start
    assert any(c.code == "runtime_health" for c in report.checks)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "state",
    [
        "running",
        "dispatch_unknown",
        "awaiting_review",
        "merged",
        "attempt_limit",
        "context_limit",
        "credential_missing",
    ],
)
async def test_preflight_plan_and_credential_blockers(tmp_path, monkeypatch, state):
    from app.factory.preflight import delivery_preflight

    monkeypatch.setattr(
        "app.factory.preflight.github_credential_configured",
        lambda: state != "credential_missing",
    )
    _, _, plan = setup(tmp_path)
    settings = Settings(
        _env_file=None, factory_build_profiles_json=build_profiles()
    ).model_copy(update={"factory_mode_enabled": True})
    if state == "attempt_limit":
        plan["features"][0]["attempts"] = [
            {"workflow_id": str(uuid4()), "status": "failed"}
        ] * 3
    elif state == "context_limit":
        plan["original_brief"] = {"text": "a" * 49_000}
    elif state != "credential_missing":
        for feature_row in plan["features"]:
            feature_row["status"] = state
    report = await delivery_preflight(plan, settings, Workflows())
    assert not report.can_start


def test_preflight_credential_presence_does_not_read_private_key_file(monkeypatch):
    from app.factory.preflight import github_credential_configured

    for key in [
        "GITHUB_TOKEN",
        "GITHUB_APP_ID",
        "GITHUB_APP_INSTALLATION_ID",
        "GITHUB_APP_PRIVATE_KEY",
        "GITHUB_APP_PRIVATE_KEY_PATH",
    ]:
        monkeypatch.delenv(key, raising=False)
    assert not github_credential_configured()
    monkeypatch.setenv("GITHUB_APP_ID", "1")
    monkeypatch.setenv("GITHUB_APP_INSTALLATION_ID", "2")
    monkeypatch.setenv("GITHUB_APP_PRIVATE_KEY_PATH", "/nonexistent/no-read")
    assert github_credential_configured()  # Presence, not file validity or permission.
