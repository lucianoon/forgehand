import pytest

from app.agents.planner import LLMPlanner
from app.providers.base import CompletionResult, Usage


@pytest.mark.asyncio
async def test_planner_repairs_structurally_invalid_dependencies():
    class RepairingRouter:
        def __init__(self):
            self.requests = []

        async def complete(self, tier, request):
            self.requests.append(request)
            invalid = len(self.requests) == 1
            return CompletionResult(
                text="plan",
                parsed={
                    "rationale": "plano corrigido",
                    "tasks": [
                        {
                            "title": "Analisar",
                            "description": "Analisar arquitetura",
                            "capability": "architecture",
                            "acceptance_criteria": ["Entregar riscos"],
                            "depends_on": [0] if invalid else [],
                        }
                    ],
                },
                model="fake",
                provider="fake",
                usage=Usage(input_tokens=10, output_tokens=5),
                cost_usd=0.001,
                latency_ms=1,
            )

    router = RepairingRouter()
    outcome = await LLMPlanner(router).create_plan("Analise", {})

    assert len(router.requests) == 2
    assert "dependências inválidas" in router.requests[1].messages[0].content
    assert len(outcome.plan) == 1
    assert outcome.usage.tokens == 30
    assert outcome.usage.cost_usd == pytest.approx(0.002)
