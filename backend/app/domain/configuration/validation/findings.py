"""Structured validation findings (Task 10.9)."""

from __future__ import annotations

from dataclasses import dataclass, field

from backend.app.domain.configuration.validation.codes import RuleCode, severity_of


@dataclass(frozen=True)
class Finding:
    """A single structured validation finding."""

    code: RuleCode
    message: str
    entity_type: str | None = None
    entity_id: str | None = None
    related_entity_id: str | None = None

    @property
    def severity(self) -> str:
        """Deterministic severity from the stable code (never per-input)."""
        return severity_of(self.code)

    @property
    def is_error(self) -> bool:
        return self.severity == "error"

    def to_dict(self) -> dict[str, str | None]:
        return {
            "code": self.code.value,
            "severity": self.severity,
            "message": self.message,
            "entity_type": self.entity_type,
            "entity_id": self.entity_id,
            "related_entity_id": self.related_entity_id,
        }


@dataclass
class FindingCollector:
    """Collects findings and tracks whether prerequisites failed.

    When ``geometry_ok`` or ``references_ok`` is False, spatial
    validators are suppressed (cascading-error suppression, Task 10.9).
    """

    findings: list[Finding] = field(default_factory=list)
    geometry_ok: bool = True
    references_ok: bool = True
    checks_performed: int = 0

    def add(self, finding: Finding) -> None:
        self.findings.append(finding)

    def error(
        self,
        code: RuleCode,
        message: str,
        *,
        entity_type: str | None = None,
        entity_id: str | None = None,
        related_entity_id: str | None = None,
    ) -> None:
        self.add(
            Finding(
                code=code,
                message=message,
                entity_type=entity_type,
                entity_id=entity_id,
                related_entity_id=related_entity_id,
            )
        )

    def warning(
        self,
        code: RuleCode,
        message: str,
        *,
        entity_type: str | None = None,
        entity_id: str | None = None,
        related_entity_id: str | None = None,
    ) -> None:
        self.add(
            Finding(
                code=code,
                message=message,
                entity_type=entity_type,
                entity_id=entity_id,
                related_entity_id=related_entity_id,
            )
        )


__all__ = ["Finding", "FindingCollector"]
