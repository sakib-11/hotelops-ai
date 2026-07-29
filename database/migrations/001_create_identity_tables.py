"""Create initial identity/tenancy tables.

Revision ID: 001_create_identity_tables
Revises:
Create Date: 2026-07-29
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "001_create_identity_tables"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the identity/tenancy schema.

    Order matters: tables with no FK dependencies first.
    """
    # --- ENUM TYPES ---
    sa.Enum("active", "suspended", "disabled", name="tenant_status").create(op.get_bind())
    sa.Enum("active", "inactive", name="venue_status").create(op.get_bind())
    sa.Enum("active", "disabled", name="user_status").create(op.get_bind())
    sa.Enum("admin", "manager", "operator", name="role_name").create(op.get_bind())
    sa.Enum("active", "inactive", name="membership_status").create(op.get_bind())
    sa.Enum("all_venues", "specific_venues", name="membership_scope").create(op.get_bind())

    # --- TENANTS ---
    op.create_table(
        "tenants",
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column(
            "status",
            sa.Enum("active", "suspended", "disabled", name="tenant_status"),
            nullable=False,
            server_default="active",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("metadata", sa.JSONB(), nullable=True),
        sa.PrimaryKeyConstraint("tenant_id"),
    )

    # --- VENUES ---
    op.create_table(
        "venues",
        sa.Column("venue_id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column(
            "status",
            sa.Enum("active", "inactive", name="venue_status"),
            nullable=False,
            server_default="active",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("metadata", sa.JSONB(), nullable=True),
        sa.PrimaryKeyConstraint("venue_id"),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.tenant_id"],
            ondelete="CASCADE",
        ),
    )
    op.create_index("ix_venues_tenant_id", "venues", ["tenant_id"])

    # --- USERS ---
    op.create_table(
        "users",
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("display_name", sa.String(255), nullable=False),
        sa.Column("email", sa.String(255), nullable=False),
        sa.Column(
            "status",
            sa.Enum("active", "disabled", name="user_status"),
            nullable=False,
            server_default="active",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("metadata", sa.JSONB(), nullable=True),
        sa.PrimaryKeyConstraint("user_id"),
        sa.UniqueConstraint("email", name="uq_users_email"),
    )

    # --- ROLES ---
    op.create_table(
        "roles",
        sa.Column("role_id", sa.UUID(), nullable=False),
        sa.Column(
            "name",
            sa.Enum("admin", "manager", "operator", name="role_name"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("role_id"),
        sa.UniqueConstraint("name", name="uq_roles_name"),
    )

    # --- PERMISSIONS ---
    op.create_table(
        "permissions",
        sa.Column("permission_id", sa.UUID(), nullable=False),
        sa.Column("name", sa.String(100), nullable=False),
        sa.PrimaryKeyConstraint("permission_id"),
        sa.UniqueConstraint("name", name="uq_permissions_name"),
    )

    # --- ROLE-PERMISSIONS (association) ---
    op.create_table(
        "role_permissions",
        sa.Column("role_id", sa.UUID(), nullable=False),
        sa.Column("permission_id", sa.UUID(), nullable=False),
        sa.PrimaryKeyConstraint("role_id", "permission_id"),
        sa.ForeignKeyConstraint(["role_id"], ["roles.role_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["permission_id"], ["permissions.permission_id"], ondelete="CASCADE"
        ),
    )

    # --- MEMBERSHIPS ---
    op.create_table(
        "memberships",
        sa.Column("membership_id", sa.UUID(), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("role_id", sa.UUID(), nullable=False),
        sa.Column(
            "scope",
            sa.Enum("all_venues", "specific_venues", name="membership_scope"),
            nullable=False,
            server_default="all_venues",
        ),
        sa.Column(
            "status",
            sa.Enum("active", "inactive", name="membership_status"),
            nullable=False,
            server_default="active",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.PrimaryKeyConstraint("membership_id"),
        sa.ForeignKeyConstraint(["user_id"], ["users.user_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.tenant_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["role_id"], ["roles.role_id"], ondelete="CASCADE"),
    )
    op.create_index("ix_memberships_user_id", "memberships", ["user_id"])
    op.create_index("ix_memberships_tenant_id", "memberships", ["tenant_id"])
    op.create_index("ix_memberships_role_id", "memberships", ["role_id"])
    op.create_index(
        "ix_memberships_tenant_user",
        "memberships",
        ["tenant_id", "user_id"],
    )

    # --- MEMBERSHIP-VENUES (association) ---
    op.create_table(
        "membership_venues",
        sa.Column("membership_id", sa.UUID(), nullable=False),
        sa.Column("venue_id", sa.UUID(), nullable=False),
        sa.PrimaryKeyConstraint("membership_id", "venue_id"),
        sa.ForeignKeyConstraint(
            ["membership_id"], ["memberships.membership_id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["venue_id"], ["venues.venue_id"], ondelete="CASCADE"),
    )
    op.create_index(
        "ix_membership_venues_venue",
        "membership_venues",
        ["venue_id"],
    )


def downgrade() -> None:
    """Drop the identity/tenancy schema in reverse order.

    Association tables first, then entity tables,
    then ENUM types last.
    """
    # Drop association tables
    op.drop_table("membership_venues")
    op.drop_table("role_permissions")

    # Drop entity tables
    op.drop_table("memberships")
    op.drop_table("permissions")
    op.drop_table("roles")
    op.drop_table("users")
    op.drop_table("venues")
    op.drop_table("tenants")

    # Drop ENUM types (order matters: children before parents)
    op.execute("DROP TYPE IF EXISTS membership_scope")
    op.execute("DROP TYPE IF EXISTS membership_status")
    op.execute("DROP TYPE IF EXISTS role_name")
    op.execute("DROP TYPE IF EXISTS user_status")
    op.execute("DROP TYPE IF EXISTS venue_status")
    op.execute("DROP TYPE IF EXISTS tenant_status")
