import asyncio
from contextlib import contextmanager
from pathlib import Path
import sqlite3
import subprocess
import sys
from threading import Lock
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import server
import mail_sync
import outlook_mcp
import item_context


def test_context_worker_recovers_from_transient_database_failure(monkeypatch):
    async def exercise():
        recovered = asyncio.Event()
        service = object.__new__(item_context.ItemContextService)
        attempts = 0

        async def process_next():
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise sqlite3.OperationalError('unable to open database file')
            recovered.set()
            return False

        async def fast_sleep(seconds):
            await asyncio.sleep(0)

        service.process_next = process_next
        monkeypatch.setattr(item_context, 'asyncio', SimpleNamespace(sleep=fast_sleep))
        task = asyncio.create_task(service.loop())
        try:
            await asyncio.wait_for(recovered.wait(), 1)
            assert not task.done()
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(exercise())


@pytest.mark.skipif(sys.platform == 'win32', reason='POSIX file descriptor limits')
def test_database_reads_do_not_exhaust_descriptors_without_garbage_collection(tmp_path):
    # Exercise the actual connection helper under the launch agent's small FD limit.
    script = '''
import gc, resource, sqlite3, sys
from pathlib import Path
import server
server.DB_PATH = Path(sys.argv[1])
resource.setrlimit(resource.RLIMIT_NOFILE, (128, resource.getrlimit(resource.RLIMIT_NOFILE)[1]))
gc.disable()
for _ in range(1200):
    with server.connect() as con:
        con.execute('CREATE TABLE IF NOT EXISTS probe (value INTEGER)')
        con.execute('SELECT COUNT(*) FROM probe').fetchone()
try:
    con.execute('SELECT 1')
    raise AssertionError('Connection was not closed')
except sqlite3.ProgrammingError:
    pass
with server.connect() as con:
    con.execute('INSERT INTO probe VALUES (1)')
try:
    with server.connect() as con:
        con.execute('INSERT INTO probe VALUES (2)')
        raise ValueError('rollback')
except ValueError:
    pass
with server.connect() as con:
    assert con.execute('SELECT value FROM probe').fetchall()[0][0] == 1
    assert con.execute('SELECT COUNT(*) FROM probe').fetchone()[0] == 1
'''
    result = subprocess.run([sys.executable, '-c', script, str(tmp_path/'probe.sqlite3')],
                            cwd=Path(server.__file__).parent, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize('failure_stage', ['read', 'trigger'])
def test_scheduler_recovers_from_transient_failure(failure_stage, monkeypatch):
    attempts = {'read': 0, 'trigger': 0}

    class Connection:
        def execute(self, query):
            return self

        def fetchone(self):
            return {'enabled': 1, 'next_run_at': '2026-10-09T10:00:00Z'}

    @contextmanager
    def connect():
        attempts['read'] += 1
        if failure_stage == 'read' and attempts['read'] == 1:
            raise sqlite3.OperationalError('unable to open database file')
        yield Connection()

    async def exercise():
        recovered = asyncio.Event()
        service = object.__new__(mail_sync.MailSyncService)
        service.host = SimpleNamespace(DB_LOCK=Lock(), connect=connect)
        service.scheduler_last_tick = None
        service.scheduler_error = None

        def trigger(reason):
            assert reason == 'hourly'
            attempts['trigger'] += 1
            if failure_stage == 'trigger' and attempts['trigger'] == 1:
                raise sqlite3.OperationalError('database is locked')
            recovered.set()

        service.trigger = trigger
        task = asyncio.create_task(service.schedule_loop())
        try:
            await asyncio.wait_for(recovered.wait(), 1)
            assert not task.done()
            assert service.scheduler_error is None
            assert service.scheduler_last_tick
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    monkeypatch.setattr(mail_sync, 'SCHEDULER_POLL_SECONDS', .01)
    asyncio.run(exercise())


def test_mcp_timeout_identifies_stage_without_logging_private_arguments(monkeypatch, caplog):
    class Input:
        def write(self, data):
            pass

        async def drain(self):
            pass

    class Output:
        async def readline(self):
            await asyncio.Event().wait()

    monkeypatch.setattr(outlook_mcp, 'RPC_TIMEOUT_SECONDS', .01)
    client = outlook_mcp.OutlookMCP(SimpleNamespace(stdin=Input(), stdout=Output()))
    with pytest.raises(outlook_mcp.OutlookError, match='读取邮件超时'):
        asyncio.run(client.rpc('mcpServer/tool/call', {
            'tool': 'microsoft_outlook_email.list_messages', 'arguments': {'folder_id': 'private-folder'}}))
    assert 'microsoft_outlook_email.list_messages' in caplog.text
    assert 'private-folder' not in caplog.text
