from __future__ import annotations

from base64 import urlsafe_b64decode, urlsafe_b64encode
from binascii import Error as Base64Error
from collections.abc import Mapping
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from sre_agent.application.operator.read_models import (
    OperatorCursorInvalid,
    OperatorIdentity,
    OperatorResourceNotFound,
)

_AUTHORIZED_INCIDENT = """
(
    :global_access OR EXISTS (
        SELECT 1
        FROM subjects subject
        JOIN scope_grants grant_ ON grant_.subject_id = subject.id
        WHERE subject.external_id = :external_id
          AND (
              grant_.team_id = incident.team_id OR
              grant_.project_id = incident.project_id OR
              grant_.environment_id = incident.environment_id OR
              grant_.service_id = incident.service_id
          )
    )
)
"""


def _identity_parameters(identity: OperatorIdentity) -> dict[str, object]:
    return {
        "external_id": identity.external_id,
        "global_access": identity.global_access,
    }


def _encode_cursor(timestamp: datetime, resource_id: UUID) -> str:
    value = f"{timestamp.isoformat()}|{resource_id}".encode()
    return urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _decode_cursor(cursor: str | None) -> tuple[datetime | None, UUID | None]:
    if cursor is None:
        return None, None
    try:
        padding = "=" * (-len(cursor) % 4)
        decoded = urlsafe_b64decode(cursor + padding).decode("utf-8")
        timestamp, resource_id = decoded.rsplit("|", 1)
        parsed_timestamp = datetime.fromisoformat(timestamp)
        if parsed_timestamp.tzinfo is None:
            raise ValueError("cursor timestamp is not timezone-aware")
        return parsed_timestamp, UUID(resource_id)
    except (Base64Error, ValueError, UnicodeDecodeError) as error:
        raise OperatorCursorInvalid("invalid cursor") from error


def _scope(row: Mapping[Any, Any]) -> dict[str, Any]:
    return {
        "team_id": row["team_id"],
        "project_id": row["project_id"],
        "environment_id": row["environment_id"],
        "service_id": row["service_id"],
    }


def _incident(row: Mapping[Any, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "incident_number": f"INC-{row['incident_number']}",
        "title": row["title"],
        "severity": row["severity"],
        "status": row["status"],
        "alert_state": row["alert_state"],
        "rca_status": row["rca_status"],
        "scope": _scope(row),
        "acknowledged": row["acknowledged_at"] is not None,
        "acknowledged_at": row["acknowledged_at"],
        "acknowledged_by": row["acknowledged_by"],
        "assignee": (
            {"id": row["assigned_to"], "displayName": row["assignee_name"]}
            if row["assigned_to"] is not None
            else None
        ),
        "opened_at": row["opened_at"],
        "updated_at": row["updated_at"],
        "resolved_at": row["resolved_at"],
        "version": row["version"],
    }


class SqlAlchemyOperatorReadRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def list_incidents(
        self,
        identity: OperatorIdentity,
        *,
        cursor: str | None,
        limit: int,
    ) -> dict[str, Any]:
        bounded_limit = min(max(limit, 1), 100)
        cursor_at, cursor_id = _decode_cursor(cursor)
        parameters = _identity_parameters(identity) | {
            "cursor_at": cursor_at,
            "cursor_id": cursor_id,
            "limit": bounded_limit + 1,
        }
        async with self._session_factory() as session:
            rows = (
                (
                    await session.execute(
                        text(
                            f"""
                            SELECT incident.id, incident.incident_number,
                                   incident.title, incident.severity, incident.status,
                                   incident.alert_state, incident.team_id,
                                   incident.project_id, incident.environment_id,
                                   incident.service_id, incident.acknowledged_at,
                                   incident.acknowledged_by, incident.assigned_to,
                                   assignee.display_name AS assignee_name,
                                   incident.opened_at, incident.updated_at,
                                   incident.resolved_at, incident.version,
                                   latest_rca.status AS rca_status
                            FROM incidents incident
                            LEFT JOIN subjects assignee ON assignee.id = incident.assigned_to
                            LEFT JOIN LATERAL (
                                SELECT run.status
                                FROM rca_runs run
                                WHERE run.incident_id = incident.id
                                ORDER BY run.created_at DESC, run.id DESC
                                LIMIT 1
                            ) latest_rca ON true
                            WHERE {_AUTHORIZED_INCIDENT}
                              AND (
                                  CAST(:cursor_at AS timestamptz) IS NULL OR
                                  (incident.updated_at, incident.id) < (
                                      CAST(:cursor_at AS timestamptz),
                                      CAST(:cursor_id AS uuid)
                                  )
                              )
                            ORDER BY incident.updated_at DESC, incident.id DESC
                            LIMIT :limit
                            """
                        ),
                        parameters,
                    )
                )
                .mappings()
                .all()
            )
        has_more = len(rows) > bounded_limit
        page_rows = rows[:bounded_limit]
        return {
            "items": [_incident(row) for row in page_rows],
            "next_cursor": (
                _encode_cursor(page_rows[-1]["updated_at"], page_rows[-1]["id"])
                if has_more
                else None
            ),
        }

    async def get_incident(
        self, identity: OperatorIdentity, incident_id: UUID
    ) -> dict[str, Any]:
        async with self._session_factory() as session:
            row = (
                (
                    await session.execute(
                        text(
                            f"""
                            SELECT incident.id, incident.incident_number,
                                   incident.title, incident.severity, incident.status,
                                   incident.alert_state, incident.team_id,
                                   incident.project_id, incident.environment_id,
                                   incident.service_id, incident.acknowledged_at,
                                   incident.acknowledged_by, incident.assigned_to,
                                   assignee.display_name AS assignee_name,
                                   incident.opened_at, incident.updated_at,
                                   incident.resolved_at, incident.version,
                                   latest_rca.status AS rca_status,
                                   COALESCE(alerts.ids, ARRAY[]::uuid[]) AS alert_ids,
                                   COALESCE(runs.ids, ARRAY[]::uuid[]) AS rca_run_ids
                            FROM incidents incident
                            LEFT JOIN subjects assignee ON assignee.id = incident.assigned_to
                            LEFT JOIN LATERAL (
                                SELECT run.status
                                FROM rca_runs run
                                WHERE run.incident_id = incident.id
                                ORDER BY run.created_at DESC, run.id DESC LIMIT 1
                            ) latest_rca ON true
                            LEFT JOIN LATERAL (
                                SELECT array_agg(DISTINCT instance.id) AS ids
                                FROM incident_alerts link
                                JOIN alert_events linked_event
                                  ON linked_event.id = link.alert_event_id
                                 AND linked_event.partition_timestamp =
                                     link.alert_event_partition_timestamp
                                JOIN alert_instances instance
                                  ON instance.source_id = linked_event.source_id
                                 AND instance.fingerprint = linked_event.fingerprint
                                WHERE link.incident_id = incident.id
                            ) alerts ON true
                            LEFT JOIN LATERAL (
                                SELECT array_agg(run.id ORDER BY run.created_at) AS ids
                                FROM rca_runs run WHERE run.incident_id = incident.id
                            ) runs ON true
                            WHERE incident.id = :incident_id
                              AND {_AUTHORIZED_INCIDENT}
                            """
                        ),
                        _identity_parameters(identity) | {"incident_id": incident_id},
                    )
                )
                .mappings()
                .one_or_none()
            )
        if row is None:
            raise OperatorResourceNotFound
        return _incident(row) | {
            "description": row["title"],
            "alert_ids": list(row["alert_ids"]),
            "rca_run_ids": list(row["rca_run_ids"]),
        }

    async def get_alert(
        self, identity: OperatorIdentity, alert_id: UUID
    ) -> dict[str, Any]:
        async with self._session_factory() as session:
            row = (
                (
                    await session.execute(
                        text(
                            f"""
                            SELECT instance.id, instance.source_id, instance.fingerprint,
                                   instance.state, instance.first_seen_at,
                                   instance.last_seen_at, instance.resolved_at,
                                   event.labels, event.annotations, event.starts_at,
                                   event.ends_at, event.provider, event.folder_code,
                                   event.alert_name, event.severity_raw,
                                   event.severity_canonical, event.issue,
                                   event.normalization_status,
                                   event.normalization_rule_id,
                                   event.normalization_rule_version,
                                   event.normalization_warnings,
                                   event.raw_payload ->> 'generatorURL' AS generator_url,
                                   incident.id AS incident_id, incident.team_id,
                                   incident.project_id, incident.environment_id,
                                   incident.service_id
                            FROM alert_instances instance
                            JOIN alert_events event
                              ON event.id = instance.latest_event_id
                             AND event.partition_timestamp =
                                 instance.latest_event_partition_timestamp
                            JOIN LATERAL (
                                SELECT candidate.id, candidate.team_id,
                                       candidate.project_id,
                                       candidate.environment_id,
                                       candidate.service_id
                                FROM incident_alerts link
                                JOIN alert_events linked_event
                                  ON linked_event.id = link.alert_event_id
                                 AND linked_event.partition_timestamp =
                                     link.alert_event_partition_timestamp
                                JOIN incidents candidate
                                  ON candidate.id = link.incident_id
                                WHERE linked_event.source_id = instance.source_id
                                  AND linked_event.fingerprint = instance.fingerprint
                                ORDER BY candidate.updated_at DESC, candidate.id DESC
                                LIMIT 1
                            ) incident ON true
                            WHERE instance.id = :alert_id
                              AND {_AUTHORIZED_INCIDENT}
                            LIMIT 1
                            """
                        ),
                        _identity_parameters(identity) | {"alert_id": alert_id},
                    )
                )
                .mappings()
                .one_or_none()
            )
        if row is None:
            raise OperatorResourceNotFound
        issue = row["issue"] or {
            "rawText": "",
            "source": "grafana.annotations.AlertValues",
            "contentType": "text/plain",
            "untrusted": True,
        }
        return {
            "id": row["id"],
            "source_id": row["source_id"],
            "incident_id": row["incident_id"],
            "fingerprint": row["fingerprint"],
            "title": row["alert_name"] or row["fingerprint"],
            "severity": row["severity_canonical"] or "UNMAPPED",
            "state": row["state"],
            "classification_status": (
                "CLASSIFIED"
                if row["normalization_status"] == "NORMALIZED"
                else "UNCLASSIFIED"
            ),
            "scope": _scope(row),
            "starts_at": row["starts_at"] or row["first_seen_at"],
            "ends_at": row["ends_at"],
            "updated_at": row["last_seen_at"],
            "provider": row["provider"],
            "folder_code": row["folder_code"],
            "alert_name": row["alert_name"],
            "severity_raw": row["severity_raw"],
            "issue": issue,
            "normalization": {
                "status": row["normalization_status"],
                "rule_id": row["normalization_rule_id"],
                "rule_version": row["normalization_rule_version"],
            },
            "normalization_warnings": list(row["normalization_warnings"]),
            "labels": dict(row["labels"]),
            "annotations": dict(row["annotations"]),
            "generator_url": row["generator_url"],
        }

    async def list_rca_runs(
        self,
        identity: OperatorIdentity,
        incident_id: UUID,
        *,
        cursor: str | None,
        limit: int,
    ) -> dict[str, Any]:
        await self.get_incident(identity, incident_id)
        bounded_limit = min(max(limit, 1), 100)
        cursor_at, cursor_id = _decode_cursor(cursor)
        async with self._session_factory() as session:
            rows = (
                (
                    await session.execute(
                        text(
                            """
                            SELECT run.id, run.incident_id, run.status, run.started_at,
                                   run.completed_at, run.created_at, run.updated_at,
                                   run.error_message,
                                   row_number() OVER (
                                       PARTITION BY run.incident_id
                                       ORDER BY run.created_at, run.id
                                   ) AS run_number,
                                   report.id AS report_id
                            FROM rca_runs run
                            LEFT JOIN LATERAL (
                                SELECT item.id FROM rca_reports item
                                WHERE item.rca_run_id = run.id
                                ORDER BY item.version DESC LIMIT 1
                            ) report ON true
                            WHERE run.incident_id = :incident_id
                              AND (
                                  CAST(:cursor_at AS timestamptz) IS NULL OR
                                  (run.created_at, run.id) < (
                                      CAST(:cursor_at AS timestamptz),
                                      CAST(:cursor_id AS uuid)
                                  )
                              )
                            ORDER BY run.created_at DESC, run.id DESC
                            LIMIT :limit
                            """
                        ),
                        {
                            "incident_id": incident_id,
                            "cursor_at": cursor_at,
                            "cursor_id": cursor_id,
                            "limit": bounded_limit + 1,
                        },
                    )
                )
                .mappings()
                .all()
            )
        has_more = len(rows) > bounded_limit
        page_rows = rows[:bounded_limit]
        items = [
            {
                "id": row["id"],
                "incident_id": row["incident_id"],
                "run_number": row["run_number"],
                "status": row["status"],
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
                "started_at": row["started_at"],
                "completed_at": row["completed_at"],
                "failure_code": None,
                "report_id": row["report_id"],
            }
            for row in page_rows
        ]
        return {
            "items": items,
            "next_cursor": (
                _encode_cursor(page_rows[-1]["created_at"], page_rows[-1]["id"])
                if has_more
                else None
            ),
        }

    async def get_rca_report(
        self, identity: OperatorIdentity, rca_run_id: UUID
    ) -> dict[str, Any]:
        async with self._session_factory() as session:
            row = (
                (
                    await session.execute(
                        text(
                            f"""
                            SELECT report.id, report.rca_run_id, report.version,
                                   report.summary, report.report, report.created_at,
                                   run.incident_id
                            FROM rca_reports report
                            JOIN rca_runs run ON run.id = report.rca_run_id
                            JOIN incidents incident ON incident.id = run.incident_id
                            WHERE report.rca_run_id = :rca_run_id
                              AND {_AUTHORIZED_INCIDENT}
                            ORDER BY report.version DESC LIMIT 1
                            """
                        ),
                        _identity_parameters(identity) | {"rca_run_id": rca_run_id},
                    )
                )
                .mappings()
                .one_or_none()
            )
        if row is None:
            raise OperatorResourceNotFound
        body = dict(row["report"])
        return {
            "id": row["id"],
            "rca_run_id": row["rca_run_id"],
            "incident_id": row["incident_id"],
            "report_version": row["version"],
            "status": body["status"],
            "summary": row["summary"],
            "root_cause": body["rootCause"],
            "impact": body["impact"],
            "recommendations": body.get("recommendations", []),
            "claims": body.get("claims", []),
            "created_at": row["created_at"],
        }
