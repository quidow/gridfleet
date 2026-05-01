"""normalize device tag values

Revision ID: 9c1f4d8b6e2a
Revises: 7f4a8e2d91bc
Create Date: 2026-05-02 00:00:00.000000

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "9c1f4d8b6e2a"
down_revision: str | None = "7f4a8e2d91bc"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        UPDATE devices
        SET tags = '{}'::json
        WHERE tags IS NOT NULL
          AND json_typeof(tags) <> 'object'
        """
    )
    op.execute(
        """
        UPDATE devices AS d
        SET tags = normalized.tags
        FROM (
            SELECT
                devices.id,
                json_object_agg(
                    entry.key,
                    CASE
                        WHEN json_typeof(entry.value) = 'string'
                            THEN to_json(entry.value #>> '{}')
                        ELSE to_json(entry.value::text)
                    END
                )::json AS tags
            FROM devices
            CROSS JOIN LATERAL json_each(devices.tags) AS entry(key, value)
            WHERE devices.tags IS NOT NULL
              AND json_typeof(devices.tags) = 'object'
            GROUP BY devices.id
        ) AS normalized
        WHERE d.id = normalized.id
        """
    )


def downgrade() -> None:
    pass
