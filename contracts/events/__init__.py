"""Canonical event/processing contracts for HotelOps AI."""

from contracts.events.analysis import AnalysisJob, JobStatus
from contracts.events.envelope import EventEnvelope
from contracts.events.evidence import EvidenceRef, EvidenceType

__all__ = [
    "AnalysisJob",
    "EventEnvelope",
    "EvidenceRef",
    "EvidenceType",
    "JobStatus",
]
