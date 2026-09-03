from datetime import datetime, timezone

import pytest

from app.factory.intake import (
    GitHubIssueWorkOrderInput,
    normalize_github_issue_work_order,
    parse_github_issue_url,
)
from app.models.factory import GitHubIssueSnapshot


def test_parse_github_issue_url() -> None:
    repository, number, host = parse_github_issue_url(
        "https://github.com/acme/widgets/issues/42", ["github.com"]
    )

    assert (repository, number, host) == ("acme/widgets", 42, "github.com")


@pytest.mark.parametrize(
    "url",
    [
        "http://github.com/acme/widgets/issues/1",
        "https://evil.test/acme/widgets/issues/1",
        "https://user:pass@github.com/acme/widgets/issues/1",
        "https://github.com:8443/acme/widgets/issues/1",
        "https://github.com/acme/widgets/pull/1",
        "https://github.com/acme/widgets/issues/1?token=secret",
        "https://github.com/acme/widgets/issues/0",
    ],
)
def test_parse_github_issue_url_rejects_unsafe_or_malformed_urls(url: str) -> None:
    with pytest.raises(ValueError):
        parse_github_issue_url(url, ["github.com"])


def test_issue_snapshot_becomes_reproducible_work_order() -> None:
    snapshot = GitHubIssueSnapshot(
        url="https://github.com/acme/widgets/issues/42",
        number=42,
        title="Corrigir total",
        body="O desconto é aplicado duas vezes.",
        labels=["bug"],
        repository="acme/widgets",
        author="octocat",
        updated_at=datetime.now(timezone.utc),
    )

    order = normalize_github_issue_work_order(
        GitHubIssueWorkOrderInput(
            issue_url=str(snapshot.url),
            acceptance_criteria=["Teste de regressão passa"],
        ),
        snapshot,
    )

    assert order.repository.full_name == "acme/widgets"
    assert order.source.kind == "github_issue"
    assert "desconto" in order.requested_outcome
