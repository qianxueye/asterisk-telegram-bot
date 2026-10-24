import ast
import asyncio
import contextlib
import html
import io
from pathlib import Path
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch


class ReceiptTimeoutTests(unittest.IsolatedAsyncioTestCase):
    async def test_timeout_preserves_the_known_submission_result(self):
        path = Path(__file__).resolve().parents[1] / 'asterisk_bot.py'
        tree = ast.parse(path.read_text())
        method = next(node for node in ast.walk(tree)
                      if isinstance(node, ast.AsyncFunctionDef) and node.name == 'cleanup_pending_sms_sends')
        namespace = {'asyncio': asyncio, 'time': time}
        exec(compile(ast.fix_missing_locations(ast.Module(body=[method], type_ignores=[])),
                     str(path), 'exec'), namespace)
        now = time.time()
        for status, age, title, detail, removed in [
            ('queued', 700, '短信发送尚未确认', '尚未收到模块发送结果，发送状态未知。', True),
            ('sent', 700, '短信已发出，未收到送达报告', '模块已确认发出', True),
            ('sent', 200, None, None, False),
            ('completed', 700, None, None, True),
        ]:
            with self.subTest(status=status, age=age):
                record = {'device_id': 'quectel1', 'phone_number': '+10000000000',
                          'content': '<synthetic>', 'chat_id': 1, 'timestamp': now-age,
                          'last_result_at': now-70, 'status': status}
                bot = SimpleNamespace(running=True, pending_sms_sends={'request': record},
                                      send_message=AsyncMock(), escape_html=html.escape,
                                      preview_text=lambda text: text)

                async def stop_after_one_pass(delay):
                    bot.running = False

                with patch('asyncio.sleep', stop_after_one_pass), contextlib.redirect_stdout(io.StringIO()):
                    await namespace['cleanup_pending_sms_sends'](bot)
                self.assertEqual('request' not in bot.pending_sms_sends, removed)
                if title:
                    bot.send_message.assert_awaited_once()
                    chat, message = bot.send_message.await_args.args
                    self.assertEqual(chat, 1)
                    self.assertIn(title, message)
                    self.assertIn(detail, message)
                    self.assertIn('&lt;synthetic&gt;', message)
                    if status == 'sent':
                        self.assertIn('这不代表发送失败', message)
                        self.assertNotIn('发送状态未知', message)
                else:
                    bot.send_message.assert_not_awaited()


if __name__ == '__main__':
    unittest.main()
