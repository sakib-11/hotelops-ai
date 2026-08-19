"""Enforce tenant consistency of membership venue scope (Task 6.3).

Task 5/6.3 invariant: "Venue scope must never allow cross-tenant
relationships."

Previously, membership_venues had independent single-column FKs to
memberships and venues — nothing stopped a membership of Tenant A being
linked to a venue of Tenant B.

This migration enforces the invariant structurally with the composite
foreign key pattern:

    membership_venues.tenant_id  (denormalized, FK-derived, NOT NULL)
    UNIQUE (membership_id, tenant_id)        on memberships
    UNIQUE (venue_id, tenant_id)             on venues
    FOREIGN KEY (membership_id, tenant_id)
        REFERENCES memberships (membership_id, tenant_id)
    FOREIGN KEY (venue_id, tenant_id)
        REFERENCES venues (venue_id, tenant_id)

A membership_venues row can therefore only exist when the membership AND
the venue share one tenant. The original single-column FKs are dropped:
each is implied by the corresponding composite FK.

NOTE (migration role + RLS): the backfill UPDATE reads memberships while
migration 002 has enabled FORCE ROW LEVEL SECURITY on these tables. The
migration role must therefore bypass RLS (superuser or BYPASSRLS), matching
the 002 contract that "migrations run as a separate role that can bypass
RLS". A non-bypass role would see zero rows (fail-closed policies) and the
NOT NULL alter would fail loudly.

Revision ID: 003_membership_venue_scope
Revises: 002_enable_rls
Create Date: 2026-08-08
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "003_membership_venue_scope"
down_revision: str | None = "002_enable_rls"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Auto-generated names from migration 001 (unnamed ForeignKeyConstraint).
_SQL_FK_MEMBERSHIP = "membership_venues_membership_id_fkey"
_SQL_FK_VENUE = "membership_venues_venue_id_fkey"


def upgrade() -> None:
    """Add cross-tenant enforcement to membership venue scope."""

    # 1. Unique targets on the parents (required before composite FKs).
    op.create_unique_constraint(
        "uq_memberships_membership_tenant",
        "memberships",
        ["membership_id", "tenant_id"],
    )
    op.create_unique_constraint(
        "uq_venues_venue_tenant",
        "venues",
        ["venue_id", "tenant_id"],
    )

    # 2. Denormalized tenant_id: add nullable, backfill from the
    #    membership, then lock to NOT NULL. FKs guarantee every
    #    membership_venues.membership_id resolves to exactly one
    #    membership with a tenant, so the backfill is total.
    op.add_column("membership_venues", sa.Column("tenant_id", sa.UUID(), nullable=True))
    op.execute(
        "UPDATE membership_venues mv "
        "SET tenant_id = m.tenant_id "
        "FROM memberships m "
        "WHERE mv.membership_id = m.membership_id"
    )
    op.alter_column("membership_venues", "tenant_id", nullable=False)

    # 3. Replace the redundant single-column FKs with composite FKs.
    op.drop_constraint(_SQL_FK_MEMBERSHIP, "membership_venues", type_="foreignkey")
    op.drop_constraint(_SQL_FK_VENUE, "membership_venues", type_="foreignkey")
    op.create_foreign_key(
        "fk_membership_venues_membership_tenant",
        "membership_venues",
        "memberships",
        ["membership_id", "tenant_id"],
        ["membership_id", "tenant_id"],
        ondelete="CASCADE",
    )
    op.create_foreign_key(
        "fk_membership_venues_venue_tenant",
        "membership_venues",
        "venues",
        ["venue_id", "tenant_id"],
        ["venue_id", "tenant_id"],
        ondelete="CASCADE",
    )


def downgrade() -> None:
    """Restore the pre-003 single-column FK structure."""

    # 1. Drop composite FKs and re-add the original single-column FKs.
    op.drop_constraint("fk_membership_venues_venue_tenant", "membership_venues", type_="foreignkey")
    op.drop_constraint(
        "fk_membership_venues_membership_tenant", "membership_venues", type_="foreignkey"
    )
    op.create_foreign_key(
        _SQL_FK_VENUE,
        "membership_venues",
        "venues",
        ["venue_id"],
        ["venue_id"],
        ondelete="CASCADE",
    )
    op.create_foreign_key(
        _SQL_FK_MEMBERSHIP,
        "membership_venues",
        "memberships",
        ["membership_id"],
        ["membership_id"],
        ondelete="CASCADE",
    )

    # 2. Remove the denormalized column.
    op.drop_column("membership_venues", "tenant_id")

    # 3. Remove the parent unique constraints.
    op.drop_constraint("uq_venues_venue_tenant", "venues", type_="unique")
    op.drop_constraint("uq_memberships_membership_tenant", "memberships", type_="unique")
