import hashlib
import json
import os

import pytest

from app.factory.architecture import architecture_feedback, check_architecture
from app.factory.build_strategy import BuildProfileRegistry
from app.models.architecture import ArchitecturePolicy
from app.models.build import BuildPhase, BuildProfile
from app.models.factory import BuildProfileSelection

# Factory mode é POSIX por design (lock fcntl, dir_fd/O_NOFOLLOW, grupo de
# processos, caminhos de lease em /): no Windows só o mission control roda.
pytestmark = pytest.mark.skipif(os.name != "posix", reason="factory mode exige POSIX")


def policy(**updates):
    return ArchitecturePolicy.model_validate(
        {
            "rules": [
                {
                    "id": "domain-isolation",
                    "source": "app.domain",
                    "forbidden": ["app.infrastructure", "requests"],
                    "remediation": "Dependa de uma interface do domínio e injete o adaptador externamente.",
                }
            ],
            **updates,
        }
    )


def source(root, text, path="app/domain/service.py"):
    target = root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text)
    return target


@pytest.mark.parametrize(
    "statement,dependency",
    [
        ("import app.infrastructure.database", "app.infrastructure.database"),
        ("import requests as http", "requests"),
        ("from app.infrastructure import database as db", "app.infrastructure"),
        ("from app import infrastructure", "app.infrastructure"),
        ("from ..infrastructure import database", "app.infrastructure"),
        ("from .. import infrastructure", "app.infrastructure"),
        ("def run():\n    import requests", "requests"),
        ("if False:\n    import requests", "requests"),
    ],
)
def test_forbidden_static_imports_are_located(tmp_path, statement, dependency):
    source(tmp_path, statement)
    report = check_architecture(tmp_path, policy())
    assert report.complete and not report.passed
    finding = report.findings[0]
    assert finding.rule_id == "domain-isolation"
    assert finding.path == "app/domain/service.py"
    assert finding.line in {1, 2}
    assert finding.dependency == dependency
    assert "interface" in finding.remediation
    feedback = architecture_feedback(report)[0]
    assert feedback["passed"] is False
    assert "service.py:" in feedback["details"]


def test_allowed_imports_comments_strings_and_no_execution(tmp_path):
    source(
        tmp_path,
        'import app.infrastructure_extra\nimport requests_cache\n# import requests\ntext="import requests"\nraise RuntimeError("DO NOT EXECUTE")',
    )
    report = check_architecture(tmp_path, policy())
    assert report.passed and report.files_checked == 1
    assert architecture_feedback(report)[0]["passed"]


def test_src_layout_and_package_relative_import(tmp_path):
    source(tmp_path, "from .. import infrastructure", "src/app/domain/__init__.py")
    report = check_architecture(tmp_path, policy(source_roots=["src"]))
    assert report.findings[0].dependency == "app.infrastructure"
    assert report.findings[0].path == "src/app/domain/__init__.py"


@pytest.mark.parametrize(
    "statement",
    [
        "from app import *",
        "from .... import outside",
        "__import__('requests')",
        "import importlib as loader\nloader.import_module('requests')",
        "from importlib import import_module as load\nload('requests')",
    ],
)
def test_unsupported_imports_do_not_silently_pass(tmp_path, statement):
    source(tmp_path, statement)
    report = check_architecture(tmp_path, policy())
    assert not report.passed
    assert any(f.code == "unsupported_import" for f in report.findings)


@pytest.mark.parametrize(
    "kind",
    [
        "syntax",
        "empty",
        "unmatched",
        "link_file",
        "link_directory",
        "fifo",
        "hardlink",
        "missing_root",
    ],
)
def test_invalid_or_unsafe_source_fails_closed(tmp_path, kind):
    if kind == "syntax":
        source(tmp_path, "def secret_not_in_report(:")
    elif kind == "unmatched":
        source(tmp_path, "import os", "other.py")
    elif kind in {"link_file", "link_directory"}:
        target = source(tmp_path, "import os")
        (target.parent / ("link.py" if kind == "link_file" else "linked")).symlink_to(
            target if kind == "link_file" else target.parent
        )
    elif kind == "fifo":
        target = source(tmp_path, "import os")
        os.mkfifo(target.parent / "pipe.py")
    elif kind == "hardlink":
        target = source(tmp_path, "import os")
        os.link(target, target.parent / "linked.py")
    p = policy(source_roots=["missing"]) if kind == "missing_root" else policy()
    report = check_architecture(tmp_path, p)
    assert not report.passed
    assert "secret_not_in_report" not in report.model_dump_json()
    assert str(tmp_path) not in report.model_dump_json()


@pytest.mark.parametrize(
    "limit,value",
    [
        ("MAX_FILES", 0),
        ("MAX_ENTRIES", 0),
        ("MAX_FILE_BYTES", 2),
        ("MAX_BYTES", 2),
        ("MAX_DEPTH", 0),
        ("MAX_SECONDS", 0),
    ],
)
def test_limits_never_return_success(tmp_path, monkeypatch, limit, value):
    source(tmp_path, "import os")
    monkeypatch.setattr("app.factory.architecture." + limit, value)
    report = check_architecture(tmp_path, policy())
    assert not report.complete and not report.passed


def test_diagnostic_limit_is_bounded_and_incomplete(tmp_path):
    source(tmp_path, "import requests\n" * 70)
    report = check_architecture(tmp_path, policy())
    assert len(report.findings) == 50
    assert not report.complete and not report.passed


@pytest.mark.parametrize(
    "roots", [["../outside"], ["/tmp"], ["app//domain"], [".", "src"], ["src", "src"]]
)
def test_policy_roots_are_confined_and_nonoverlapping(roots):
    with pytest.raises(ValueError):
        policy(source_roots=roots)


def test_profile_identity_and_policy_drift():
    original = BuildProfile(
        name="python",
        ecosystem="python",
        image="python@sha256:" + "a" * 64,
        phases=(
            BuildPhase(name="test", argv=("/usr/local/bin/python", "-m", "pytest")),
        ),
    )
    legacy = original.model_dump(mode="json")
    legacy.pop("architecture")
    legacy.pop("acceptance")
    assert (
        original.fingerprint()
        == hashlib.sha256(
            json.dumps(legacy, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
    )
    configured = BuildProfile.model_validate(
        {**original.model_dump(), "architecture": policy().model_dump()}
    )
    assert configured.fingerprint() != original.fingerprint()
    registry = BuildProfileRegistry({"python": configured})
    selection = BuildProfileSelection(
        selected_profile="python",
        selection_reason="explicit",
        phases=["test"],
        profile_digest=configured.fingerprint(),
        architecture_digest=policy().fingerprint(),
    )
    assert registry.profile_for(selection).architecture == policy()
    with pytest.raises(ValueError):
        registry.profile_for(selection.model_copy(update={"architecture_digest": None}))
    changed = configured.model_copy(
        update={"architecture": policy(source_roots=["src"])}
    )
    with pytest.raises(ValueError):
        BuildProfileRegistry({"python": changed}).profile_for(selection)
    with pytest.raises(ValueError):
        BuildProfile.model_validate({**configured.model_dump(), "ecosystem": "node"})
