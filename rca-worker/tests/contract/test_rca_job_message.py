import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, FormatChecker
from pydantic import ValidationError

from sre_rca_worker.integrations.pubsub.messages import RcaJobMessage

ROOT = Path(__file__).resolve().parents[3]
VALID = {
    "schemaVersion": 1,
    "workerJobId": "10000000-0000-0000-0000-000000000001",
    "rcaRunId": "20000000-0000-0000-0000-000000000002",
    "incidentId": "30000000-0000-0000-0000-000000000003",
    "attempt": 1,
}


def test_worker_message_matches_shared_schema_and_canonical_bytes() -> None:
    schema = json.loads(
        (ROOT / "contracts/schemas/rca-job-message-v1.json").read_text()
    )
    Draft202012Validator(schema, format_checker=FormatChecker()).validate(VALID)

    message = RcaJobMessage.from_mapping(VALID)

    assert json.loads(message.to_bytes()) == VALID
    assert message.to_bytes() == (
        b'{"attempt":1,"incidentId":"30000000-0000-0000-0000-000000000003",'
        b'"rcaRunId":"20000000-0000-0000-0000-000000000002","schemaVersion":1,'
        b'"workerJobId":"10000000-0000-0000-0000-000000000001"}'
    )


def test_worker_message_rejects_extra_keys() -> None:
    with pytest.raises(ValidationError):
        RcaJobMessage.from_mapping(VALID | {"AlertValues": "secret"})
