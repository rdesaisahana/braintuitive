"""upload preview

Adds ``curriculum_uploads.preview``: what the parser found in an uploaded PDF --
title, grade, units -- shown to the parent before anything is built.

Ingestion now stops after parsing, at a new ``review`` status, and only embeds,
indexes and writes questions once the parent confirms. A wrong file then costs
a few seconds of parsing rather than minutes of embedding and model spend.

No constraint changes are needed for the two new statuses (``review`` and
``cancelled``): ``status`` is a plain ``VARCHAR(20)`` with no CHECK, and both
values fit.

Revision ID: c7e19a4d2f60
Revises: 06f24c4eb9dd
Create Date: 2026-09-10 21:40:03.118294
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c7e19a4d2f60"
down_revision: str | None = "06f24c4eb9dd"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the nullable preview column. Existing rows need no value."""
    with op.batch_alter_table("curriculum_uploads", schema=None) as batch_op:
        batch_op.add_column(sa.Column("preview", sa.JSON(), nullable=True))


def downgrade() -> None:
    """Drop the preview, and resolve rows the old schema cannot express.

    The older code knows nothing of ``review`` or ``cancelled``. An upload left
    in either would sit there forever -- counted as active by nothing, finished
    by nothing -- so both are closed out as failed, with a reason a parent can
    read, rather than silently re-queued to be built without the confirmation
    they never gave.

    No ``naming_convention`` here: batch mode reflects the table before
    rebuilding it, and a convention would re-prefix the names SQLite already
    holds, so the rebuild would look for constraints that do not exist.
    """
    op.execute(
        "UPDATE curriculum_uploads SET status = 'failed', "
        "error = 'Closed before it was confirmed.' "
        "WHERE status IN ('review', 'cancelled')"
    )
    with op.batch_alter_table("curriculum_uploads", schema=None) as batch_op:
        batch_op.drop_column("preview")
