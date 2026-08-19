"""SQLAlchemy ORM models for HotelOps AI.

Models are organized by domain and all inherit from
backend.app.infrastructure.database.base.Base. Importing this package
registers every model on the shared Base.metadata registry so that
Alembic autogenerate and metadata inspection see the full schema.
"""

from backend.app.infrastructure.database.models.ai import (
    FindingModel,
    RecommendationModel,
)
from backend.app.infrastructure.database.models.alerts_approvals import (
    AlertModel,
    ApprovalDecisionModel,
    ApprovalRequestModel,
)
from backend.app.infrastructure.database.models.analytics import (
    MetricModel,
    OpportunityModel,
)
from backend.app.infrastructure.database.models.audit_outbox_inbox import (
    AuditEventModel,
    InboxMessageModel,
    OutboxEventModel,
)
from backend.app.infrastructure.database.models.config import (
    AnalysisConfigModel,
    CameraConfigModel,
)
from backend.app.infrastructure.database.models.configuration import (
    CameraProfileEntityModel,
    ConfigurationModel,
    ConfigurationVersionModel,
    EntranceEntityModel,
    ExclusionROIEntityModel,
    PrivacyROIEntityModel,
    QueueAreaEntityModel,
    ServiceAreaEntityModel,
    TableEntityModel,
    ZoneEntityModel,
)
from backend.app.infrastructure.database.models.events import OperationalEventModel
from backend.app.infrastructure.database.models.evidence import (
    EvidencePackageModel,
    EvidenceRefModel,
)
from backend.app.infrastructure.database.models.idempotency import IdempotencyRecordModel
from backend.app.infrastructure.database.models.identity import (
    MembershipModel,
    PermissionModel,
    RoleModel,
    TenantModel,
    UserModel,
    VenueModel,
)
from backend.app.infrastructure.database.models.integrations import IntegrationModel
from backend.app.infrastructure.database.models.media import MediaAssetModel
from backend.app.infrastructure.database.models.temporal import TemporalFactModel
from backend.app.infrastructure.database.models.video import (
    CameraModel,
    LiveSessionTransitionLogModel,
    VideoAssetModel,
    VideoSessionModel,
    VideoStreamModel,
)

__all__ = [
    "AlertModel",
    "AnalysisConfigModel",
    "ApprovalDecisionModel",
    "ApprovalRequestModel",
    "AuditEventModel",
    "CameraConfigModel",
    "CameraModel",
    "CameraProfileEntityModel",
    "ConfigurationModel",
    "ConfigurationVersionModel",
    "EntranceEntityModel",
    "EvidencePackageModel",
    "EvidenceRefModel",
    "ExclusionROIEntityModel",
    "FindingModel",
    "IdempotencyRecordModel",
    "InboxMessageModel",
    "IntegrationModel",
    "MediaAssetModel",
    "MembershipModel",
    "MetricModel",
    "OperationalEventModel",
    "OpportunityModel",
    "OutboxEventModel",
    "PermissionModel",
    "PrivacyROIEntityModel",
    "QueueAreaEntityModel",
    "RecommendationModel",
    "RoleModel",
    "ServiceAreaEntityModel",
    "TableEntityModel",
    "TemporalFactModel",
    "TenantModel",
    "UserModel",
    "VenueModel",
    "VideoAssetModel",
    "VideoSessionModel",
    "VideoStreamModel",
    "LiveSessionTransitionLogModel",
    "ZoneEntityModel",
]
