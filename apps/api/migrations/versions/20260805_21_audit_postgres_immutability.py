"""PostgreSQL audit immutability trigger

Revision ID: 20260805_21
Revises: 20260805_20
"""

from alembic import op

revision = "20260805_21"
down_revision = "20260805_20"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute(
            """
            CREATE OR REPLACE FUNCTION reject_audit_mutation()
            RETURNS trigger AS $$
            BEGIN
              RAISE EXCEPTION 'audit log is immutable';
            END;
            $$ LANGUAGE plpgsql
            """
        )
        op.execute(
            """
            CREATE TRIGGER prevent_audit_mutation
            BEFORE UPDATE OR DELETE ON audit_logs
            FOR EACH ROW EXECUTE FUNCTION reject_audit_mutation()
            """
        )


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute("DROP TRIGGER IF EXISTS prevent_audit_mutation ON audit_logs")
        op.execute("DROP FUNCTION IF EXISTS reject_audit_mutation")
