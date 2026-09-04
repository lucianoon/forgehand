"""Black-box acceptance against real, preloaded digest-pinned Python/Node images."""

import json
import os
from pathlib import Path

import pytest

from app.factory.sandbox import DockerBuildRunner, DockerCLI
from app.models.build import AcceptanceSuite, BuildPhase, BuildProfile
from app.models.build_execution import BuildOutcome
from tests.unit.test_factory_sandbox import make_lease
from tests.unit.test_independent_acceptance import CRITERION, selected

pytestmark = pytest.mark.skipif(
    os.getenv("RUN_FACTORY_DOCKER_TESTS") != "1",
    reason="explicit Docker opt-in and preloaded images required",
)


@pytest.mark.asyncio
@pytest.mark.parametrize("ecosystem", ["python", "node"])
async def test_real_behavior_gate_cannot_be_replaced_with_green_tests(
    tmp_path, ecosystem
):
    profiles = json.loads(
        (
            Path(__file__).resolve().parents[2] / "benchmarks/factory/profiles.json"
        ).read_text()
    )
    template = BuildProfile.model_validate(profiles[f"{ecosystem}-fixture"])
    extension, binary = ("py", "python") if ecosystem == "python" else ("cjs", "node")
    script = f"calc.{extension}"
    test_script = f"always_green.{extension}"
    green = (
        "print('all tests passed')\n"
        if ecosystem == "python"
        else "console.log('all tests passed');\n"
    )
    (tmp_path / script).write_text(green)
    (tmp_path / test_script).write_text(green)
    policy = AcceptanceSuite(
        cases=[
            {
                "id": "sum",
                "criterion": CRITERION,
                "command": {
                    "name": "test",
                    "argv": [f"/usr/local/bin/{binary}", script, "2", "3"],
                    "timeout_seconds": 10,
                },
                "expected_stdout": "5\n",
            }
        ]
    )
    profile = template.model_copy(
        update={
            "phases": (
                BuildPhase(name="test", argv=(f"/usr/local/bin/{binary}", test_script)),
            ),
            "acceptance": policy,
        }
    )
    registry, selection = selected(profile, tmp_path)
    runner = DockerBuildRunner(
        registry,
        DockerCLI(
            socket_path=os.getenv("FACTORY_DOCKER_SOCKET", "/var/run/docker.sock")
        ),
    )
    lease = make_lease(tmp_path)
    wrong = await runner.run(lease, selection)
    assert wrong.phases[0].outcome == BuildOutcome.SUCCESS, wrong.model_dump_json()
    assert not wrong.acceptance.passed
    assert wrong.outcome != BuildOutcome.SUCCESS
    correct = (
        "import sys\nprint(int(sys.argv[1]) + int(sys.argv[2]))\n"
        if ecosystem == "python"
        else "console.log(Number(process.argv[2]) + Number(process.argv[3]));\n"
    )
    (tmp_path / script).write_text(correct)
    fixed = await runner.run(lease, selection)
    assert fixed.outcome == BuildOutcome.SUCCESS, fixed.model_dump_json()
    assert fixed.acceptance.passed
    assert fixed.acceptance.cases[0].execution.workspace_read_only
    assert not fixed.acceptance.cases[0].execution.network_enabled
    tamper = (
        "open('candidate-marker', 'w').write('changed')\nprint(5)\n"
        if ecosystem == "python"
        else "require('fs').writeFileSync('candidate-marker', 'changed'); console.log(5);\n"
    )
    (tmp_path / script).write_text(tamper)
    blocked = await runner.run(lease, selection)
    assert blocked.phases[0].outcome == BuildOutcome.SUCCESS
    assert not blocked.acceptance.passed
    assert not (tmp_path / "candidate-marker").exists()
    assert not runner.active_containers
