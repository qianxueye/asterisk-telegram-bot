import asyncio
import json
import os
import tempfile
import unittest
from collections import defaultdict
from datetime import datetime
import sys
import types


# The production dependencies are deliberately not required for offline unit tests.
if 'telethon' not in sys.modules:
    telethon = types.ModuleType('telethon')
    telethon.TelegramClient = object
    telethon.events = types.SimpleNamespace(NewMessage=object, CallbackQuery=object)
    sys.modules['telethon'] = telethon
    sys.modules['telethon.tl'] = types.ModuleType('telethon.tl')
    sys.modules['telethon.tl.functions'] = types.ModuleType('telethon.tl.functions')
    messages = types.ModuleType('telethon.tl.functions.messages')
    messages.SendMessageRequest = object
    sys.modules['telethon.tl.functions.messages'] = messages
    tl_types = types.ModuleType('telethon.tl.types')
    tl_types.UpdateNewMessage = object
    tl_types.Message = object
    sys.modules['telethon.tl.types'] = tl_types
    custom = types.ModuleType('telethon.tl.custom')
    custom.Button = types.SimpleNamespace(inline=lambda *args, **kwargs: (args, kwargs))
    sys.modules['telethon.tl.custom'] = custom

if 'dotenv' not in sys.modules:
    dotenv = types.ModuleType('dotenv')
    dotenv.load_dotenv = lambda: None
    sys.modules['dotenv'] = dotenv

from asterisk_bot import AsteriskBot
from config import Config


class UACRecoveryTests(unittest.IsolatedAsyncioTestCase):
    def make_bot(self):
        """A bare bot avoids Telegram clients, FIFOs, and real CLI processes."""
        bot = AsteriskBot.__new__(AsteriskBot)
        bot.uac_device_locks = defaultdict(asyncio.Lock)
        bot.uac_error_events = defaultdict(list)
        bot.uac_recovery_state = {'devices': {}}
        bot.background_tasks = []
        return bot

    def setUp(self):
        self.old_values = {name: getattr(Config, name) for name in (
            'UAC_AUTO_RECOVERY_ENABLED', 'UAC_AUTO_RECOVERY_DEVICES', 'UAC_ERROR_THRESHOLD',
            'UAC_ERROR_WINDOW_SECONDS', 'UAC_RECOVERY_COOLDOWN_SECONDS', 'UAC_RECOVERY_MAX_PER_DAY',
            'UAC_RECOVERY_STATE_FILE', 'UAC_RECOVERY_DRY_RUN'
        )}
        self.old_authorized_users = Config.AUTHORIZED_USERS
        self.state_file = tempfile.NamedTemporaryFile(delete=False).name
        os.unlink(self.state_file)
        Config.UAC_AUTO_RECOVERY_ENABLED = True
        Config.UAC_AUTO_RECOVERY_DEVICES = {'quectel1'}
        Config.UAC_ERROR_THRESHOLD = 25
        Config.UAC_ERROR_WINDOW_SECONDS = 5
        Config.UAC_RECOVERY_COOLDOWN_SECONDS = 3600
        Config.UAC_RECOVERY_MAX_PER_DAY = 2
        Config.UAC_RECOVERY_STATE_FILE = self.state_file
        Config.UAC_RECOVERY_DRY_RUN = True

    def tearDown(self):
        for name, value in self.old_values.items():
            setattr(Config, name, value)
        Config.AUTHORIZED_USERS = self.old_authorized_users
        if os.path.exists(self.state_file):
            os.unlink(self.state_file)

    def test_log_classification_and_health_require_all_conditions(self):
        self.assertIsNone(AsteriskBot.classify_uac_log_line('[quectel1] normal call ended'))
        event = AsteriskBot.classify_uac_log_line('[quectel1] ALSA PLAYBACK Error - try again later')
        self.assertEqual(('quectel1', 'burst', 'playback'), (event['device'], event['severity'], event['direction']))
        self.assertEqual('severe', AsteriskBot.classify_uac_log_line('[quectel1] Prepare failed')['severity'])
        healthy = 'Current device state: Start\nDesired device state: Start\nVoice: Yes\nGSM registration: Registered\nCalls/Channels: 0'
        self.assertTrue(AsteriskBot.parse_uac_health(healthy))
        self.assertFalse(AsteriskBot.parse_uac_health(healthy.replace('Calls/Channels: 0', 'Calls/Channels: 1')))

    async def test_only_25_errors_trigger_one_automatic_soft_recovery(self):
        bot = self.make_bot()
        scheduled = []

        def schedule(coro):
            scheduled.append(coro)
            coro.close()

        bot.create_background_task = schedule
        line = '[quectel1] ALSA PLAYBACK Error - try again later'
        for i in range(24):
            self.assertFalse(await bot.handle_uac_log_line(line, now=float(i) / 10))
        self.assertTrue(await bot.handle_uac_log_line(line, now=2.5))
        self.assertEqual(1, len(scheduled))
        self.assertEqual(1, bot.uac_state_for('quectel1')['automatic_count'])
        self.assertFalse(await bot.handle_uac_log_line(line, now=2.6))

    async def test_daily_limit_and_atomic_state_persistence(self):
        bot = self.make_bot()
        record = bot.uac_state_for('quectel1')
        record.update({'day': datetime.now().date().isoformat(), 'automatic_count': 2, 'cooldown_until': 0})
        self.assertEqual((False, '已达到每日自动恢复上限'), bot.uac_auto_recovery_allowed('quectel1', 1000.0))
        record['automatic_count'] = 1
        record['last_reason'] = 'test'
        bot.save_uac_recovery_state()
        self.assertEqual(0o600, os.stat(self.state_file).st_mode & 0o777)
        with open(self.state_file, encoding='utf-8') as saved:
            self.assertEqual('test', json.load(saved)['devices']['quectel1']['last_reason'])

    async def test_dry_run_never_executes_cli_and_invalid_device_is_rejected(self):
        bot = self.make_bot()
        calls = []

        async def cli(command):
            calls.append(command)
            return ''

        bot.execute_asterisk_cli = cli
        rejected = await bot.recover_uac('quectel1; shutdown', source='manual')
        self.assertIn('拒绝', rejected['detail'])
        result = await bot.recover_uac('quectel1', source='manual')
        self.assertTrue(result['dry_run'])
        self.assertEqual([], calls)

    async def test_hard_reset_requires_second_confirmation_and_authorization(self):
        bot = self.make_bot()
        bot.user_states = {7: {'action': 'uac_action', 'flow_id': 'token', 'devices': ['quectel1']}}

        async def live_devices():
            return ['quectel1']

        recoveries = []

        async def recover(device, source, hard_reset=False):
            recoveries.append((device, source, hard_reset))
            return {'healthy': True, 'command': 'test', 'detail': 'ok'}

        class Event:
            def __init__(self):
                self.answers = []
            async def answer(self, message):
                self.answers.append(message)
            async def edit(self, *args, **kwargs):
                pass

        bot.get_live_uac_devices = live_devices
        bot.recover_uac = recover
        bot.send_message = lambda *args, **kwargs: asyncio.sleep(0)
        event = Event()
        await bot.handle_uac_callback(event, 'hard', 'token', 'quectel1', 7, 7)
        self.assertEqual('uac_hard_confirm', bot.user_states[7]['action'])
        self.assertEqual([], recoveries)
        await bot.handle_uac_callback(event, 'apply', 'token', 'quectel1', 7, 7)
        self.assertEqual([('quectel1', 'manual', True)], recoveries)

        Config.AUTHORIZED_USERS = set()
        await bot.handle_callback_action(event, 'uac:apply:token:quectel1', 99, 99)
        self.assertIn('没有权限', event.answers[-1])


if __name__ == '__main__':
    unittest.main()
