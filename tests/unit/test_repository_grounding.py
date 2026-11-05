from pathlib import Path

import pytest

from app.agents.tools import SearchRepositoryTool
from app.infrastructure.repository_grounding import RepositoryGroundingCollector


def test_repository_grounding_collector_returns_real_file_evidence(tmp_path: Path):
    (tmp_path / "app").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "README.md").write_text(
        "# Forgehand\n\nFastAPI app\n", encoding="utf-8"
    )
    (tmp_path / "app" / "main.py").write_text(
        "from fastapi import FastAPI\n\napp = FastAPI()\n",
        encoding="utf-8",
    )
    (tmp_path / "tests" / "test_api.py").write_text(
        "def test_api():\n    assert True\n",
        encoding="utf-8",
    )

    collector = RepositoryGroundingCollector(
        str(tmp_path),
        max_files=4,
        max_excerpt_lines=10,
        max_file_bytes=8_000,
    )

    grounding = collector.collect("analisar fastapi app")

    assert grounding["repo_root"] == str(tmp_path.resolve())
    assert grounding["require_citations"] is True
    assert grounding["evidence"]
    paths = {item["path"] for item in grounding["evidence"]}
    assert "app/main.py" in paths
    assert "README.md" in paths
    assert any("fastapi" in item["excerpt"].lower() for item in grounding["evidence"])


async def test_node_fixture_source_and_test_conventions_are_discoverable():
    root = Path(__file__).resolve().parents[2] / "benchmarks/factory/fixtures/node"
    grounding = RepositoryGroundingCollector(str(root)).collect(
        "Exporte uniqueTags em catalog.cjs e adicione testes."
    )
    evidence = {item["path"]: item["excerpt"] for item in grounding["evidence"]}

    assert "function retail(price)" in evidence.get("catalog.cjs", "")
    assert "require('node:assert/strict')" in evidence.get("tests/catalog.test.cjs", "")
    assert "require('node:test')" in evidence["tests/catalog.test.cjs"]

    matches = await SearchRepositoryTool(str(root)).run({"pattern": "retail"})
    assert "catalog.cjs:1: function retail(price)" in matches
    assert "tests/catalog.test.cjs:3:" in matches


@pytest.mark.parametrize(
    "extension", ["js", "cjs", "mjs", "jsx", "ts", "cts", "mts", "tsx"]
)
async def test_javascript_and_typescript_source_discovery(
    tmp_path: Path, extension: str
):
    source = "export const normalizeTag = (tag) => tag.trim();\n"
    path = f"normalize.{extension}"
    (tmp_path / path).write_text(source, encoding="utf-8")

    grounding = RepositoryGroundingCollector(str(tmp_path)).collect("normalizeTag")
    assert [item["path"] for item in grounding["evidence"]] == [path]
    assert grounding["evidence"][0]["excerpt"] == source.rstrip("\n")

    matches = await SearchRepositoryTool(str(tmp_path)).run({"pattern": "normalizeTag"})
    assert f"{path}:1: {source.rstrip()}" in matches
