"""Real custom-format dump/restore; only fresh test databases are created/dropped."""

import asyncio
import os
import shutil
import subprocess
from contextlib import asynccontextmanager
from typing import TypedDict
from uuid import uuid4

import pytest
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from app.api.container import checkpointer_context
from app.api.service import WorkflowService
from app.factory.lifecycle import WorkspaceJournal
from app.graph.state import WorkflowPhase
from app.infrastructure.settings import Settings
from app.infrastructure.workflow_queue import PostgresWorkflowQueue, WorkflowDispatchConflict
from app.models.factory import RepositoryTarget, WorkspaceLease, WorkspaceLifecycle
from app.operations import team_backup as operation


class ApprovalState(TypedDict, total=False):
    workflow_id: str
    project_id: str
    owner_client_id: str
    request: str
    phase: str
    approved: str


@asynccontextmanager
async def runtime(dsn):
    settings = Settings(_env_file=None, environment="dev", factory_mode_enabled=False,
                        database_url=dsn, checkpointer_backend="postgres")
    queue = PostgresWorkflowQueue(dsn)
    await queue.setup()
    try:
        async with checkpointer_context(settings) as saver:
            async def prepare(state):
                return {"phase": WorkflowPhase.AWAITING_HUMAN.value}

            async def approval(state):
                decision = interrupt({"reason": "review_restored_delivery", "commit": "a" * 40})
                return {"phase": WorkflowPhase.COMPLETED.value, "approved": decision}

            builder = StateGraph(ApprovalState)
            builder.add_node("prepare", prepare)
            builder.add_node("approval", approval)
            builder.add_edge(START, "prepare")
            builder.add_edge("prepare", "approval")
            builder.add_edge("approval", END)
            graph = builder.compile(checkpointer=saver)
            yield queue, graph, WorkflowService(graph, settings, queue, run_workers=False)
    finally:
        await queue.close()


@pytest.mark.asyncio
async def test_real_backup_restore_preserves_pending_approval_history_owner_and_idempotency(tmp_path):
    dsn = os.getenv("TEST_DATABASE_URL")
    pg_dump = os.getenv("TEAM_BACKUP_PG_DUMP", "pg_dump")
    pg_restore = os.getenv("TEAM_BACKUP_PG_RESTORE", "pg_restore")
    if (os.getenv("RUN_TEAM_BACKUP_TESTS") != "1" or not dsn
            or not shutil.which(pg_dump) or not shutil.which(pg_restore)):
        pytest.skip("requires isolated PostgreSQL admin DSN, client tools and RUN_TEAM_BACKUP_TESTS=1")
    from psycopg import AsyncConnection, sql
    from psycopg.conninfo import make_conninfo

    suffix = uuid4().hex
    source_name, target_name = "backup_source_" + suffix, "backup_restore_" + suffix
    source_dsn, target_dsn = make_conninfo(dsn, dbname=source_name), make_conninfo(dsn, dbname=target_name)
    # This connection stays on the external test database, never either snapshot database.
    async with await AsyncConnection.connect(dsn, autocommit=True) as admin:
        created = []
        try:
            for name in (source_name, target_name):
                await admin.execute(sql.SQL("CREATE DATABASE {} TEMPLATE template0").format(sql.Identifier(name)))
                created.append(name)
            root = tmp_path.resolve() / "team-data"
            root.mkdir(mode=0o700)
            workspace = root / "factory" / "workspaces" / "pending-workflow"
            workspace.mkdir(parents=True)
            (workspace / "change.txt").write_text("approved only after explicit restored decision\n")
            (workspace / ".env").write_text("FIXTURE_VALUE=local-test\n")
            (workspace / ".env.example").write_text("FIXTURE_VALUE=\n")
            git_env = {**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull}

            def git(*args):
                return subprocess.run(["git", "-C", str(workspace), *args], env=git_env,
                                      text=True, capture_output=True, check=True).stdout.strip()

            git("init", "--initial-branch=main")
            git("add", ".")
            git("-c", "user.name=Restore Test", "-c", "user.email=restore@example.test",
                "-c", "commit.gpgsign=false", "commit", "-m", "Local restore fixture")
            original_commit = git("rev-parse", "HEAD")
            assert git("status", "--porcelain") == ""
            journal = WorkspaceJournal(root / "factory" / "control")
            lease = WorkspaceLease(
                workflow_id="pending-workflow", repository=RepositoryTarget(full_name="team/private"),
                local_path=str(workspace), branch="forgehand/restore-test", base_sha="a" * 40,
                state=WorkspaceLifecycle.READY,
            )
            journal.save(lease)
            lease = lease.model_copy(update={"state": WorkspaceLifecycle.RETAINED})
            journal.save(lease)
            history = journal.history(lease.workflow_id)
            journal_bytes = journal.path.read_bytes()
            (root / "audit").mkdir()
            audit = b'{"action":"awaiting_approval","owner":"team-owner","workflow":"pending-workflow"}\n'
            (root / "audit" / "api.jsonl").write_bytes(audit)
            (root / "fixture.env").write_text("FIXTURE_VALUE=preserved-verbatim\n")
            payload = {"request": "review local delivery"}
            admission = dict(project_id="team-project", owner_client_id="team-owner", payload=payload,
                             repository="team/private", idempotency_key="restore-proof")
            config = {"configurable": {"thread_id": lease.workflow_id}}
            async with runtime(source_dsn) as (queue, graph, service):
                namespace = await queue.dispatch_scope()
                admitted = await queue.enqueue_start(workflow_id=lease.workflow_id, **admission)
                job = await queue.dequeue("before-backup", 0.01)
                assert job is not None and admitted == lease.workflow_id
                await service._invoke_job(job)
                assert await queue.acknowledge(job)
                pending = await graph.aget_state(config)
                checkpoints = [snapshot.config async for snapshot in graph.aget_state_history(config)]
                assert pending.interrupts and pending.values["phase"] == WorkflowPhase.AWAITING_HUMAN.value
                # Existing PG sessions refuse an unsafe snapshot, even outside the wrapper.
                with pytest.raises(operation.BackupError, match="other client sessions"):
                    await asyncio.to_thread(operation.backup, source_dsn, root, tmp_path / "denied", pg_dump=pg_dump)
                assert not (tmp_path / "denied").exists()

            bundle = tmp_path / "backup"
            manifest = await asyncio.to_thread(operation.backup, source_dsn, root, bundle, pg_dump=pg_dump)
            assert manifest["source"]["database"] == source_name
            # Retain the original while making its required path absent for the isolated rehearsal.
            retained = tmp_path / "retained-original"
            root.rename(retained)
            receipt = await asyncio.to_thread(operation.restore, target_dsn, root, bundle,
                                              pg_restore=pg_restore, original_path=True)
            assert receipt["runtime_path_matches"]
            assert (root / "fixture.env").read_text() == "FIXTURE_VALUE=preserved-verbatim\n"
            assert (workspace / ".env").read_text() == "FIXTURE_VALUE=local-test\n"
            assert (workspace / ".env.example").read_text() == "FIXTURE_VALUE=\n"
            assert git("rev-parse", "HEAD") == original_commit
            assert git("status", "--porcelain") == ""
            assert (root / "audit" / "api.jsonl").read_bytes() == audit
            assert (root / "factory" / "control" / "lifecycle.sqlite3").read_bytes() == journal_bytes
            restored_journal = WorkspaceJournal(root / "factory" / "control")
            assert restored_journal.get(lease.workflow_id) == lease
            assert restored_journal.history(lease.workflow_id) == history
            assert (workspace / "change.txt").read_bytes() == (retained / workspace.relative_to(root) / "change.txt").read_bytes()
            async with runtime(target_dsn) as (queue, graph, service):
                restored = await graph.aget_state(config)
                assert restored.values == pending.values
                assert restored.interrupts == pending.interrupts
                assert [snapshot.config async for snapshot in graph.aget_state_history(config)] == checkpoints
                access = await service.get_access_context(lease.workflow_id)
                assert (access.project_id, access.owner_client_id) == ("team-project", "team-owner")
                assert await queue.dispatch_scope() == namespace
                assert await queue.enqueue_start(workflow_id="never-created", expected_dispatch_scope=namespace,
                                                 **admission) == lease.workflow_id
                with pytest.raises(WorkflowDispatchConflict):
                    await queue.enqueue_start(workflow_id="never-created", **{**admission, "payload": {"request": "changed"}})
                assert await queue.dequeue("no-replay", 0.01) is None
                assert (await service.get(lease.workflow_id))["pending_decision"] == pending.interrupts[0].value
                await service.decide(lease.workflow_id, "explicit-restored-approval")
                job = await queue.dequeue("after-restore", 0.01)
                assert job is not None
                await service._invoke_job(job)
                assert await queue.acknowledge(job)
                completed = await graph.aget_state(config)
                assert not completed.interrupts and not completed.next
                assert completed.values["approved"] == "explicit-restored-approval"
            async with runtime(source_dsn) as (_, graph, _):
                original = await graph.aget_state(config)
                assert original.interrupts == pending.interrupts and original.values == pending.values
        finally:
            # Only UUID-named databases created here are removed; existing databases are untouched.
            for name in reversed(created):
                await admin.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))
