"""Opt-in: daemon local + imagem Python/Node já instalada e fixada por digest.

Não baixa imagens, não usa credenciais e só remove containers criados pelo runner.
"""

import json
import os
import asyncio
from pathlib import Path

import pytest

from app.factory.build_strategy import BuildProfileRegistry
from app.factory.sandbox import BuildRunCancelled, DockerBuildRunner, DockerCLI
from app.models.build import BuildPhase, BuildProfile
from app.models.build_execution import BuildOutcome, SandboxLimits
from app.models.factory import (
    BuildProfileSelection,
    RepositoryTarget,
    WorkspaceLease,
    WorkspaceLifecycle,
)


pytestmark = pytest.mark.skipif(
    os.getenv("RUN_FACTORY_DOCKER_TESTS") != "1",
    reason="requires explicit Docker integration opt-in and preloaded digest-pinned images",
)


@pytest.mark.asyncio
async def test_node_observes_updated_file_between_builds(tmp_path):
    (tmp_path / "catalog.cjs").write_text("module.exports = {};\n")
    (tmp_path / "probe.cjs").write_text(
        "console.log(JSON.stringify(require('./catalog.cjs')));\n"
    )
    fixtures = Path(__file__).resolve().parents[2] / "benchmarks/factory"
    profile = BuildProfile.model_validate(
        json.loads((fixtures / "profiles.json").read_text())["node-fixture"]
    )
    profile = profile.model_copy(
        update={
            "phases": (
                BuildPhase(name="test", argv=("/usr/local/bin/node", "probe.cjs")),
            )
        }
    )
    selected = BuildProfileSelection(
        selected_profile=profile.name,
        selection_reason="explicit",
        phases=["test"],
        profile_digest=profile.fingerprint(),
    )
    lease = WorkspaceLease(
        workflow_id="visibility",
        repository=RepositoryTarget(full_name="fixture/test"),
        local_path=str(tmp_path),
        branch="forgehand/test",
        base_sha="a" * 40,
        state=WorkspaceLifecycle.ACTIVE,
    )
    runner = DockerBuildRunner(
        BuildProfileRegistry({profile.name: profile}),
        DockerCLI(
            socket_path=os.getenv("FACTORY_DOCKER_SOCKET", "/var/run/docker.sock")
        ),
    )
    before = await runner.run(lease, selected)
    assert before.phases[0].stdout.strip() == "{}"
    from app.infrastructure.workspace_runtime import LocalWorkspaceRuntime

    LocalWorkspaceRuntime._apply_operation(
        "create",
        tmp_path / "catalog.cjs",
        {"content": "module.exports = {};\nmodule.exports.value = 2;\n"},
    )
    after = await runner.run(lease, selected)
    assert json.loads(after.phases[0].stdout) == {"value": 2}, after.model_dump_json()


@pytest.mark.asyncio
async def test_qualification_preflight_runs_both_pinned_images():
    from app.evaluation.factory_qualification import sandbox_preflight

    assert await sandbox_preflight(
        Path(__file__).resolve().parents[2] / "benchmarks/factory",
        os.getenv("FACTORY_DOCKER_SOCKET", "/var/run/docker.sock"),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("case_index", range(5))
async def test_independent_checks_reject_base_and_accept_reference_fix(
    tmp_path, case_index
):
    from app.evaluation.factory_fixtures import prepare_fixture

    fixtures = Path(__file__).resolve().parents[2] / "benchmarks/factory"
    case = json.loads((fixtures / "cases.json").read_text())[case_index]
    ecosystem = case["ecosystem"]
    root, sha = prepare_fixture(ecosystem, tmp_path, fixtures)
    extension = "py" if ecosystem == "python" else "cjs"
    script = f"__forgehand_verify.{extension}"
    (root / script).write_text(
        (fixtures / "hidden" / f"{ecosystem}.{extension}").read_text()
    )
    profile = BuildProfile.model_validate(
        json.loads((fixtures / "profiles.json").read_text())[f"{ecosystem}-fixture"]
    )
    profile = profile.model_copy(
        update={
            "phases": (
                BuildPhase(
                    name="test",
                    argv=(
                        f"/usr/local/bin/{'python' if ecosystem == 'python' else 'node'}",
                        script,
                        case["hidden_case"],
                    ),
                ),
            )
        }
    )
    runner = DockerBuildRunner(
        BuildProfileRegistry({profile.name: profile}),
        DockerCLI(
            socket_path=os.getenv("FACTORY_DOCKER_SOCKET", "/var/run/docker.sock")
        ),
    )
    selected = BuildProfileSelection(
        selected_profile=profile.name,
        selection_reason="explicit",
        phases=["test"],
        profile_digest=profile.fingerprint(),
    )
    lease = WorkspaceLease(
        workflow_id=f"oracle-{case_index}",
        repository=RepositoryTarget(full_name="fixture/test"),
        local_path=str(root),
        branch="forgehand/test",
        base_sha=sha,
        state=WorkspaceLifecycle.ACTIVE,
    )
    baseline = await runner.run(lease, selected)
    assert baseline.outcome == BuildOutcome.COMMAND_FAILURE, baseline.model_dump_json()
    if case["hidden_case"] == "defect":
        path = root / "orders.py"
        path.write_text(
            path.read_text().replace(
                "round(sum(prices), 2)", "round(sum(prices) * (1-discount), 2)"
            )
        )
    elif case["hidden_case"] == "tests":
        (root / "tests/test_regression.py").write_text(
            "import unittest\nfrom orders import line_total\nclass Regression(unittest.TestCase):\n def test_zero(self): self.assertEqual(line_total(4,0),0)\n def test_fraction(self): self.assertEqual(line_total(1.25,3),3.75)\n def test_negative(self):\n  with self.assertRaises(ValueError): line_total(4,-1)\n"
        )
    elif case["hidden_case"] == "configuration":
        (root / "config.json").write_text('{"currency":"EUR"}')
        path = root / "README.md"
        path.write_text(path.read_text().replace("USD", "EUR"))
    elif case["hidden_case"] == "feature":
        from app.infrastructure.workspace_runtime import LocalWorkspaceRuntime

        path = root / "catalog.cjs"
        LocalWorkspaceRuntime._apply_operation(
            "create",
            path,
            {
                "content": path.read_text()
                + "\nmodule.exports.uniqueTags = tags => [...new Set(tags.map(tag => tag.trim().toLowerCase()).filter(Boolean))];\n"
            },
        )
    else:
        (root / "catalog.cjs").write_text(
            "function tax(price,rate){return Math.round(price*rate*100)/100;}\nmodule.exports={retail:price=>tax(price,1.2),wholesale:price=>tax(price,1.1)};\n"
        )
    fixed = await runner.run(lease, selected)
    assert fixed.outcome == BuildOutcome.SUCCESS, fixed.model_dump_json()


@pytest.mark.asyncio
@pytest.mark.parametrize("ecosystem", ["python", "node"])
async def test_real_sandbox_build_and_isolation(tmp_path, monkeypatch, ecosystem):
    image = os.getenv(f"FACTORY_DOCKER_{ecosystem.upper()}_TEST_IMAGE")
    if not image:
        pytest.skip(f"no preloaded {ecosystem} image configured")
    monkeypatch.setenv("GITHUB_TOKEN", "host-secret-must-not-enter")
    monkeypatch.setenv("OPENAI_API_KEY", "host-secret-must-not-enter")
    if ecosystem == "python":
        script = "probe.py"
        (tmp_path / script).write_text(
            "import json, os, pathlib, socket\n"
            "assert not os.getenv('GITHUB_TOKEN')\n"
            "assert not os.getenv('OPENAI_API_KEY')\n"
            "assert not os.getenv('PYTHONPATH')\n"
            "assert os.getuid() != 0\n"
            "try:\n"
            "    pathlib.Path('/outside-lease').write_text('forbidden')\n"
            "except OSError:\n"
            "    pass\n"
            "else:\n"
            "    raise AssertionError('root filesystem is writable')\n"
            "assert len(pathlib.Path('/proc/net/route').read_text().splitlines()) == 1\n"
            "pathlib.Path('created.txt').write_text('python build passed')\n"
            "print(json.dumps({'passed': True}))\n",
            encoding="utf-8",
        )
        executable = "/usr/local/bin/python"
    else:
        script = "probe.js"
        (tmp_path / script).write_text(
            "const fs = require('node:fs');\n"
            "const assert = require('node:assert/strict');\n"
            "assert.equal(process.env.GITHUB_TOKEN, undefined);\n"
            "assert.equal(process.env.OPENAI_API_KEY, undefined);\n"
            "assert.equal(process.env.NODE_OPTIONS, undefined);\n"
            "assert.notEqual(process.getuid(), 0);\n"
            "assert.throws(() => fs.writeFileSync('/outside-lease', 'forbidden'));\n"
            "assert.equal(fs.readFileSync('/proc/net/route', 'utf8').trim().split('\\n').length, 1);\n"
            "fs.writeFileSync('created.txt', 'node build passed');\n"
            "console.log(JSON.stringify({passed: true}));\n",
            encoding="utf-8",
        )
        executable = "/usr/local/bin/node"
    profile = BuildProfile(
        name=f"{ecosystem}-fixture",
        ecosystem=ecosystem,
        image=image,
        phases=(BuildPhase(name="test", argv=(executable, script)),),
    )
    selection = BuildProfileSelection(
        selected_profile=profile.name,
        selection_reason="explicit",
        phases=["test"],
        profile_digest=profile.fingerprint(),
    )
    lease = WorkspaceLease(
        workflow_id=f"docker-{ecosystem}",
        repository=RepositoryTarget(full_name="fixture/test"),
        local_path=str(tmp_path),
        branch="forgehand/test",
        base_sha="a" * 40,
        state=WorkspaceLifecycle.READY,
    )
    execution = DockerBuildRunner(
        BuildProfileRegistry({profile.name: profile}),
        DockerCLI(
            socket_path=os.getenv("FACTORY_DOCKER_SOCKET", "/var/run/docker.sock")
        ),
    )
    result = await execution.run(lease, selection)
    assert result.outcome == BuildOutcome.SUCCESS, result.model_dump_json()
    assert json.loads(result.phases[0].stdout)["passed"] is True
    assert (tmp_path / "created.txt").is_file()
    assert execution.active_containers == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("ecosystem", ["python", "node"])
async def test_versioned_fixture_profiles_build_in_real_sandbox(tmp_path, ecosystem):
    from app.evaluation.factory_fixtures import prepare_fixture

    fixtures = Path(__file__).resolve().parents[2] / "benchmarks/factory"
    workspace, sha = prepare_fixture(ecosystem, tmp_path, fixtures)
    profiles = json.loads((fixtures / "profiles.json").read_text())
    profile = BuildProfile.model_validate(profiles[f"{ecosystem}-fixture"])
    lease = WorkspaceLease(
        workflow_id=f"fixture-{ecosystem}",
        repository=RepositoryTarget(full_name="fixture/test"),
        local_path=str(workspace),
        branch="forgehand/test",
        base_sha=sha,
        state=WorkspaceLifecycle.ACTIVE,
    )
    runner = DockerBuildRunner(
        BuildProfileRegistry({profile.name: profile}),
        DockerCLI(
            socket_path=os.getenv("FACTORY_DOCKER_SOCKET", "/var/run/docker.sock")
        ),
    )
    selection = BuildProfileSelection(
        selected_profile=profile.name,
        selection_reason="explicit",
        phases=[p.name.value for p in profile.phases],
        profile_digest=profile.fingerprint(),
    )
    report = await runner.run(lease, selection)
    assert report.outcome == BuildOutcome.SUCCESS, report.model_dump_json()
    assert [phase.phase.value for phase in report.phases] == ["build", "test"]
    assert not runner.active_containers


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "scenario", ["memory", "timeout", "truncation", "ordering", "cancel", "network"]
)
async def test_real_sandbox_resource_and_lifecycle(tmp_path, scenario):
    image = os.getenv("FACTORY_DOCKER_PYTHON_TEST_IMAGE")
    if not image:
        pytest.skip("no Python image")
    scripts = {
        "memory": "chunks=[]\nwhile True: chunks.append(bytearray(8*1024*1024))\n",
        "timeout": "import time\ntime.sleep(60)\n",
        "cancel": "import pathlib,time\npathlib.Path('started').touch()\ntime.sleep(60)\n",
        "truncation": "print('x'*200000)\n",
        "ordering": "from pathlib import Path\nassert Path('prepared').read_text() == 'ready'\n",
        "network": "import socket\nsocket.setdefaulttimeout(1)\ntry:\n socket.create_connection(('1.1.1.1',443))\nexcept OSError:\n pass\nelse:\n raise AssertionError('network available')\n",
    }
    (tmp_path / "probe.py").write_text(scripts[scenario])
    (tmp_path / "prepare.py").write_text(
        "from pathlib import Path\nPath('prepared').write_text('ready')\n"
    )
    phases = [
        BuildPhase(
            name="test",
            argv=("/usr/local/bin/python", "probe.py"),
            timeout_seconds=1 if scenario == "timeout" else 30,
            output_limit=256,
        )
    ]
    if scenario == "ordering":
        phases.insert(
            0, BuildPhase(name="prepare", argv=("/usr/local/bin/python", "prepare.py"))
        )
    profile = BuildProfile(
        name="python-probe", ecosystem="python", image=image, phases=tuple(phases)
    )
    selected = BuildProfileSelection(
        selected_profile=profile.name,
        selection_reason="explicit",
        phases=[p.name.value for p in phases],
        profile_digest=profile.fingerprint(),
    )
    lease = WorkspaceLease(
        workflow_id=f"probe-{scenario}",
        repository=RepositoryTarget(full_name="fixture/test"),
        local_path=str(tmp_path),
        branch="forgehand/test",
        base_sha="a" * 40,
        state=WorkspaceLifecycle.ACTIVE,
    )
    execution = DockerBuildRunner(
        BuildProfileRegistry({profile.name: profile}),
        DockerCLI(
            socket_path=os.getenv("FACTORY_DOCKER_SOCKET", "/var/run/docker.sock")
        ),
        limits=SandboxLimits(memory_mib=64),
    )
    task = asyncio.create_task(execution.run(lease, selected))
    if scenario == "cancel":
        async with asyncio.timeout(20):
            while not (tmp_path / "started").exists():
                await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(BuildRunCancelled):
            await task
    else:
        result = await task
        expected = {
            "memory": BuildOutcome.RESOURCE_LIMIT,
            "timeout": BuildOutcome.TIMEOUT,
        }.get(scenario, BuildOutcome.SUCCESS)
        assert result.outcome == expected, result.model_dump_json()
        assert not any(p.cleanup_failed for p in result.phases)
        if scenario == "truncation":
            assert result.phases[0].output_truncated
            assert len(result.phases[0].stdout) <= 256
        if scenario == "ordering":
            assert [p.phase.value for p in result.phases] == ["prepare", "test"]
    assert execution.active_containers == {}
