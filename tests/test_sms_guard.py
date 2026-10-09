import asyncio
import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from asterisk_bot import AsteriskBot
from config import Config
from sms_guard import SmsFloodGuard


class GuardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.now = 1000
        self.path = Path(self.temp.name) / 'state.json'
        self.guard = SmsFloodGuard(self.path, clock=lambda: self.now)

    def otp(self, n, device='quectel0', content='【平台】验证码 739204'):
        return self.guard.admit(device, content, str(n))

    def test_15_per_minute_multi_sender_boundary_and_other_sim(self):
        for number in range(14):
            self.assertEqual(self.otp(number)[0], 'allow')
        self.assertEqual(self.otp(14), ('drop', ('start', 0)))
        self.assertEqual(self.otp(15)[0], 'drop')
        self.assertEqual(self.otp(100, 'quectel1')[0], 'allow')
        self.assertEqual(self.otp(16, content='订单123456已发货')[0], 'allow')

    def test_30_per_five_minutes_rolling_boundary(self):
        for number in range(29):
            self.now = 1000 + number * 9
            self.assertEqual(self.otp(number)[0], 'allow')
        self.now += 9
        self.assertEqual(self.otp(29)[0], 'drop')
        other = SmsFloodGuard(Path(self.temp.name)/'other', clock=lambda: self.now)
        other.admit('quectel0', '验证码123456', 'a')
        self.now += 300
        self.assertEqual(other.admit('quectel0', '验证码123456', 'b')[0], 'allow')
        self.assertEqual(sum(other.record('quectel0')['buckets'].values()), 1)

    def test_recovery_counts_drops_and_requires_less_than_five(self):
        for number in range(15):
            self.otp(number)
        self.now = 1299
        for number in range(5):
            self.assertEqual(self.otp(100+number)[0], 'drop')
        self.now = 1301
        self.assertEqual(self.guard.recover(), [])
        self.now = 1600
        self.assertEqual(self.guard.recover(), [('quectel0', 6)])
        self.assertEqual(self.otp(300)[0], 'allow')

    def test_restart_manual_off_auto_and_retained_observation(self):
        self.guard.set_mode('manual', 'quectel0')
        for number in range(16):
            self.assertEqual(self.otp(number)[0], 'drop')
        self.guard.save(force=True)
        restored = SmsFloodGuard(self.path, clock=lambda: self.now)
        self.assertEqual(restored.admit('quectel0', '验证码654321', 'new')[0], 'drop')
        restored.set_mode('off', 'quectel0')
        self.assertEqual(restored.admit('quectel0', '验证码654321', 'off')[0], 'allow')
        restored.set_mode('auto', 'quectel0')
        self.assertEqual(restored.admit('quectel0', '验证码654321', 'auto')[0], 'drop')
        self.assertEqual(os.stat(self.path).st_mode & 0o777, 0o600)
        self.assertNotIn('654321', self.path.read_text())

    def test_sim_identity_follows_sim_between_slots(self):
        for number in range(15):
            self.guard.admit('quectel0', '验证码123456', str(number), '12345678901234567890')
        self.assertEqual(self.guard.admit('quectel1', '验证码123456', 'moved', '12345678901234567890')[0], 'drop')
        self.assertEqual(self.guard.admit('quectel0', '验证码123456', 'new_sim', '22345678901234567890')[0], 'allow')

    def test_duplicates_do_not_count_or_deliver_twice(self):
        self.assertEqual(self.otp(1)[0], 'allow')
        self.assertEqual(self.otp(1)[0], 'duplicate')
        self.assertEqual(sum(self.guard.record('quectel0')['buckets'].values()), 1)

    def test_classifier_and_bounded_flood_state(self):
        self.guard.set_mode('manual')
        for content in ('验证码 123456', 'Your verification code is 123456', 'OTP: A2B3C4'):
            self.assertEqual(self.otp(content, content=content)[0], 'drop')
        for content in ('123456', '验证码诈骗，请勿上当', '订单号123456', 'Ticket 123456'):
            self.assertEqual(self.otp(content, content=content)[0], 'allow')
        for number in range(5000):
            self.otp(number)
        self.assertLessEqual(len(self.guard.state['seen']), 1024)
        self.assertLessEqual(len(self.guard.record('quectel0')['buckets']), 300)

    def test_corrupt_state_and_write_failure_discard_only_otp(self):
        self.path.write_text('{}')
        bad = SmsFloodGuard(self.path, clock=lambda: self.now)
        self.assertEqual(bad.admit('quectel0', '验证码123456', 'bad')[0], 'drop')
        self.assertEqual(bad.admit('quectel0', '快递到了', 'ordinary')[0], 'allow')
        with patch.object(self.guard, 'save', side_effect=OSError('private error')):
            self.assertEqual(self.otp('failed')[0], 'drop')
            with self.assertRaises(OSError):
                self.guard.set_mode('off')
        self.assertEqual(self.guard.state['default_mode'], 'auto')

    def test_clock_rollback_and_structurally_corrupt_state_fail_closed(self):
        self.otp('first')
        self.now -= 5
        self.assertEqual(self.otp('clock-back')[0], 'drop')
        self.guard.save(force=True)
        restored = SmsFloodGuard(self.path, clock=lambda: self.now)
        self.assertEqual(restored.admit('quectel0', '验证码123456', 'restart')[0], 'drop')
        for invalid in ('[]', '{"version":1,"devices":null}', 'null'):
            self.path.write_text(invalid)
            broken = SmsFloodGuard(self.path, clock=lambda: self.now)
            self.assertEqual(broken.admit('quectel0', '验证码123456', invalid)[0], 'drop')

    def test_auto_protection_survives_restart(self):
        for number in range(15):
            self.otp(number)
        restored = SmsFloodGuard(self.path, clock=lambda: self.now)
        self.assertEqual(restored.admit('quectel0', '验证码123456', 'restart')[0], 'drop')


class IngressTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.bot = AsteriskBot.__new__(AsteriskBot)
        self.bot.sms_guard = SmsFloodGuard(Path(self.temp.name)/'state.json')
        self.bot.sms_outbox = asyncio.Queue(maxsize=64)
        self.bot.sms_queue_dropped = 0
        self.bot.pending_replies = {}
        self.bot.log_cache = []
        self.bot.add_to_log_cache = lambda record: self.bot.log_cache.append(record)
        self.bot.verify_silent_sms = AsyncMock(return_value=(False, '', {}))
        self.bot.push_sms_to_users = AsyncMock()
        self.bot.send_message = AsyncMock()

    def frame(self, number, body='【平台】验证码 739204'):
        return json.dumps({'device_id': 'quectel0', 'sender_number': 'platform'+str(number),
                           'content': body, 'timestamp': '2030-01-01 00:00:00', 'event_id': str(number)})

    async def test_blocked_canary_never_reaches_log_cache_queue_reply_or_send(self):
        self.bot.sms_guard.set_mode('manual')
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            await self.bot.process_sms_data(self.frame(1, '验证码 739204 CANARY_PRIVATE_BODY'))
            await self.bot.process_sms_data('{CANARY_PRIVATE_BODY bad json')
        self.assertNotIn('CANARY_PRIVATE_BODY', output.getvalue())
        self.assertNotIn('739204', self.bot.sms_guard.path.read_text())
        self.assertEqual(self.bot.sms_outbox.qsize(), 0)
        self.assertEqual(self.bot.log_cache, [])
        self.assertEqual(self.bot.pending_replies, {})
        self.bot.verify_silent_sms.assert_not_awaited()
        self.bot.push_sms_to_users.assert_not_awaited()

    async def test_ingress_remains_fast_with_blocked_telegram_and_multi_platform_bomb(self):
        for number in range(100):
            await asyncio.wait_for(self.bot.process_sms_data(self.frame(number)), timeout=.5)
        self.assertEqual(self.bot.sms_outbox.qsize(), 15)  # 14 admitted bodies, one start notice
        self.bot.push_sms_to_users.assert_not_awaited()
        self.assertEqual(self.bot.sms_guard.record('quectel0')['dropped'], 86)
        await self.bot.process_sms_data(self.frame(101, '快递到了，订单123456'))
        item = None
        while not self.bot.sms_outbox.empty():
            item = self.bot.sms_outbox.get_nowait()
        await self.bot.deliver_sms_item(item)
        self.bot.push_sms_to_users.assert_awaited_once()
        self.assertEqual(self.bot.log_cache[0]['type'], 'sms_received')
        self.assertNotIn('content', self.bot.log_cache[0])

    async def test_commands_authorization_menu_and_mode_persistence(self):
        with patch.object(Config, 'AUTHORIZED_USERS', {1}):
            await self.bot.handle_sms_guard_command(9, '/sms_guard off', 9)
            self.assertEqual(self.bot.sms_guard.state['default_mode'], 'auto')
            self.bot.send_message.assert_not_awaited()
            await self.bot.handle_sms_guard_command(1, '/sms_guard manual quectel0', 1)
            self.assertEqual(self.bot.sms_guard.record('quectel0')['mode'], 'manual')
            await self.bot.handle_sms_guard_command(1, '/sms_guard off', 1)
            self.assertEqual(self.bot.sms_guard.state['default_mode'], 'off')
        self.assertIn('sms_guard', [command for command, label in AsteriskBot.COMMANDS])

    async def test_queue_bound_and_log_ingress_cannot_bypass_guard(self):
        for number in range(80):
            await self.bot.process_sms_data(self.frame(number, '快递到了'))
        self.assertEqual(self.bot.sms_outbox.qsize(), 64)
        self.assertEqual(self.bot.sms_queue_dropped, 16)
        await self.bot.handle_sms_received('quectel0', '1', 'sender', '验证码123456', 'time')
        self.bot.push_sms_to_users.assert_not_awaited()

    async def test_worker_wait_does_not_block_ingress_and_cancel_is_clean(self):
        self.bot.running = True
        entered = asyncio.Event()
        release = asyncio.Event()
        async def blocked(item):
            entered.set()
            await release.wait()
        self.bot.deliver_sms_item = blocked
        await self.bot.process_sms_data(self.frame(0))
        worker = asyncio.create_task(self.bot.sms_delivery_worker())
        await asyncio.wait_for(entered.wait(), timeout=.5)
        for number in range(1, 100):
            await asyncio.wait_for(self.bot.process_sms_data(self.frame(number)), timeout=.5)
        self.assertEqual(self.bot.sms_guard.record('quectel0')['dropped'], 86)
        worker.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await worker
