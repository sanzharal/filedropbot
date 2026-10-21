"""Real production builder/handlers with disposable SQLite and fake Telegram HTTP.

Run from repository root. No post_init hooks, polling, webhook registration,
background workers or payment services are started.
"""
import asyncio
import importlib
import inspect
import json
import os
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch

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
        elif endpoint == 'sendMessage':
            result = {'message_id': len(self.calls), 'date': 1700000000,
                      'chat': {'id': int(params['chat_id']), 'type': 'private'}, 'text': params['text']}
        else:
            raise AssertionError('Unexpected Telegram HTTP action: ' + endpoint)
        return 200, json.dumps({'ok': True, 'result': result}).encode()


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
                      'job_queue': REPO != 'textformating2htmlbot', 'handler_errors': errors,
                      'telegram_calls': [endpoint for endpoint, _ in transport.calls]}))


try:
    asyncio.run(run())
finally:
    TEMP.cleanup()
