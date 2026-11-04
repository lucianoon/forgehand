from app.graph.nodes import _is_unrecoverable_provider_failure
from app.providers.base import NonRetryableProviderError


def test_openrouter_credit_errors_escalate_instead_of_retrying_the_same_payload():
    assert _is_unrecoverable_provider_failure(
        NonRetryableProviderError("x", "openrouter", 402)
    )
    assert _is_unrecoverable_provider_failure(
        NonRetryableProviderError(
            '{"error":{"metadata":{"limit_source":"openrouter_credits"}}}',
            "openrouter",
        )
    )
    assert not _is_unrecoverable_provider_failure(TimeoutError("timeout"))
