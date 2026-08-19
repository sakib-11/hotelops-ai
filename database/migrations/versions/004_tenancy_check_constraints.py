"""Add CHECK constraints for tenancy identity invariants (Task 6.3).

Mirrors the contract-level validation (contracts/identity/models.py:
name/display_name min_length=1) at the database level so invalid rows
cannot be persisted through any code path.

Revision ID: 004_tenancy_check_constraints
Revises: 003_membership_venue_scope
Create Date: 2026-08-08
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "004_tenancy_check_constraints"
down_revision: str | None = "003_membership_venue_scope"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add column-level CHECK constraints to tenancy tables."""
    op.create_check_constraint("ck_tenants_name_not_empty", "tenants", "length(btrim(name)) > 0")
    op.create_check_constraint("ck_venues_name_not_empty", "venues", "length(btrim(name)) > 0")
    op.create_check_constraint(
        "ck_users_display_name_not_empty",
        "users",
        "length(btrim(display_name)) > 0",
    )
    op.create_check_constraint("ck_users_email_has_at", "users", "email LIKE '%@%'")


def downgrade() -> None:
    """Drop the CHECK constraints added in this migration."""
    op.drop_constraint("ck_tenants_name_not_empty", "tenants", type_="check")
    op.drop_constraint("ck_venues_name_not_empty", "venues", type_="check")
    op.drop_constraint("ck_users_display_name_not_empty", "users", type_="check")
    op.drop_constraint("ck_users_email_has_at", "users", type_="check")
