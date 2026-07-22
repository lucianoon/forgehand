from pathlib import Path

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
