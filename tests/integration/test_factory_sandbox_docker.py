"""Opt-in: daemon local + imagem Python/Node já instalada e fixada por digest.

Não baixa imagens, não usa credenciais e só remove containers criados pelo runner.
"""

import json
import os

import pytest

from app.factory.build_strategy import BuildProfileRegistry
from app.factory.sandbox import DockerBuildRunner, DockerCLI
from app.models.build import BuildPhase, BuildProfile
from app.models.build_execution import BuildOutcome
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
