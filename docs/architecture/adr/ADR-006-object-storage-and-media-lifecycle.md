# ADR-006: Object Storage & Media Lifecycle Architecture

## Status

Accepted

## Context

HotelOps AI processes large binary media assets including CCTV video recordings, high-resolution keyframes, cropped visual evidence packages, analytical heatmaps, and diagnostic artifacts.

In accordance with [ADR-003](ADR-003-postgresql-source-of-truth.md) and [Database Governance §1 Rule 3](../database-governance.md), PostgreSQL must never store raw binary media bytes. Authoritative business metadata, tenant ownership, provenance, retention lifecycle state, and security references live in PostgreSQL; the actual unstructured media bytes reside in S3-compatible Object Storage (MinIO CE in development/on-premise, AWS S3 in cloud).

A formal architectural contract is required to define:
1. The boundary between PostgreSQL metadata and object storage bytes.
2. The provider-independent storage abstraction (StoragePort/StorageService).
3. The multi-tenant and multi-venue object-key naming strategy.
4. The two-phase upload and state-machine lifecycle (preventing unvalidated or orphaned media).
5. The short-lived presigned access model (preventing public URLs or credential leaks).
6. Reconciliation and asynchronous retention cleanup workflows.

## Decision

We establish the following architectural rules for Object Storage and Media Lifecycle in HotelOps AI:

### 1. Storage Boundary & Separation of Concerns
- **PostgreSQL (Authoritative Metadata Store):** Maintains business identity, tenant/venue ownership, capture provenance, content MIME type, file size, SHA-256 integrity checksum, lifecycle state machine (`INITIATED`, `UPLOADING`, `UPLOADED`, `VALIDATING`, `AVAILABLE`, `FAILED`, `DELETION_PENDING`, `DELETED`), expiration timestamps, and audit event linkages.
- **Object Storage (Byte Store):** A passive, durable byte container. It has zero knowledge of tenancy, RBAC, users, incidents, or business rules.
- **Rule:** An object existing in storage does *not* mean the media is available or authorized. Application code must always query PostgreSQL metadata first.

### 2. Provider Abstraction (Port/Adapter Pattern)
- The domain and application layers must depend solely on an abstract `StoragePort` protocol / `StorageService`.
- No direct coupling to `boto3`, `aioboto3`, or MinIO SDKs in application routers or domain services.
- The concrete `S3StorageAdapter` encapsulates all S3 API interactions (head, put, get, delete, multipart, presign).

### 3. Object-Key Hierarchy & Scoping
Object keys in storage are deterministic, hierarchical, and scoped to prevent namespace collisions and simplify lifecycle rules:
```
tenants/{tenant_id}/venues/{venue_id}/{category}/{year}/{month}/{day}/{artifact_id}.{ext}
```
Where:
- `category` ∈ `{"recordings", "evidence", "analytics", "temporary"}`
- `artifact_id` is the canonical UUID matching `asset_id` or `ref_id` in PostgreSQL.
- Raw user-provided filenames are never used in storage keys (they are stored in PostgreSQL metadata only).

### 4. Upload Lifecycle & Two-Phase Commit
Direct client-to-storage upload using presigned PUT URLs with a strict two-phase commit:
1. **Initiate:** Client requests upload URL with expected file size and content type. Backend checks `ActorContext` permissions, creates a `pending`/`initiated` metadata record in PostgreSQL, and generates a short-lived (15-min) presigned PUT URL.
2. **Upload:** Client transfers bytes directly to Object Storage via HTTP PUT.
3. **Complete & Validate:** Client notifies backend of completion. Backend performs S3 `head_object` to verify actual byte size and computes/verifies the SHA-256 checksum against constraints.
4. **Activate:** Once validated, PostgreSQL transitions the record to `AVAILABLE` and writes an `AuditEvent`.

### 5. Access Security & Signed URLs
- Object storage buckets and objects are **100% private** (no public ACLs, no public read access).
- Clients never receive storage credentials.
- Read access requires an authenticated request to FastAPI, which verifies `ActorContext` (tenant match, venue access, `Permission.VIDEO_READ` / `Permission.EVIDENCE_READ`) and generates a short-lived (15-min) presigned GET URL with `response-content-disposition=inline`.

### 6. Retention, Deletion & Orphan Reconciliation
- Media deletion is a two-phase state transition: `AVAILABLE` -> `DELETION_PENDING` -> S3 Delete -> `DELETED`.
- Deletion is idempotent.
- An asynchronous `MediaRetentionWorker` (inheriting from `PollingWorker`) periodically scans for expired media (`expires_at <= NOW()`) and incomplete upload sessions exceeding TTL, purges storage objects, and updates PostgreSQL records.

## Rationale

- **Performance & Scalability:** Offloads high-bandwidth video transfers from FastAPI event loops directly to MinIO/S3 via presigned URLs.
- **Tenant Isolation:** Enforces multi-tenancy at the application layer via PostgreSQL RLS and `require_tenant_venue_access`, while physically partitioning keys in object storage.
- **Data Integrity:** Two-phase upload prevents "phantom" media records from being referenced before bytes are verified on disk.
- **Auditability:** Every media creation, access URL generation, and deletion generates a structured `AuditEvent`.

## Consequences

- **Positive:**
  - Zero raw media bytes stored in PostgreSQL tables.
  - Zero storage provider lock-in (MinIO in local dev, AWS S3 / Cloudflare R2 / GCS in cloud).
  - Defense-in-depth: PostgreSQL RLS + FastAPI Scope + S3 Presigned URLs.
  - High resilience against orphaned files and interrupted uploads.
- **Negative:**
  - Eventual consistency between PostgreSQL and S3 requires background reconciliation for edge-case crashes.
  - Multi-step upload flow requires clients to execute an explicit completion call.
