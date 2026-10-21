"""Real production builder/handlers with disposable SQLite and fake Telegram HTTP.

Run from repository root. The actual post_init runs with fake external I/O; polling, webhook registration,
background payment/reminder ticks are never started.
"""
import asyncio
import importlib
import inspect
import json
import os
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch, AsyncMock

REPO = 'filedropbot'
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
TEMP = tempfile.TemporaryDirectory(prefix='ptb-dispatch-contract-')
os.environ.update({
    'BOT_TOKEN': '123456:offline-contract-token', 'ALLOWED_USER_ID': '42',
    'SUPER_ADMIN_ID': '900001', 'DASHBOARD_SECRET': 'offline-contract-secret-please-replace',
    'WEBHOOK_URL': 'https://offline.invalid', 'WEBHOOK_SECRET': 'offline-contract-webhook-secret',
    'DB_PATH': str(Path(TEMP.name) / 'state.sqlite'), 'DATA_DIR': str(Path(TEMP.name) / 'data'),
    'HOST_DATA_DIR': '/offline/operator-data', 'KASPI_API_KEY': '', 'KASPI_PAY_API_KEY': '',
    'PAID_WEBHOOK_URL': '', 'META_ACCESS_TOKEN': '', 'META_PIXEL_ID': '',
    'ROBO_LOGIN': '', 'ROBO_PASS1': '', 'ROBO_PASS2': '', 'PRODAMUS_SECRET': '',
    'CHANNEL_ID': '0', 'CHANNEL_ID_15K': '0', 'VIP_CHAT_ID': '0',
    'CLUB_CHAT_ID': '0', 'CLUB_VIP_CHAT_ID': '0', 'GROUP_CHAT_ID': '0',
    'HOMEWORK_GROUP_ID': '0', 'CRYPTOPAY_API_TOKEN': '', 'CRYPTO_PAY_API_TOKEN': '',
})

from telegram import Update
from telegram.ext import Application
from telegram.request import BaseRequest


class OfflineTelegram(BaseRequest):
    def __init__(self):
        self.calls = []

    @property
    def read_timeout(self):
        return 5

    async def initialize(self):
        pass

    async def shutdown(self):
        pass

    async def do_request(self, url, method, request_data=None, **kwargs):
        if '/file/bot' in url:
            return 200, b'offline document bytes'
        endpoint = url.rsplit('/', 1)[-1]
        params = request_data.parameters if request_data else {}
        self.calls.append((endpoint, params))
        if endpoint == 'getMe':
            result = {'id': 123456, 'is_bot': True, 'first_name': 'Offline', 'username': 'offline_bot'}
        elif endpoint == 'getFile':
            result = {'file_id': 'file', 'file_unique_id': 'unique', 'file_path': 'document.txt'}
        elif endpoint in {'setChatMenuButton', 'setMyCommands'}:
            result = True
        elif endpoint == 'sendMessage':
            result = {'message_id': len(self.calls), 'date': 1700000000,
                      'chat': {'id': int(params['chat_id']), 'type': 'private'}, 'text': params['text']}
        else:
            raise AssertionError('Unexpected Telegram HTTP action: ' + endpoint)
        return 200, json.dumps({'ok': True, 'result': result}).encode()


async def checked_startup(callback):
    """Keep real Task semantics and retain finished startup tasks for inspection."""
    loop = asyncio.get_running_loop()
    previous_factory = loop.get_task_factory()
    created = []
    errors = []

    def tracking_factory(task_loop, coro, **kwargs):
        task = (previous_factory(task_loop, coro, **kwargs) if previous_factory
                else asyncio.Task(coro, loop=task_loop, **kwargs))
        created.append(task)
        return task

    loop.set_task_factory(tracking_factory)
    try:
        await callback()
        await asyncio.sleep(0)  # Let actual startup tasks enter once.
    finally:
        try:
            inspected = set()
            while pending_inspection := [task for task in created if task not in inspected]:
                for task in pending_inspection:
                    if not task.done():
                        task.cancel()  # Dormant timer/poller, no payment tick.
                outcomes = await asyncio.gather(*pending_inspection, return_exceptions=True)
                errors.extend(outcome for outcome in outcomes
                              if isinstance(outcome, BaseException)
                              and not isinstance(outcome, asyncio.CancelledError))
                inspected.update(pending_inspection)
        finally:
            loop.set_task_factory(previous_factory)
    assert not errors, 'Startup background task failed: ' + '; '.join(map(repr, errors))
    return created


async def verify_startup_failure_detection():
    """Completed crashes and crashes during cancellation must fail the gate."""
    for during_cancel in (False, True):
        marker = 'startup-failure-control-' + str(during_cancel)

        async def external_failure():
            if during_cancel:
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    raise RuntimeError(marker)
            raise RuntimeError(marker)

        async def startup_callback():
            asyncio.create_task(external_failure())
            await asyncio.sleep(0)  # First control finishes before callback returns.

        try:
            await checked_startup(startup_callback)
        except AssertionError as exc:
            assert marker in str(exc), exc
        else:
            raise AssertionError('Startup background failure was not detected: ' + marker)


def update(bot, *, user=42, document=False):
    message = {'message_id': 1, 'date': 1700000000, 'chat': {'id': user, 'type': 'private'},
               'from': {'id': user, 'is_bot': False, 'first_name': 'Offline'}}
    if document:
        message['document'] = {'file_id': 'file', 'file_unique_id': 'unique',
                               'file_name': 'document.txt', 'mime_type': 'text/plain', 'file_size': 22}
    else:
        message.update(text='Hello world', entities=[{'type': 'bold', 'offset': 0, 'length': 5}])
    return Update.de_json({'update_id': user + int(document), 'message': message}, bot)


captured = []


def capture_launch(self, *args, **kwargs):
    captured.append(self)


module = importlib.import_module('bot' if REPO in {'filedropbot', 'textformating2htmlbot'} else 'bot.main')
# Patch only the terminal launch. Production main still builds its real
# Application, request objects, rate limiter, job queue and full handler table.
with patch.object(Application, 'run_webhook', capture_launch), patch.object(Application, 'run_polling', capture_launch):
    module.main()
assert len(captured) == 1, 'Production main did not build exactly one application'
app = captured[0]
assert app.handlers, 'Missing production handler registrations'


async def run():
    transport = OfflineTelegram()
    # Replace only Telegram HTTP boundary after capturing the production builder.
    for request in app.bot._request:
        await request.shutdown()
    object.__setattr__(app.bot, '_request', (transport, transport))
    errors = []

    async def capture_error(update, context):
        errors.append(repr(context.error))

    app.add_error_handler(capture_error)
    if REPO != 'filedropbot':
        db = importlib.import_module('db')
        init = getattr(db, 'init_db', None) or getattr(db, 'init')
        result = init()
        if inspect.isawaitable(result):
            await result
        if REPO != 'textformating2htmlbot':
            # Existing ordinary user, no wizard/purchase intent. This is real
            # DB state, never a mocked filter/context or handler.
            upsert = db.upsert_user
            kwargs = {'funnel_version': 5} if 'funnel_version' in inspect.signature(upsert).parameters else {}
            upsert(42, **kwargs)
    async with app:
        # Execute the real startup callback: do not replace DB restore, imports,
        # scheduler registration or task creation. Only the dashboard socket
        # bind is an external fake; Telegram HTTP already uses OfflineTelegram.
        async def execute_post_init():
            if hasattr(module, 'dashboard'):
                dashboard_io = AsyncMock()
                with patch.object(module.dashboard, 'start_dashboard', dashboard_io):
                    await app.post_init(app)
                dashboard_io.assert_awaited_once()
            else:
                await app.post_init(app)
        await verify_startup_failure_detection()
        startup_tasks = await checked_startup(execute_post_init)
        jobs = {job.name: job for job in app.job_queue.jobs()} if app.job_queue else {}
        if REPO in {'AishaAsyabot', 'contrastbot', 'karinaugcbot', 'kunuspekovabotasya', 'malikaxoxobot'}:
            expected = {'daily_backup', 'daily_admin_report'}
            if REPO in {'kunuspekovabotasya', 'malikaxoxobot'}:
                expected.add('access_expiry_scan')
            if REPO == 'karinaugcbot':
                expected |= {'topup_reminder', 'topup_deadline', 'access_expiry_sweep'}
            if REPO == 'contrastbot':
                expected |= {'topup_reminder', 'topup_deadline', 'daily_hw_report'}
            assert expected <= jobs.keys(), (expected - jobs.keys())
            from datetime import datetime, timezone
            from zoneinfo import ZoneInfo
            reference = datetime(2026, 10, 21, tzinfo=timezone.utc)
            for name, hour in [('daily_backup', 3), ('daily_admin_report', 20)]:
                scheduled = jobs[name].job.trigger.get_next_fire_time(None, reference)
                local = scheduled.astimezone(ZoneInfo('Asia/Almaty'))
                assert (local.hour, local.minute) == (hour, 0), (name, local)
        if REPO == 'filedropbot':
            assert {'cleanup', '_drain_outbox'} <= jobs.keys(), jobs.keys()
            assert any(endpoint == 'setMyCommands' for endpoint, _ in transport.calls)
        if REPO != 'textformating2htmlbot':
            assert app.job_queue is not None, 'Production reminder/file queues unavailable'
            async def noop(context):
                raise AssertionError('Scheduler must remain stopped in smoke test')
            job = app.job_queue.run_once(noop, when=3600, name='offline-smoke')
            assert app.job_queue.get_jobs_by_name('offline-smoke') == (job,)
            job.schedule_removal()
        before = len(transport.calls)
        await app.process_update(update(app.bot))
        text_calls = [params for endpoint, params in transport.calls[before:] if endpoint == 'sendMessage']
        if REPO == 'filedropbot':
            assert not text_calls, 'File-only bot unexpectedly consumed ordinary text'
            before = len(transport.calls)
            await app.process_update(update(app.bot, user=43, document=True))
            assert len(transport.calls) == before, 'Unauthorized media passed production filter'
        else:
            assert text_calls, 'Ordinary user text did not reach real fallback/converter'
            if REPO == 'textformating2htmlbot':
                assert any('&lt;b&gt;Hello&lt;/b&gt; world' in c['text'] for c in text_calls), text_calls
        if REPO != 'textformating2htmlbot':
            before = len(transport.calls)
            await app.process_update(update(app.bot, document=True))
            replies = [params['text'] for endpoint, params in transport.calls[before:] if endpoint == 'sendMessage']
            assert replies, 'Document did not reach production handler'
            if REPO == 'filedropbot':
                saved = list(module.DROPS_DIR.rglob('*.txt'))
                assert len(saved) == 1 and saved[0].read_bytes() == b'offline document bytes'
            else:
                assert not any(endpoint == 'getFile' for endpoint, _ in transport.calls), 'No-intent/non-PDF receipt must not download'
        assert not errors, errors
    print(json.dumps({'repo': REPO, 'production_builder': True, 'real_dispatch': True,
                      'job_queue': REPO != 'textformating2htmlbot', 'post_init': True,
                      'scheduled_jobs': sorted(jobs), 'startup_tasks_checked': len(startup_tasks),
                      'startup_failure_controls': 2, 'handler_errors': errors,
                      'telegram_calls': [endpoint for endpoint, _ in transport.calls]}))


try:
    asyncio.run(run())
finally:
    TEMP.cleanup()
