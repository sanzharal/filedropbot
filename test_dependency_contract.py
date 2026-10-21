"""Exercise PTB dispatch and file delivery with an in-memory Telegram transport."""
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

os.environ["BOT_TOKEN"] = "123456:offline-test-token"
os.environ["ALLOWED_USER_ID"] = "42"

from telegram import Update
from telegram.ext import Application, MessageHandler, filters
from telegram.request import BaseRequest
from telegram.error import NetworkError

import bot as service


class OfflineTelegram(BaseRequest):
    def __init__(self):
        self.calls = []
        self.fail_upload = False

    async def initialize(self):
        pass

    async def shutdown(self):
        pass

    async def do_request(self, url, method, request_data=None, **kwargs):
        endpoint = url.rsplit("/", 1)[-1]
        params = request_data.parameters if request_data else {}
        self.calls.append((endpoint, params))
        if "/file/bot" in url:
            return 200, b"the uploaded bytes"
        if endpoint == "getMe":
            result = {"id": 123456, "is_bot": True, "first_name": "Offline", "username": "offline_bot"}
        elif endpoint == "getFile":
            result = {"file_id": "file-1", "file_unique_id": "unique-1", "file_path": "docs/test.bin"}
        elif endpoint == "sendDocument" and self.fail_upload:
            raise NetworkError("offline simulated failure")
        elif endpoint in {"sendMessage", "sendDocument"}:
            result = {"message_id": 99, "date": 1, "chat": {"id": 42, "type": "private"}, "text": params.get("text", "sent")}
        else:
            raise AssertionError(f"Unexpected Telegram endpoint: {endpoint}")
        return 200, json.dumps({"ok": True, "result": result}).encode()


class DeliveryContract(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        service.DATA_DIR = Path(self.tmp.name)
        service.DROPS_DIR = service.DATA_DIR / "drops"
        service.OUTBOX_DIR = service.DATA_DIR / "outbox"
        service.SENT_DIR = service.DATA_DIR / "sent"
        service.HOST_DATA_DIR = "/host/operator-data"
        service.OUTBOX_DIR.mkdir()
        self.transport = OfflineTelegram()
        self.app = (Application.builder().token(service.BOT_TOKEN)
                    .request(self.transport).get_updates_request(OfflineTelegram()).build())
        self.app.add_handler(MessageHandler(filters.User(42) & filters.Document.ALL, service.handle_media))
        await self.app.initialize()

    async def asyncTearDown(self):
        await self.app.shutdown()
        self.tmp.cleanup()

    def update(self, user=42, size=18):
        return Update.de_json({"update_id": 1, "message": {
            "message_id": 7, "date": 1, "chat": {"id": user, "type": "private"},
            "from": {"id": user, "is_bot": False, "first_name": "Test"},
            "document": {"file_id": "file-1", "file_unique_id": "unique-1", "file_name": "../name.bin", "file_size": size},
        }}, self.app.bot)

    async def test_authorized_document_dispatch_saves_bytes_and_reports_host_path(self):
        await self.app.process_update(self.update())
        files = list(service.DROPS_DIR.rglob("*.bin"))
        self.assertEqual(len(files), 1)
        self.assertEqual(files[0].read_bytes(), b"the uploaded bytes")
        messages = [p["text"] for name, p in self.transport.calls if name == "sendMessage"]
        self.assertEqual(len(messages), 1)
        self.assertIn("/host/operator-data/drops/", messages[0])

    async def test_unauthorized_document_never_reaches_download_or_reply(self):
        self.transport.calls.clear()
        await self.app.process_update(self.update(user=43))
        self.assertEqual(self.transport.calls, [])
        self.assertFalse(service.DROPS_DIR.exists())

    async def test_oversize_is_replied_to_without_download(self):
        await self.app.process_update(self.update(size=service.TELEGRAM_DOWNLOAD_LIMIT + 1))
        self.assertFalse(any(name == "getFile" for name, _ in self.transport.calls))
        self.assertTrue(any("skipped" in p.get("text", "") for _, p in self.transport.calls))

    async def test_outbox_upload_archives_only_after_success_and_keeps_failed_file(self):
        path = service.OUTBOX_DIR / "report.bin"
        path.write_bytes(b"document bytes")
        path.with_name(path.name + service._CAPTION_SUFFIX).write_text("Readable caption")
        os.utime(path, (time.time() - 60, time.time() - 60))
        context = SimpleNamespace(bot=self.app.bot, application=self.app)
        self.transport.fail_upload = True
        await service._drain_outbox(context)
        self.assertTrue(path.exists())
        self.assertFalse(list(service.SENT_DIR.rglob("report.bin")))
        self.transport.fail_upload = False
        await service._drain_outbox(context)
        self.assertFalse(path.exists())
        self.assertEqual(next(service.SENT_DIR.rglob("report.bin")).read_bytes(), b"document bytes")
        self.assertTrue(any(p.get("caption") == "Readable caption" for name, p in self.transport.calls if name == "sendDocument"))


if __name__ == "__main__":
    unittest.main()
