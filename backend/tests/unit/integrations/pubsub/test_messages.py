import json
from uuid import UUID

import pytest
from pydantic import ValidationError

from sre_agent.integrations.pubsub.messages import RcaJobMessage

WORKER_JOB_ID = UUID("10000000-0000-0000-0000-000000000001")
RCA_RUN_ID = UUID("20000000-0000-0000-0000-000000000002")
INCIDENT_ID = UUID("30000000-0000-0000-0000-000000000003")


def test_rca_job_message_is_canonical_minimal_and_secret_free() -> None:
    message = RcaJobMessage.from_mapping(
        {
            "schemaVersion": 1,
            "workerJobId": WORKER_JOB_ID,
            "rcaRunId": RCA_RUN_ID,
            "incidentId": INCIDENT_ID,
            "attempt": 1,
        }
    )

    assert message.to_bytes() == (
        b'{"attempt":1,"incidentId":"30000000-0000-0000-0000-000000000003",'
        b'"rcaRunId":"20000000-0000-0000-0000-000000000002","schemaVersion":1,'
        b'"workerJobId":"10000000-0000-0000-0000-000000000001"}'
    )
    assert b"AlertValues" not in message.to_bytes()
    assert len(message.to_bytes()) < 1024


@pytest.mark.parametrize(
    "mutation",
    [
        {"unknown": "value"},
        {"schemaVersion": 2},
        {"workerJobId": "not-a-uuid"},
        {"attempt": 2},
    ],
)
def test_rca_job_message_rejects_unknown_or_invalid_values(mutation) -> None:
    payload = {
        "schemaVersion": 1,
        "workerJobId": str(WORKER_JOB_ID),
        "rcaRunId": str(RCA_RUN_ID),
        "incidentId": str(INCIDENT_ID),
        "attempt": 1,
    }
    payload.update(mutation)

    with pytest.raises(ValidationError):
        RcaJobMessage.from_mapping(payload)


def test_rca_job_message_rejects_oversized_encoded_payload() -> None:
    payload = json.dumps(
        {
            "schemaVersion": 1,
            "workerJobId": str(WORKER_JOB_ID),
            "rcaRunId": str(RCA_RUN_ID),
            "incidentId": str(INCIDENT_ID),
            "attempt": 1,
            "padding": "x" * 1024,
        }
    ).encode()

    with pytest.raises(ValueError, match="exceeds 1024 bytes"):
        RcaJobMessage.from_bytes(payload)
