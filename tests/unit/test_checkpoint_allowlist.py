"""O checkpoint precisa desserializar critérios tipados sem bloquear nada.

Nos logs das rodadas reais: "Blocked deserialization of
app.models.task.CriterionKind - not in allowed_msgpack_modules".
"""

from __future__ import annotations

import logging

from app.graph.workflow import DOMAIN_TYPES, build_serde
from app.models.task import AcceptanceCriterion, AgentTask, Capability, CriterionKind


def test_typed_criteria_roundtrip_through_checkpoint_serde(caplog) -> None:
    task = AgentTask(
        title="t",
        description="d",
        capability=Capability.RESEARCH,
        acceptance_criteria=[
            AcceptanceCriterion(text="a", kind=CriterionKind.OUTPUT_CONTAINS, pattern="x"),
            AcceptanceCriterion(text="b", kind=CriterionKind.OUTPUT_MIN_CHARS, min_chars=10),
            AcceptanceCriterion(text="c", kind=CriterionKind.FILE_CREATED, path="f.py"),
        ],
    )
    serde = build_serde()
    with caplog.at_level(logging.WARNING):
        restored = serde.loads_typed(serde.dumps_typed(task))
    assert "Blocked deserialization" not in caplog.text
    assert isinstance(restored, AgentTask)
    assert [c.kind for c in restored.acceptance_criteria] == [
        CriterionKind.OUTPUT_CONTAINS,
        CriterionKind.OUTPUT_MIN_CHARS,
        CriterionKind.FILE_CREATED,
    ]
    assert restored.acceptance_criteria[1].min_chars == 10


def test_allowlist_declares_every_task_model_type() -> None:
    declared = {name for module, name in DOMAIN_TYPES if module == "app.models.task"}
    assert {"AgentTask", "AcceptanceCriterion", "CriterionKind", "TaskAttempt"} <= declared
