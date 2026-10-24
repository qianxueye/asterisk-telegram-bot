import ast
import asyncio
import contextlib
from datetime import datetime
import io
from pathlib import Path
import time
from types import SimpleNamespace
from typing import Optional
import unittest
from unittest.mock import AsyncMock, patch


def receipt_class():
    path = Path(__file__).resolve().parents[1] / 'asterisk_bot.py'
    tree = ast.parse(path.read_text())
    cls = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == 'AsteriskBot')
    names = {'handle_sms_send_result', 'find_pending_sms_send', 'format_sms_send_result_message',
             'cleanup_pending_sms_sends', 'escape_html', 'preview_text'}
    cls.body = [node for node in cls.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name in names]
    namespace = {'asyncio': asyncio, 'datetime': datetime, 'time': time, 'Optional': Optional,
                 'Config': SimpleNamespace(AUTHORIZED_USERS={1})}
    exec(compile(ast.fix_missing_locations(ast.Module(body=[cls], type_ignores=[])),
                 str(path), 'exec'), namespace)
    return namespace['AsteriskBot']


Receipt = receipt_class()


class ModuleReceiptTests(unittest.IsolatedAsyncioTestCase):
    def bot(self, notifications=True):
        bot = Receipt()
        bot.running = True
        bot.sms_notifications = notifications
        bot.send_message = AsyncMock(return_value=True)
        bot.pending_sms_sends = {'request': {
            'device_id': 'quectel1', 'phone_number': '+10000000000', 'content': 'synthetic',
            'timestamp': time.time(), 'status': 'queued', 'chat_id': 10,
        }}
        return bot

    def result(self, kind='send_success'):
        return {'device': 'quectel1', 'sms_id': '1', 'ref_id': '210', 'phone_number': None,
                'status_code': '0' if kind == 'status_report' else None,
                'message_type': kind, 'success': kind != 'send_error'}

    async def apply(self, bot, result):
        with contextlib.redirect_stdout(io.StringIO()):
            await bot.handle_sms_send_result(result)

    async def test_module_success_is_final_and_not_recipient_delivery(self):
        bot = self.bot()
        await self.apply(bot, self.result())
        self.assertEqual(bot.pending_sms_sends['request']['status'], 'completed')
        bot.send_message.assert_awaited_once()
        self.assertEqual(bot.send_message.await_args.args[0], 10)
        message = bot.send_message.await_args.args[1]
        self.assertIn('模块已确认发出', message)
        self.assertIn('不代表对方已收到', message)

    async def test_carrier_reports_do_not_complete_or_notify_the_send(self):
        bot = self.bot()
        for kind in ('status_report', 'report_success'):
            await self.apply(bot, self.result(kind))
        self.assertEqual(bot.pending_sms_sends['request']['status'], 'queued')
        bot.send_message.assert_not_awaited()
        await self.apply(bot, self.result())
        bot.send_message.reset_mock()
        await self.apply(bot, self.result('status_report'))
        bot.send_message.assert_not_awaited()
        self.assertEqual(bot.pending_sms_sends['request']['status'], 'completed')

    async def test_module_completion_does_not_depend_on_notification_delivery(self):
        for notifications in (False, True):
            with self.subTest(notifications=notifications):
                bot = self.bot(notifications)
                bot.send_message.return_value = None
                await self.apply(bot, self.result())
                record = bot.pending_sms_sends['request']
                self.assertEqual(record['status'], 'completed')
                record.update(timestamp=time.time()-1000, last_result_at=time.time()-100)
                bot.send_message.reset_mock()

                async def stop(delay):
                    bot.running = False

                with patch('asyncio.sleep', stop), contextlib.redirect_stdout(io.StringIO()):
                    await bot.cleanup_pending_sms_sends()
                bot.send_message.assert_not_awaited()
                self.assertNotIn('request', bot.pending_sms_sends)

    async def test_module_failure_is_final_but_unrelated_ack_errors_are_not(self):
        bot = self.bot()
        await self.apply(bot, self.result('ack_error'))
        self.assertEqual(bot.pending_sms_sends['request']['status'], 'queued')
        bot.send_message.reset_mock()
        await self.apply(bot, self.result('send_error'))
        self.assertEqual(bot.pending_sms_sends['request']['status'], 'completed')
        self.assertIn('短信发送失败', bot.send_message.await_args.args[1])


if __name__ == '__main__':
    unittest.main()
