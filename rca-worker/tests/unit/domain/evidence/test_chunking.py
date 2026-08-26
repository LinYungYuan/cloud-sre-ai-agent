from datetime import UTC, datetime
from uuid import UUID

import pytest

from sre_rca_worker.domain.evidence.chunking import build_evidence_chunks
from sre_rca_worker.domain.evidence.models import EvidenceReference

REFERENCE_A = EvidenceReference(
    id=UUID("00000000-0000-0000-0000-000000000001"),
    partition_timestamp=datetime(2026, 8, 24, 0, 0, tzinfo=UTC),
)
REFERENCE_B = EvidenceReference(
    id=UUID("00000000-0000-0000-0000-000000000002"),
    partition_timestamp=datetime(2026, 8, 24, 0, 1, tzinfo=UTC),
)


def test_chunks_canonical_json_by_unicode_code_points() -> None:
    chunks = build_evidence_chunks(
        REFERENCE_A,
        {"z": 1, "a": "台🔥"},
        chunk_chars=8,
        max_chunks=4,
        max_total_chars=32,
    )

    assert [
        (chunk.chunk_index, chunk.chunk_count, chunk.content) for chunk in chunks
    ] == [
        (0, 2, '{"a":"台🔥'),
        (1, 2, '","z":1}'),
    ]
    assert all(chunk.reference == REFERENCE_A for chunk in chunks)
    assert all(not chunk.truncated for chunk in chunks)


def test_chunks_are_repeatable_and_preserve_list_order_and_timestamp() -> None:
    structured = [
        {"timestamp": "2026-08-24T00:01:00Z", "service": "checkout"},
        {"timestamp": "2026-08-24T00:00:00Z", "service": "payments"},
    ]

    first = build_evidence_chunks(
        REFERENCE_A,
        structured,
        chunk_chars=8_000,
        max_chunks=4,
        max_total_chars=32_000,
    )
    second = build_evidence_chunks(
        REFERENCE_A,
        structured,
        chunk_chars=8_000,
        max_chunks=4,
        max_total_chars=32_000,
    )

    assert first == second
    assert first[0].content == (
        '[{"service":"checkout","timestamp":"2026-08-24T00:01:00Z"},'
        '{"service":"payments","timestamp":"2026-08-24T00:00:00Z"}]'
    )


def test_chunks_enforce_total_and_chunk_limits_with_truncation_signal() -> None:
    chunks = build_evidence_chunks(
        REFERENCE_A,
        {"a": "abcdefghijklmnopqrstuvwxyz"},
        chunk_chars=8,
        max_chunks=3,
        max_total_chars=20,
    )

    assert [chunk.content for chunk in chunks] == ['{"a":"ab', "cdefghij", "klmn"]
    assert [(chunk.chunk_index, chunk.chunk_count) for chunk in chunks] == [
        (0, 3),
        (1, 3),
        (2, 3),
    ]
    assert all(len(chunk.content) <= 8 for chunk in chunks)
    assert all(chunk.truncated for chunk in chunks)


def test_evidence_chunks_never_mix_references() -> None:
    first = build_evidence_chunks(
        REFERENCE_A,
        {"service": "checkout"},
        chunk_chars=8_000,
        max_chunks=4,
        max_total_chars=32_000,
    )
    second = build_evidence_chunks(
        REFERENCE_B,
        {"service": "payments"},
        chunk_chars=8_000,
        max_chunks=4,
        max_total_chars=32_000,
    )

    assert first[0].reference == REFERENCE_A
    assert second[0].reference == REFERENCE_B
    assert first[0].chunk_index == second[0].chunk_index == 0


def test_chunks_reject_total_above_chunk_capacity() -> None:
    with pytest.raises(ValueError):
        build_evidence_chunks(
            REFERENCE_A,
            {"a": "value"},
            chunk_chars=8_000,
            max_chunks=3,
            max_total_chars=32_000,
        )


def test_chunks_allow_total_below_chunk_capacity() -> None:
    chunks = build_evidence_chunks(
        REFERENCE_A,
        {"a": "value"},
        chunk_chars=8_000,
        max_chunks=4,
        max_total_chars=16_000,
    )

    assert chunks[0].content == '{"a":"value"}'


@pytest.mark.parametrize(
    ("chunk_chars", "max_chunks", "max_total_chars"),
    [
        (8_001, 4, 32_000),
        (8_000, 5, 32_000),
        (8_000, 4, 32_001),
        (8, 2, 17),
    ],
)
def test_chunks_reject_settings_that_exceed_the_persisted_contract(
    chunk_chars: int, max_chunks: int, max_total_chars: int
) -> None:
    with pytest.raises(ValueError):
        build_evidence_chunks(
            REFERENCE_A,
            {"a": "value"},
            chunk_chars=chunk_chars,
            max_chunks=max_chunks,
            max_total_chars=max_total_chars,
        )
