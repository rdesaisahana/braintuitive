"""avatar shop

Adds ``gamification_profiles.points_spent`` so points become a currency a child
redeems for avatars rather than only a score that goes up.

The split matters. ``total_points`` stays the lifetime record and remains what
drives levelling; ``points_spent`` tracks redemption, and the spendable balance
is the difference. Deducting from ``total_points`` instead would drop a child's
level when they bought something -- punishing them for using the reward, which
is the opposite of a reward.

Existing profiles get ``0``, which is exactly right: nobody has spent anything
because until now there was nothing to spend on.

Revision ID: 06f24c4eb9dd
Revises: b882273cd4c1
Create Date: 2026-09-10 09:14:52.331207
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "06f24c4eb9dd"
down_revision: str | None = "b882273cd4c1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

NAMING_CONVENTION = {
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "ix": "ix_%(table_name)s_%(column_0_name)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "pk": "pk_%(table_name)s",
}


def upgrade() -> None:
    """Add the redeemed-points counter."""
    with op.batch_alter_table(
        "gamification_profiles", schema=None, naming_convention=NAMING_CONVENTION
    ) as batch_op:
        # server_default so existing rows get a value; the ORM default takes
        # over for new ones.
        batch_op.add_column(
            sa.Column("points_spent", sa.Integer(), nullable=False, server_default="0")
        )


def downgrade() -> None:
    """Drop it, forgetting what was redeemed.

    Avatars already unlocked stay unlocked -- they live in
    ``unlocked_avatars`` -- so a child keeps what they bought and simply stops
    having been charged for it.
    """
    with op.batch_alter_table("gamification_profiles", schema=None) as batch_op:
        batch_op.drop_column("points_spent")
