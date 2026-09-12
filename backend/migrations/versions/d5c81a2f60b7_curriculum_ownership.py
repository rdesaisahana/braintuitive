"""curriculum ownership

Gives each curriculum unit an owner, so a parent can upload their own child's
guide instead of every family sharing one operator-loaded set.

``curriculum_units.user_id`` is **nullable on purpose**: NULL means the shared
sample curriculum, which every family sees until they upload their own. A brand
new account with an empty dashboard looks broken rather than new, and the
existing sample (6 units, 47 sub-units, 683 banked questions) is worth keeping
for exactly that.

The identity constraint widens from ``(subject, grade_level, unit_number)`` to
``(user_id, subject, grade_level, unit_number)``. Without that, the second
family to upload a grade-6 maths guide would collide with the first -- their
Unit 1 is a different Unit 1.

**Known limitation of the nullable column:** SQLite treats NULLs as distinct in
a UNIQUE constraint, so two shared units could in principle share a
(subject, grade, unit) triple where one row previously could not. Shared
curriculum is only ever written by the operator CLI, which upserts by that
triple, so nothing in the codebase can produce the duplicate -- but a hand-
written INSERT could.

Revision ID: d5c81a2f60b7
Revises: a1f4c07b93de
Create Date: 2026-09-09 18:02:41.550913
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d5c81a2f60b7"
down_revision: str | None = "a1f4c07b93de"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Batch mode recreates the table, so every constraint it carries needs a name.
NAMING_CONVENTION = {
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "ix": "ix_%(table_name)s_%(column_0_name)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "pk": "pk_%(table_name)s",
}

IDENTITY = "uq_unit_identity"


def upgrade() -> None:
    """Add the owner column and widen the identity constraint."""
    with op.batch_alter_table(
        "curriculum_units", schema=None, naming_convention=NAMING_CONVENTION
    ) as batch_op:
        batch_op.add_column(sa.Column("user_id", sa.String(length=36), nullable=True))
        batch_op.create_index("ix_curriculum_units_user_id", ["user_id"], unique=False)
        batch_op.create_foreign_key(
            "fk_curriculum_units_user_id_users",
            "users",
            ["user_id"],
            ["id"],
            ondelete="CASCADE",
        )
        # Drop before create: the old constraint would reject a second family's
        # Unit 1 before the new one ever got to allow it.
        batch_op.drop_constraint(IDENTITY, type_="unique")
        batch_op.create_unique_constraint(
            IDENTITY, ["user_id", "subject", "grade_level", "unit_number"]
        )

    # Existing rows keep user_id NULL, which is exactly right: whatever was
    # ingested before this migration was operator-loaded, and that is the
    # definition of the shared sample.


def downgrade() -> None:
    """Return to one global curriculum, discarding uploaded ones.

    Uploaded curricula cannot be represented once the owner column is gone, and
    keeping them would collide on the narrowed constraint -- two families' Unit
    1s becoming one. They are deleted rather than merged, which is destructive
    and deliberately so: merging would corrupt both families' progress.

    This batch omits ``NAMING_CONVENTION`` for the drops. Batch mode reflects
    the table before rebuilding it, and the convention would re-prefix the
    names SQLite already holds, so a drop would find nothing.
    """
    op.execute("DELETE FROM curriculum_units WHERE user_id IS NOT NULL")

    with op.batch_alter_table("curriculum_units", schema=None) as batch_op:
        batch_op.drop_constraint(IDENTITY, type_="unique")
        batch_op.create_unique_constraint(IDENTITY, ["subject", "grade_level", "unit_number"])
        batch_op.drop_constraint("fk_curriculum_units_user_id_users", type_="foreignkey")
        batch_op.drop_index("ix_curriculum_units_user_id")
        batch_op.drop_column("user_id")
