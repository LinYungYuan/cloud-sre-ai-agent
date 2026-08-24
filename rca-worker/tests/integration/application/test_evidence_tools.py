import json
import os
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import Session

from sre_rca_worker.agents.specialists.base import (
    SpecialistRequest,
    SpecialistResult,
)
from sre_rca_worker.agents.specialists.metrics_agent import MetricsSpecialist
from sre_rca_worker.agents.specialists.trace_agent import TraceSpecialist
from sre_rca_worker.application.rca.evidence_tools import (
    EvidenceToolError,
    EvidenceToolSession,
)
from sre_rca_worker.domain.evidence.models import EvidenceDraft, Finding
from sre_rca_worker.integrations.mcp.models import AllowedTool, CloudScope
from sre_rca_worker.persistence.repositories.rca import (
    AmbiguousEvidenceError,
    RcaRepository,
)

DATABASE_URL = os.getenv(
    "MIGRATION_TEST_DATABASE_URL",
    "postgresql+asyncpg://postgres@127.0.0.1:55432/sre_agent",
)


class CountingClient:
    endpoint_identity = "metrics"

    def __init__(self) -> None:
        self.calls = 0

    async def list_tools(self):
        return ()

    async def call(self, tool_name, arguments, deadline):
        self.calls += 1
        return b'{"cpu":85.23,"series":[1,2,3]}'


class DraftCollector:
    kind = MetricsSpecialist.kind

    def __init__(self, drafts: tuple[EvidenceDraft, ...]) -> None:
        self.calls = 0
        self._drafts = drafts

    async def run(self, request, deadline):
        self.calls += 1
        return SpecialistResult(
            specialist=self.kind,
            findings=tuple(
                Finding(summary="collected", confidence=0.5, evidence=(draft,))
                for draft in self._drafts
            ),
        )


def _tool(index: int = 0) -> AllowedTool:
    return AllowedTool(
        name=f"metrics_query_{index}",
        capability="metrics.query",
        endpoint_identity="metrics",
        input_schema={
            "type": "object",
            "properties": {"project_id": {"type": "string"}},
            "required": ["project_id"],
            "additionalProperties": False,
        },
    )


def _request(run_id: UUID, *, tool_count: int = 1) -> SpecialistRequest:
    now = datetime.now(UTC)
    return SpecialistRequest(
        incident_id=uuid4(),
        rca_run_id=run_id,
        alert_issue="CPU high",
        scope=CloudScope(provider="GCP", scope_id="project-a", safe=True),
        window_start=now - timedelta(minutes=15),
        window_end=now,
        available_tools=tuple(_tool(index) for index in range(tool_count)),
    )


def _draft(
    request: SpecialistRequest, *, observed_at: datetime, marker: str
) -> EvidenceDraft:
    assert request.scope is not None
    return EvidenceDraft(
        endpoint_identity="metrics",
        capability="metrics.query",
        tool="metrics_query_0",
        input_scope=request.scope,
        normalized_scope=request.scope,
        observed_at=observed_at,
        request_window_start=request.window_start,
        request_window_end=request.window_end,
        window_start=request.window_start,
        window_end=request.window_end,
        structured_json={"marker": marker},
        raw_result=json.dumps({"marker": marker}).encode(),
        content_type="application/json",
        input_sha256=("a" if marker == "late" else "b") * 64,
    )


async def _seed_run(
    sessions: async_sessionmaker[AsyncSession],
    *,
    run_id: UUID,
    specialist_ids: dict[str, UUID],
) -> tuple[UUID, UUID, UUID, UUID]:
    team_id, project_id, environment_id, incident_id = [uuid4() for _ in range(4)]
    now = datetime.now(UTC)
    async with sessions() as session, session.begin():
        await session.execute(
            text("INSERT INTO teams(id,name) VALUES (:id,:name)"),
            {"id": team_id, "name": f"team-{team_id}"},
        )
        await session.execute(
            text("INSERT INTO projects(id,team_id,name) VALUES (:id,:team,:name)"),
            {"id": project_id, "team": team_id, "name": f"project-{project_id}"},
        )
        await session.execute(
            text(
                "INSERT INTO environments(id,project_id,name) VALUES (:id,:project,:name)"
            ),
            {
                "id": environment_id,
                "project": project_id,
                "name": f"env-{environment_id}",
            },
        )
        await session.execute(
            text(
                """INSERT INTO incidents(
                       id,identity_key,title,severity,status,alert_state,
                       team_id,project_id,environment_id,opened_at)
                   VALUES (:id,:identity,'test','SEV3','OPEN','FIRING',
                           :team,:project,:environment,:now)"""
            ),
            {
                "id": incident_id,
                "identity": f"identity-{incident_id}",
                "team": team_id,
                "project": project_id,
                "environment": environment_id,
                "now": now,
            },
        )
        await session.execute(
            text(
                "INSERT INTO rca_runs(id,incident_id,status) VALUES (:id,:incident,'RUNNING')"
            ),
            {"id": run_id, "incident": incident_id},
        )
        for specialist_type, specialist_id in specialist_ids.items():
            await session.execute(
                text(
                    """INSERT INTO specialist_runs(
                           id,rca_run_id,specialist_type,status)
                       VALUES (:id,:run,:kind,'RUNNING')"""
                ),
                {"id": specialist_id, "run": run_id, "kind": specialist_type},
            )
    return team_id, project_id, environment_id, incident_id


async def _cleanup_run(
    sessions: async_sessionmaker[AsyncSession],
    *,
    run_id: UUID,
    team_id: UUID,
    project_id: UUID,
    environment_id: UUID,
    incident_id: UUID,
) -> None:
    async with sessions() as session, session.begin():
        await session.execute(
            text("DELETE FROM evidence_records WHERE rca_run_id=:run"),
            {"run": run_id},
        )
        await session.execute(
            text("DELETE FROM specialist_runs WHERE rca_run_id=:run"),
            {"run": run_id},
        )
        await session.execute(
            text("DELETE FROM rca_runs WHERE id=:run"), {"run": run_id}
        )
        await session.execute(
            text("DELETE FROM incidents WHERE id=:incident"),
            {"incident": incident_id},
        )
        await session.execute(
            text("DELETE FROM environments WHERE id=:environment"),
            {"environment": environment_id},
        )
        await session.execute(
            text("DELETE FROM projects WHERE id=:project"), {"project": project_id}
        )
        await session.execute(
            text("DELETE FROM teams WHERE id=:team"), {"team": team_id}
        )


def _tools(
    *,
    request: SpecialistRequest,
    specialist_run_id: UUID,
    collector,
    sessions: async_sessionmaker[AsyncSession],
) -> EvidenceToolSession:
    return EvidenceToolSession(
        request=request,
        specialist_run_id=specialist_run_id,
        collector=collector,
        sessions=sessions,
        deadline=datetime.now(UTC) + timedelta(minutes=1),
        chunk_chars=20,
        max_chunks=4,
        max_total_chars=80,
        max_tool_calls=5,
    )


@pytest.mark.asyncio
async def test_collect_commits_before_return_and_reuses_after_process_crash() -> None:
    engine = create_async_engine(DATABASE_URL)
    observer = {"completed_before_tool_return": False}

    def after_commit(session: Session) -> None:
        if session.info.get("observe_evidence_commit"):
            observer["completed_before_tool_return"] = True

    event.listen(Session, "after_commit", after_commit)
    sessions = async_sessionmaker(
        engine,
        expire_on_commit=False,
        info={"observe_evidence_commit": True},
    )
    run_id, specialist_id = uuid4(), uuid4()
    parents = await _seed_run(
        sessions, run_id=run_id, specialist_ids={"METRICS": specialist_id}
    )
    observer["completed_before_tool_return"] = False
    client = CountingClient()
    request = _request(run_id)
    try:
        tools = _tools(
            request=request,
            specialist_run_id=specialist_id,
            collector=MetricsSpecialist(lambda: client),
            sessions=sessions,
        )

        receipt = await tools.collect_evidence()

        assert observer["completed_before_tool_return"] is True
        assert len(receipt.references) == 1
        assert len(receipt.first_chunks) == 1
        assert "raw_result" not in receipt.model_dump()
        async with sessions() as session:
            exists = await session.scalar(
                text(
                    """SELECT EXISTS(
                           SELECT 1 FROM evidence_records
                           WHERE rca_run_id=:run AND specialist_run_id=:specialist
                             AND id=:evidence)"""
                ),
                {
                    "run": run_id,
                    "specialist": specialist_id,
                    "evidence": receipt.references[0].id,
                },
            )
        assert exists is True

        same_session = await tools.collect_evidence()
        assert same_session == receipt
        assert client.calls == 1

        after_crash = _tools(
            request=request,
            specialist_run_id=specialist_id,
            collector=MetricsSpecialist(lambda: client),
            sessions=sessions,
        )
        rebuilt = await after_crash.collect_evidence()
        assert rebuilt == receipt
        assert client.calls == 1
    finally:
        event.remove(Session, "after_commit", after_commit)
        await _cleanup_run(
            sessions,
            run_id=run_id,
            team_id=parents[0],
            project_id=parents[1],
            environment_id=parents[2],
            incident_id=parents[3],
        )
        await engine.dispose()


@pytest.mark.asyncio
async def test_initial_multi_evidence_receipt_uses_canonical_db_rebuild_order() -> None:
    engine = create_async_engine(DATABASE_URL)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    run_id, specialist_id = uuid4(), uuid4()
    parents = await _seed_run(
        sessions, run_id=run_id, specialist_ids={"METRICS": specialist_id}
    )
    request = _request(run_id)
    collector = DraftCollector(
        (
            _draft(request, observed_at=request.window_end, marker="late"),
            _draft(
                request,
                observed_at=request.window_end - timedelta(seconds=1),
                marker="early",
            ),
        )
    )
    try:
        first = await _tools(
            request=request,
            specialist_run_id=specialist_id,
            collector=collector,
            sessions=sessions,
        ).collect_evidence()
        rebuilt = await _tools(
            request=request,
            specialist_run_id=specialist_id,
            collector=collector,
            sessions=sessions,
        ).collect_evidence()

        assert first == rebuilt
        assert [chunk.content for chunk in first.first_chunks] == [
            '{"marker":"early"}',
            '{"marker":"late"}',
        ]
        assert collector.calls == 1
    finally:
        await _cleanup_run(
            sessions,
            run_id=run_id,
            team_id=parents[0],
            project_id=parents[1],
            environment_id=parents[2],
            incident_id=parents[3],
        )
        await engine.dispose()


@pytest.mark.asyncio
async def test_repository_reads_require_run_specialist_and_evidence_ownership() -> None:
    engine = create_async_engine(DATABASE_URL)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    run_a, run_b = uuid4(), uuid4()
    metrics_a, trace_a, metrics_b = uuid4(), uuid4(), uuid4()
    parents_a = await _seed_run(
        sessions,
        run_id=run_a,
        specialist_ids={"METRICS": metrics_a, "TRACES": trace_a},
    )
    parents_b = await _seed_run(
        sessions, run_id=run_b, specialist_ids={"METRICS": metrics_b}
    )
    client = CountingClient()
    request_a = _request(run_a)
    try:
        owner = _tools(
            request=request_a,
            specialist_run_id=metrics_a,
            collector=MetricsSpecialist(lambda: client),
            sessions=sessions,
        )
        receipt = await owner.collect_evidence()
        evidence_id = receipt.references[0].id

        wrong_kind = _tools(
            request=request_a,
            specialist_run_id=trace_a,
            collector=TraceSpecialist(lambda: client),
            sessions=sessions,
        )
        wrong_run = _tools(
            request=_request(run_b),
            specialist_run_id=metrics_b,
            collector=MetricsSpecialist(lambda: client),
            sessions=sessions,
        )

        for unowned in (wrong_kind, wrong_run):
            with pytest.raises(EvidenceToolError) as raised:
                await unowned.read_evidence_chunk(evidence_id, 0)
            assert raised.value.code == "ANALYSIS_UNKNOWN_EVIDENCE"

        async with sessions() as session:
            repository = RcaRepository(session)
            assert (
                await repository.get_specialist_evidence(run_a, trace_a, evidence_id)
                is None
            )
            assert (
                await repository.get_specialist_evidence(run_b, metrics_b, evidence_id)
                is None
            )
        assert client.calls == 1
    finally:
        await _cleanup_run(
            sessions,
            run_id=run_b,
            team_id=parents_b[0],
            project_id=parents_b[1],
            environment_id=parents_b[2],
            incident_id=parents_b[3],
        )
        await _cleanup_run(
            sessions,
            run_id=run_a,
            team_id=parents_a[0],
            project_id=parents_a[1],
            environment_id=parents_a[2],
            incident_id=parents_a[3],
        )
        await engine.dispose()


@pytest.mark.asyncio
async def test_duplicate_partitioned_evidence_id_fails_closed() -> None:
    engine = create_async_engine(DATABASE_URL)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    run_id, specialist_id = uuid4(), uuid4()
    parents = await _seed_run(
        sessions, run_id=run_id, specialist_ids={"METRICS": specialist_id}
    )
    evidence_id = uuid4()
    now = datetime.now(UTC)
    async with sessions() as session, session.begin():
        for offset, marker in enumerate(("first", "second")):
            observed_at = now + timedelta(microseconds=offset)
            await session.execute(
                text(
                    """INSERT INTO evidence_records(
                           id,partition_timestamp,observed_at,rca_run_id,
                           specialist_run_id,evidence_type,source_agent,
                           source_endpoint,tool_name,time_window_start,
                           time_window_end,structured_data,content_hash,
                           raw_result,metadata)
                       VALUES (:id,:observed,:observed,:run,:specialist,
                               'metrics.query','METRICS','metrics',
                               'metrics_query_0',:observed,:observed,
                               CAST(:structured AS JSONB),:hash,:raw,
                               CAST('{}' AS JSONB))"""
                ),
                {
                    "id": evidence_id,
                    "observed": observed_at,
                    "run": run_id,
                    "specialist": specialist_id,
                    "structured": json.dumps({"marker": marker}),
                    "hash": marker,
                    "raw": marker.encode(),
                },
            )
    client = CountingClient()
    request = _request(run_id)
    try:
        tools = _tools(
            request=request,
            specialist_run_id=specialist_id,
            collector=MetricsSpecialist(lambda: client),
            sessions=sessions,
        )

        with pytest.raises(EvidenceToolError) as read_error:
            await tools.read_evidence_chunk(evidence_id, 0)
        with pytest.raises(EvidenceToolError) as collect_error:
            await tools.collect_evidence()

        assert read_error.value.code == "ANALYSIS_UNKNOWN_EVIDENCE"
        assert collect_error.value.code == "ANALYSIS_UNKNOWN_EVIDENCE"
        async with sessions() as session:
            repository = RcaRepository(session)
            with pytest.raises(AmbiguousEvidenceError):
                await repository.get_specialist_evidence(
                    run_id, specialist_id, evidence_id
                )
            with pytest.raises(AmbiguousEvidenceError):
                await repository.list_specialist_evidence(run_id, specialist_id)
        assert client.calls == 0
    finally:
        await _cleanup_run(
            sessions,
            run_id=run_id,
            team_id=parents[0],
            project_id=parents[1],
            environment_id=parents[2],
            incident_id=parents[3],
        )
        await engine.dispose()


@pytest.mark.asyncio
async def test_more_than_five_allowed_tools_never_make_a_sixth_mcp_call() -> None:
    engine = create_async_engine(DATABASE_URL)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    run_id, specialist_id = uuid4(), uuid4()
    parents = await _seed_run(
        sessions, run_id=run_id, specialist_ids={"METRICS": specialist_id}
    )
    client = CountingClient()
    try:
        receipt = await _tools(
            request=_request(run_id, tool_count=6),
            specialist_run_id=specialist_id,
            collector=MetricsSpecialist(lambda: client),
            sessions=sessions,
        ).collect_evidence()

        assert client.calls == 5
        assert len(receipt.references) == 5
    finally:
        await _cleanup_run(
            sessions,
            run_id=run_id,
            team_id=parents[0],
            project_id=parents[1],
            environment_id=parents[2],
            incident_id=parents[3],
        )
        await engine.dispose()


@pytest.mark.asyncio
async def test_failed_transaction_rolls_back_all_evidence_and_returns_no_receipt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = create_async_engine(DATABASE_URL)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    run_id, specialist_id = uuid4(), uuid4()
    parents = await _seed_run(
        sessions, run_id=run_id, specialist_ids={"METRICS": specialist_id}
    )
    client = CountingClient()
    original_insert = RcaRepository.insert_evidence
    inserts = 0

    async def fail_second_insert(self, rca_run_id, specialist_run_id, draft):
        nonlocal inserts
        inserts += 1
        if inserts % 2 == 0:
            raise RuntimeError("sensitive database detail")
        return await original_insert(self, rca_run_id, specialist_run_id, draft)

    monkeypatch.setattr(RcaRepository, "insert_evidence", fail_second_insert)
    try:
        tools = _tools(
            request=_request(run_id, tool_count=2),
            specialist_run_id=specialist_id,
            collector=MetricsSpecialist(lambda: client),
            sessions=sessions,
        )

        failures = []
        for _ in range(2):
            with pytest.raises(EvidenceToolError) as raised:
                await tools.collect_evidence()
            failures.append(raised.value)

        assert [failure.code for failure in failures] == [
            "ANALYSIS_FAILED",
            "ANALYSIS_FAILED",
        ]
        assert all("sensitive" not in str(failure) for failure in failures)
        assert client.calls == 2
        async with sessions() as session:
            count = await session.scalar(
                text("SELECT count(*) FROM evidence_records WHERE rca_run_id=:run"),
                {"run": run_id},
            )
        assert count == 0
    finally:
        await _cleanup_run(
            sessions,
            run_id=run_id,
            team_id=parents[0],
            project_id=parents[1],
            environment_id=parents[2],
            incident_id=parents[3],
        )
        await engine.dispose()
