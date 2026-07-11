"""rename lifecycle policy stop_pending keys to deferred_stop

Revision ID: 2026_07_12_deferred_stop
Revises: 2026_07_11_exclusion_kind
Create Date: 2026-07-12
"""

from alembic import op

revision = "2026_07_12_deferred_stop"
down_revision = "2026_07_11_exclusion_kind"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        UPDATE devices
           SET lifecycle_policy_state =
               (lifecycle_policy_state - 'stop_pending' - 'stop_pending_reason' - 'stop_pending_since')
               || jsonb_build_object(
                    'deferred_stop',
                    COALESCE(lifecycle_policy_state -> 'stop_pending', 'false'::jsonb),
                    'deferred_stop_reason',
                    COALESCE(lifecycle_policy_state -> 'stop_pending_reason', 'null'::jsonb),
                    'deferred_stop_since',
                    COALESCE(lifecycle_policy_state -> 'stop_pending_since', 'null'::jsonb)
                  )
         WHERE jsonb_exists_any(
                   lifecycle_policy_state,
                   ARRAY['stop_pending', 'stop_pending_reason', 'stop_pending_since']
               )
        """
    )


def downgrade() -> None:
    op.execute(
        """
        UPDATE devices
           SET lifecycle_policy_state =
               (lifecycle_policy_state - 'deferred_stop' - 'deferred_stop_reason' - 'deferred_stop_since')
               || jsonb_build_object(
                    'stop_pending',
                    COALESCE(lifecycle_policy_state -> 'deferred_stop', 'false'::jsonb),
                    'stop_pending_reason',
                    COALESCE(lifecycle_policy_state -> 'deferred_stop_reason', 'null'::jsonb),
                    'stop_pending_since',
                    COALESCE(lifecycle_policy_state -> 'deferred_stop_since', 'null'::jsonb)
                  )
         WHERE jsonb_exists_any(
                   lifecycle_policy_state,
                   ARRAY['deferred_stop', 'deferred_stop_reason', 'deferred_stop_since']
               )
        """
    )
