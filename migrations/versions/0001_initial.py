"""Frozen initial schema: decimal text ledger, append-only events and job queue."""
from alembic import op
import sqlalchemy as sa
revision = '0001'
down_revision = None
branch_labels = None
depends_on = None


def ensure_table(name, *columns):
    # Support the first local bootstrapped schema without dropping account data.
    if not op.get_context().as_sql:
        inspector = sa.inspect(op.get_bind())
        if inspector.has_table(name):
            expected = {column.name for column in columns if isinstance(column, sa.Column)}
            present = {column['name'] for column in inspector.get_columns(name)}
            if expected != present:
                raise RuntimeError(f'Existing {name} schema differs; explicit migration required')
            return
    op.create_table(name, *columns)


def ensure_index(name, table, columns, unique=False):
    if not op.get_context().as_sql:
        if name in {index['name'] for index in sa.inspect(op.get_bind()).get_indexes(table)}:
            return
    op.create_index(name, table, columns, unique=unique)

def upgrade():
    ensure_table('accounts',
        sa.Column('id', sa.String(), nullable=False, primary_key=True),
        sa.Column('mode', sa.String(), nullable=False, primary_key=False),
        sa.Column('cash', sa.Text(), nullable=False, primary_key=False),
        sa.Column('settled_cash', sa.Text(), nullable=False, primary_key=False),
        sa.Column('realized_pnl', sa.Text(), nullable=False, primary_key=False),
        sa.Column('initial_equity', sa.Text(), nullable=False, primary_key=False),
        sa.Column('currency', sa.String(), nullable=False, primary_key=False),
    )
    ensure_table('audit',
        sa.Column('id', sa.String(), nullable=False, primary_key=True),
        sa.Column('timestamp', sa.String(), nullable=False, primary_key=False),
        sa.Column('category', sa.String(), nullable=False, primary_key=False),
        sa.Column('event', sa.String(), nullable=False, primary_key=False),
        sa.Column('actor', sa.String(), nullable=False, primary_key=False),
        sa.Column('account_id', sa.String(), nullable=False, primary_key=False),
        sa.Column('symbol', sa.String(), nullable=True, primary_key=False),
        sa.Column('run_id', sa.String(), nullable=True, primary_key=False),
        sa.Column('signal_id', sa.String(), nullable=True, primary_key=False),
        sa.Column('intent_id', sa.String(), nullable=True, primary_key=False),
        sa.Column('order_id', sa.String(), nullable=True, primary_key=False),
        sa.Column('version', sa.Integer(), nullable=True, primary_key=False),
        sa.Column('detail', sa.JSON(), nullable=False, primary_key=False),
    )
    ensure_index('ix_audit_timestamp', 'audit', ['timestamp'], unique=False)
    ensure_table('candidates',
        sa.Column('id', sa.String(), nullable=False, primary_key=True),
        sa.Column('session_date', sa.String(), nullable=False, primary_key=False),
        sa.Column('symbol', sa.String(), nullable=False, primary_key=False),
        sa.Column('security_id', sa.String(), nullable=False, primary_key=False),
        sa.Column('data', sa.JSON(), nullable=False, primary_key=False),
        sa.Column('created_at', sa.String(), nullable=False, primary_key=False),
        sa.UniqueConstraint('session_date', 'symbol', name='uq_candidate_session_symbol'),
    )
    ensure_index('ix_candidates_session_date', 'candidates', ['session_date'], unique=False)
    ensure_table('leases',
        sa.Column('id', sa.String(), nullable=False, primary_key=True),
        sa.Column('owner', sa.String(), nullable=False, primary_key=False),
        sa.Column('expires_at', sa.String(), nullable=False, primary_key=False),
    )
    ensure_table('runs',
        sa.Column('id', sa.String(), nullable=False, primary_key=True),
        sa.Column('started_at', sa.String(), nullable=False, primary_key=False),
        sa.Column('completed_at', sa.String(), nullable=True, primary_key=False),
        sa.Column('duration_ms', sa.Integer(), nullable=False, primary_key=False),
        sa.Column('coverage', sa.Integer(), nullable=False, primary_key=False),
        sa.Column('status', sa.String(), nullable=False, primary_key=False),
        sa.Column('data_source', sa.String(), nullable=False, primary_key=False),
        sa.Column('quality', sa.String(), nullable=False, primary_key=False),
        sa.Column('message', sa.String(), nullable=False, primary_key=False),
    )
    ensure_table('schema_versions',
        sa.Column('version', sa.Integer(), nullable=False, primary_key=True),
        sa.Column('applied_at', sa.String(), nullable=False, primary_key=False),
    )
    ensure_table('session_equity',
        sa.Column('session_date', sa.String(), nullable=False, primary_key=True),
        sa.Column('opening_equity', sa.Text(), nullable=False, primary_key=False),
    )
    ensure_table('settings',
        sa.Column('id', sa.Integer(), nullable=False, primary_key=True),
        sa.Column('mode', sa.String(), nullable=False, primary_key=False),
        sa.Column('paused_entries', sa.Boolean(), nullable=False, primary_key=False),
        sa.Column('stopped', sa.Boolean(), nullable=False, primary_key=False),
        sa.Column('emergency', sa.Boolean(), nullable=False, primary_key=False),
        sa.Column('reconciliation_blocked', sa.Boolean(), nullable=False, primary_key=False),
        sa.Column('params', sa.JSON(), nullable=False, primary_key=False),
        sa.Column('version', sa.Integer(), nullable=False, primary_key=False),
        sa.Column('failures', sa.Integer(), nullable=False, primary_key=False),
    )
    ensure_table('positions',
        sa.Column('id', sa.String(), nullable=False, primary_key=True),
        sa.Column('account_id', sa.String(), sa.ForeignKey('accounts.id'), nullable=False, primary_key=False),
        sa.Column('symbol', sa.String(), nullable=False, primary_key=False),
        sa.Column('security_id', sa.String(), nullable=False, primary_key=False),
        sa.Column('quantity', sa.Text(), nullable=False, primary_key=False),
        sa.Column('avg_price', sa.Text(), nullable=False, primary_key=False),
        sa.Column('last_price', sa.Text(), nullable=False, primary_key=False),
        sa.Column('entry_atr', sa.Text(), nullable=False, primary_key=False),
        sa.Column('ma20', sa.Text(), nullable=False, primary_key=False),
        sa.Column('highest_close', sa.Text(), nullable=False, primary_key=False),
        sa.Column('protection', sa.Text(), nullable=False, primary_key=False),
        sa.Column('managed', sa.Boolean(), nullable=False, primary_key=False),
        sa.Column('strategy', sa.String(), nullable=False, primary_key=False),
        sa.Column('opened_at', sa.String(), nullable=False, primary_key=False),
        sa.Column('updated_at', sa.String(), nullable=False, primary_key=False),
        sa.Column('exit_status', sa.String(), nullable=False, primary_key=False),
        sa.Column('last_bar_at', sa.String(), nullable=True, primary_key=False),
        sa.UniqueConstraint('account_id', 'symbol', name='uq_account_position'),
    )
    ensure_table('signals',
        sa.Column('id', sa.String(), nullable=False, primary_key=True),
        sa.Column('dedup_key', sa.String(), nullable=False, primary_key=False),
        sa.Column('account_id', sa.String(), sa.ForeignKey('accounts.id'), nullable=False, primary_key=False),
        sa.Column('symbol', sa.String(), nullable=False, primary_key=False),
        sa.Column('side', sa.String(), nullable=False, primary_key=False),
        sa.Column('status', sa.String(), nullable=False, primary_key=False),
        sa.Column('reason', sa.String(), nullable=False, primary_key=False),
        sa.Column('risk_result', sa.String(), nullable=False, primary_key=False),
        sa.Column('run_id', sa.String(), nullable=False, primary_key=False),
        sa.Column('version', sa.Integer(), nullable=False, primary_key=False),
        sa.Column('created_at', sa.String(), nullable=False, primary_key=False),
        sa.Column('expires_at', sa.String(), nullable=False, primary_key=False),
        sa.Column('evidence', sa.JSON(), nullable=False, primary_key=False),
        sa.UniqueConstraint('dedup_key', name=None),
    )
    ensure_index('ix_signals_symbol', 'signals', ['symbol'], unique=False)
    ensure_table('order_intents',
        sa.Column('id', sa.String(), nullable=False, primary_key=True),
        sa.Column('dedup_key', sa.String(), nullable=False, primary_key=False),
        sa.Column('signal_id', sa.String(), sa.ForeignKey('signals.id'), nullable=False, primary_key=False),
        sa.Column('account_id', sa.String(), sa.ForeignKey('accounts.id'), nullable=False, primary_key=False),
        sa.Column('symbol', sa.String(), nullable=False, primary_key=False),
        sa.Column('side', sa.String(), nullable=False, primary_key=False),
        sa.Column('quantity', sa.Text(), nullable=False, primary_key=False),
        sa.Column('limit_price', sa.Text(), nullable=False, primary_key=False),
        sa.Column('reserved_cash', sa.Text(), nullable=False, primary_key=False),
        sa.Column('reserved_quantity', sa.Text(), nullable=False, primary_key=False),
        sa.Column('status', sa.String(), nullable=False, primary_key=False),
        sa.Column('mode', sa.String(), nullable=False, primary_key=False),
        sa.Column('strategy', sa.String(), nullable=False, primary_key=False),
        sa.Column('version', sa.Integer(), nullable=False, primary_key=False),
        sa.Column('run_id', sa.String(), nullable=False, primary_key=False),
        sa.Column('created_at', sa.String(), nullable=False, primary_key=False),
        sa.Column('data', sa.JSON(), nullable=False, primary_key=False),
        sa.UniqueConstraint('dedup_key', name=None),
    )
    ensure_table('broker_orders',
        sa.Column('id', sa.String(), nullable=False, primary_key=True),
        sa.Column('intent_id', sa.String(), sa.ForeignKey('order_intents.id'), nullable=False, primary_key=False),
        sa.Column('account_id', sa.String(), sa.ForeignKey('accounts.id'), nullable=False, primary_key=False),
        sa.Column('broker_id', sa.String(), nullable=True, primary_key=False),
        sa.Column('client_order_id', sa.String(), nullable=False, primary_key=False),
        sa.Column('symbol', sa.String(), nullable=False, primary_key=False),
        sa.Column('side', sa.String(), nullable=False, primary_key=False),
        sa.Column('quantity', sa.Text(), nullable=False, primary_key=False),
        sa.Column('filled_quantity', sa.Text(), nullable=False, primary_key=False),
        sa.Column('limit_price', sa.Text(), nullable=False, primary_key=False),
        sa.Column('status', sa.String(), nullable=False, primary_key=False),
        sa.Column('owner', sa.String(), nullable=False, primary_key=False),
        sa.Column('mode', sa.String(), nullable=False, primary_key=False),
        sa.Column('submitted_at', sa.String(), nullable=False, primary_key=False),
        sa.Column('expires_at', sa.String(), nullable=False, primary_key=False),
        sa.Column('cancel_requested_at', sa.String(), nullable=True, primary_key=False),
        sa.Column('last_match_at', sa.String(), nullable=True, primary_key=False),
        sa.Column('message', sa.String(), nullable=False, primary_key=False),
        sa.UniqueConstraint('broker_id', name=None),
        sa.UniqueConstraint('client_order_id', name=None),
        sa.UniqueConstraint('intent_id', name=None),
    )
    ensure_table('fills',
        sa.Column('id', sa.String(), nullable=False, primary_key=True),
        sa.Column('broker_fill_id', sa.String(), nullable=False, primary_key=False),
        sa.Column('order_id', sa.String(), sa.ForeignKey('broker_orders.id'), nullable=False, primary_key=False),
        sa.Column('account_id', sa.String(), sa.ForeignKey('accounts.id'), nullable=False, primary_key=False),
        sa.Column('symbol', sa.String(), nullable=False, primary_key=False),
        sa.Column('side', sa.String(), nullable=False, primary_key=False),
        sa.Column('quantity', sa.Text(), nullable=False, primary_key=False),
        sa.Column('price', sa.Text(), nullable=False, primary_key=False),
        sa.Column('fee', sa.Text(), nullable=False, primary_key=False),
        sa.Column('timestamp', sa.String(), nullable=False, primary_key=False),
        sa.Column('realized_pnl', sa.Text(), nullable=False, primary_key=False),
        sa.UniqueConstraint('broker_fill_id', name=None),
    )
    ensure_table('settlements',
        sa.Column('id', sa.String(), nullable=False, primary_key=True),
        sa.Column('fill_id', sa.String(), sa.ForeignKey('fills.id'), nullable=False, primary_key=False),
        sa.Column('account_id', sa.String(), sa.ForeignKey('accounts.id'), nullable=False, primary_key=False),
        sa.Column('amount', sa.Text(), nullable=False, primary_key=False),
        sa.Column('settle_date', sa.String(), nullable=False, primary_key=False),
        sa.Column('settled', sa.Boolean(), nullable=False, primary_key=False),
        sa.UniqueConstraint('fill_id', name=None),
    )
    ensure_table('service_jobs',
        sa.Column('id', sa.String(64), nullable=False, primary_key=True),
        sa.Column('kind', sa.String(40), nullable=True, primary_key=False),
        sa.Column('status', sa.String(20), nullable=True, primary_key=False),
        sa.Column('payload', sa.Text(), nullable=True, primary_key=False),
        sa.Column('result', sa.Text(), nullable=True, primary_key=False),
        sa.Column('created_at', sa.String(50), nullable=True, primary_key=False),
        sa.Column('updated_at', sa.String(50), nullable=True, primary_key=False),
    )
    ensure_index('ix_service_jobs_kind', 'service_jobs', ['kind'], unique=False)
    ensure_index('ix_service_jobs_status', 'service_jobs', ['status'], unique=False)
    ensure_table('service_state',
        sa.Column('key', sa.String(100), nullable=False, primary_key=True),
        sa.Column('value', sa.Text(), nullable=False, primary_key=False),
        sa.Column('updated_at', sa.String(50), nullable=True, primary_key=False),
    )

def downgrade():
    raise RuntimeError('Trading ledger downgrade is destructive; restore a verified backup instead.')
