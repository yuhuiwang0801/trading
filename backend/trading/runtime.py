"""Durable operational state and work queue, separate from the trading ledger."""
from datetime import datetime, timezone, timedelta
import uuid
from sqlalchemy import create_engine, MetaData, Table, Column, String, Text, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
import json

metadata = MetaData()
state_table = Table('service_state', metadata, Column('key', String(100), primary_key=True),
                    Column('value', Text, nullable=False), Column('updated_at', String(50)))
jobs_table = Table('service_jobs', metadata, Column('id', String(64), primary_key=True),
                   Column('kind', String(40), index=True), Column('status', String(20), index=True),
                   Column('payload', Text), Column('result', Text), Column('created_at', String(50)),
                   Column('updated_at', String(50)))

def utcnow(): return datetime.now(timezone.utc)
def iso(): return utcnow().isoformat()

class RuntimeStore:
    def __init__(self, database_url):
        kw = {'connect_args': {'timeout': 30, 'check_same_thread': False}} if database_url.startswith('sqlite') else {}
        self.engine = create_engine(database_url, **kw)
        metadata.create_all(self.engine)

    def put(self, key, value):
        row = {'key': key, 'value': json.dumps(value, default=str), 'updated_at': iso()}
        with self.engine.begin() as c:
            if self.engine.dialect.name == 'sqlite':
                s = sqlite_insert(state_table).values(**row)
                c.execute(s.on_conflict_do_update(index_elements=['key'], set_=row))
            elif self.engine.dialect.name == 'postgresql':
                from sqlalchemy.dialects.postgresql import insert
                s = insert(state_table).values(**row)
                c.execute(s.on_conflict_do_update(index_elements=['key'], set_=row))
            else:
                if c.execute(select(state_table.c.key).where(state_table.c.key == key)).first():
                    c.execute(update(state_table).where(state_table.c.key == key).values(**row))
                else: c.execute(state_table.insert().values(**row))

    def get(self, key, default=None):
        with self.engine.connect() as c:
            row = c.execute(select(state_table).where(state_table.c.key == key)).mappings().first()
            return json.loads(row['value']) if row else default

    def enqueue(self, kind, payload=None):
        row = {'id': str(uuid.uuid4()), 'kind': kind, 'status': 'queued', 'payload': json.dumps(payload or {}),
               'result': '{}', 'created_at': iso(), 'updated_at': iso()}
        with self.engine.begin() as c: c.execute(jobs_table.insert().values(**row))
        return {**row, 'payload': payload or {}, 'result': {}}

    def claim(self, kinds):
        with self.engine.begin() as c:
            # Atomic conditional update. Losing workers receive no job.
            row = c.execute(select(jobs_table).where(jobs_table.c.kind.in_(kinds), jobs_table.c.status == 'queued')
                            .order_by(jobs_table.c.created_at).limit(1)).mappings().first()
            if not row: return None
            result = c.execute(update(jobs_table).where(jobs_table.c.id == row['id'], jobs_table.c.status == 'queued')
                               .values(status='running', updated_at=iso()))
            if result.rowcount != 1: return None
            return {**row, 'payload': json.loads(row['payload'])}

    def finish(self, job_id, result, status='completed'):
        with self.engine.begin() as c:
            c.execute(update(jobs_table).where(jobs_table.c.id == job_id).values(status=status,
                       result=json.dumps(result, default=str), updated_at=iso()))

    def recover_jobs(self, kinds):
        # Interrupted research is safe to mark failed; never blindly replay broker side effects.
        with self.engine.begin() as c:
            c.execute(update(jobs_table).where(jobs_table.c.kind.in_(kinds), jobs_table.c.status == 'running')
                      .values(status='interrupted', result=json.dumps({'error': 'Process interrupted; inspect ledger before retry'}), updated_at=iso()))

    def jobs(self, kind=None):
        with self.engine.connect() as c:
            q = select(jobs_table).order_by(jobs_table.c.created_at.desc()).limit(100)
            if kind: q = q.where(jobs_table.c.kind == kind)
            return [{**r, 'payload': json.loads(r['payload']), 'result': json.loads(r['result'] or '{}')}
                    for r in c.execute(q).mappings()]
