import asyncio
import importlib.util
import io
import json
import os
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from inbound_call import InboundCallPolicy, PolicyWriteUncertain, normalize_caller, parse_live_line_state, sim_identities, subscriber_number
from inbound_call_agi import handle_call


SIM = 'iccid:12345678901234567890'
OTHER = 'iccid:22345678901234567890'
IMSI = 'imsi:123456789012345'
PHONE = '+8613800138000'


def device_state(device, phone=PHONE, sim=SIM, imsi=IMSI, state='Free'):
    return (f'-------------- Status -------------\n  Device : {device}\n'
            f'  State : {state}\n'
            f'  ICCID : {sim.split(":", 1)[1] if sim else ""}\n'
            f'  IMSI : {imsi.split(":", 1)[1] if imsi else ""}\n'
            f'  Subscriber Number : {phone}\n')


def device_rows(*devices):
    return 'ID Group State RSSI Mode Provider Name Model Firmware Number\n' + ''.join(
        f'{device} 0 Start 20 0 Provider Model Firmware\n' for device in devices)


class PolicyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        checker = patch.object(InboundCallPolicy, '_check_directory')
        checker.start()
        self.addCleanup(checker.stop)
        self.now = 1000
        self.policy = InboundCallPolicy(Path(self.temp.name) / 'state.json', clock=lambda: self.now)

    def test_identity_and_caller_filter(self):
        self.assertEqual(sim_identities({'iccid': SIM.split(':')[1], 'imsi': '123456789012345'}),
                         (SIM, 'imsi:123456789012345'))
        self.assertEqual(sim_identities({'iccid': 'invalid', 'imsi': '123456789012345'}), ())
        self.assertEqual(sim_identities({'name': 'quectel1'}), ())
        for value in ('0211234567', '02112345678', '+86 02112345678', '0086-0211234567'):
            self.assertEqual(normalize_caller(value), value.replace('+86', '').replace('0086', '').replace(' ', '').replace('-', ''))
        for value in ('021123456', '021123456789', '021123456x', '+1 0211234567', '8610211234567', '0211234567#'):
            self.assertIsNone(normalize_caller(value))
        for value, expected in (('+86211234567', '0211234567'), ('0086211234567', '0211234567'),
                                ('+862112345678', '02112345678'), ('00862112345678', '02112345678')):
            self.assertEqual(normalize_caller(value), expected)
        for value in ('+8621123456', '008621123456789', '+862212345678', '+8613812345678'):
            self.assertIsNone(normalize_caller(value))

    def test_live_line_state_requires_matching_device_and_valid_identity(self):
        self.assertEqual(subscriber_number('+86 (138) 0013-8000'), PHONE)
        self.assertIsNone(subscriber_number('13800138000'))
        line = parse_live_line_state(device_state('quectel0'), 'quectel0')
        self.assertEqual((line['number'], line['identities']), (PHONE, (SIM, IMSI)))
        self.assertEqual(parse_live_line_state(device_state('quectel0', phone='Unknown'), 'quectel0')['number'], '')
        self.assertEqual(parse_live_line_state(device_state('quectel0', sim='', imsi=''), 'quectel0')['identities'], ())
        self.assertTrue(parse_live_line_state(device_state('quectel1', phone='Unknown', sim='', imsi='',
                                                           state='Not initialized'), 'quectel1')['inactive'])
        self.assertFalse(parse_live_line_state(device_state('quectel1', phone='Unknown', sim='', imsi='',
                                                            state='Free'), 'quectel1')['inactive'])
        self.assertFalse(parse_live_line_state(device_state('quectel0', state='Stopped'), 'quectel0')['selectable'])
        with self.assertRaises(ValueError):
            parse_live_line_state(device_state('quectel1'), 'quectel0')
        with self.assertRaises(ValueError):
            parse_live_line_state(device_state('quectel0') + '  ICCID : 123\n', 'quectel0')

    def test_default_off_exact_sim_test_expiry_and_snapshot(self):
        self.assertIsNone(self.policy.snapshot(SIM, '0211234567'))
        self.policy.update(SIM, 'mode', 'direct')
        self.assertIsNone(self.policy.snapshot(OTHER, '0211234567'))
        self.assertIsNone(self.policy.snapshot(SIM, 'not-a-number'))
        snapshot = self.policy.snapshot(SIM, '0211234567')
        self.policy.update(SIM, 'mode', 'forward')
        self.assertEqual(snapshot['mode'], 'direct')
        self.assertIsNone(self.policy.snapshot(SIM, '0211234567'))
        self.policy.update(SIM, 'mode', 'direct')
        self.policy.update(SIM, 'test', 30)
        self.assertIsNotNone(self.policy.snapshot(SIM, 'other'))
        self.assertIsNone(self.policy.snapshot(OTHER, 'other'))
        self.now = 1030
        self.assertIsNone(self.policy.snapshot(SIM, 'other'))

    def test_both_channel_identifiers_select_one_policy_or_fail_on_conflict(self):
        self.policy.update(IMSI, 'mode', 'direct')
        identities = sim_identities({'iccid': SIM.split(':')[1], 'imsi': IMSI.split(':')[1]})
        self.assertEqual(self.policy.snapshot(identities, '0211234567')['mode'], 'direct')
        self.policy.update(SIM, 'mode', 'forward')
        self.assertIsNone(self.policy.snapshot(identities, '0211234567'))
        self.policy.update(SIM, 'mode', 'direct')
        self.assertIsNone(self.policy.snapshot(identities, '0211234567'))
        self.policy.update(OTHER, 'mode', 'direct')
        self.assertIsNone(self.policy.snapshot((OTHER, IMSI), '0211234567'))

    def test_number_binding_reuses_existing_identity_and_rejects_conflict(self):
        self.policy.update(IMSI, 'mode', 'forward')
        self.policy.update_for_identities((SIM, IMSI), 'mode', 'direct', PHONE)
        state = self.policy.load()['sims']
        self.assertEqual(list(state), [IMSI])
        self.assertEqual((state[IMSI]['number'], state[IMSI]['mode']), (PHONE, 'direct'))
        self.policy.update(SIM, 'mode', 'forward')
        with self.assertRaisesRegex(ValueError, 'identity conflict'):
            self.policy.update_for_identities((SIM, IMSI), 'mode', 'direct', PHONE)
        self.assertEqual(self.policy.load()['sims'][IMSI]['mode'], 'direct')

    def test_number_reassignment_does_not_create_a_second_sim_policy(self):
        self.policy.update_for_identities((SIM, IMSI), 'mode', 'direct', PHONE)
        with self.assertRaisesRegex(ValueError, 'number reassigned'):
            self.policy.update_for_identities((OTHER, 'imsi:223456789012345'), 'mode', 'direct', PHONE)
        self.assertEqual(list(self.policy.load()['sims']), [SIM])

    def test_removed_settings_rejected_and_fail_safe_config(self):
        for field, value in (('secret', '12#*'), ('volume', 10), ('mode', 'protected')):
            with self.assertRaises(ValueError):
                self.policy.update(SIM, field, value)
        self.policy.update(SIM, 'mode', 'direct')
        self.assertEqual(set(self.policy.load()['sims'][SIM]), {'mode', 'test_until', 'number'})
        self.assertEqual(self.policy.path.stat().st_mode & 0o777, 0o640)
        self.policy.path.write_text('{bad')
        self.assertIsNone(self.policy.snapshot(SIM, '0211234567'))
        with self.assertRaises(ValueError):
            self.policy.update(SIM, 'mode', 'direct')

    def test_legacy_policy_migration_strips_secret_and_disables_protected(self):
        legacy = {'version': 1, 'sims': {
            SIM: {'mode': 'protected', 'salt': 'a' * 32, 'digest': 'b' * 64, 'length': 4,
                  'volume': 10, 'test_until': 1030, 'number': PHONE},
            OTHER: {'mode': 'direct', 'salt': '', 'digest': '', 'length': 0,
                    'volume': 10, 'test_until': 0, 'number': ''}}}
        self.policy.path.write_text(json.dumps(legacy))
        os.chmod(self.policy.path, 0o640)
        self.assertIsNone(self.policy.snapshot(SIM, '0211234567'))
        self.assertEqual(self.policy.snapshot(OTHER, '0211234567')['mode'], 'direct')
        self.policy.migrate_legacy()
        stored = json.loads(self.policy.path.read_text())
        self.assertEqual(stored['version'], 2)
        self.assertEqual(stored['sims'][SIM], {'mode': 'forward', 'test_until': 1030, 'number': PHONE})
        self.assertEqual(stored['sims'][OTHER]['mode'], 'direct')
        self.assertNotIn('digest', self.policy.path.read_text())
        self.assertNotIn('salt', self.policy.path.read_text())

    def test_reader_observes_atomic_snapshots_during_updates(self):
        self.policy.update(SIM, 'mode', 'direct')
        errors = []
        def writer():
            for value in range(1, 31):
                self.policy.update(SIM, 'test', value)
        worker = threading.Thread(target=writer)
        worker.start()
        while worker.is_alive():
            if self.policy.snapshot(SIM, '0211234567') is None:
                errors.append('invalid snapshot')
        worker.join()
        self.assertEqual(errors, [])
        self.assertEqual(self.policy.load()['sims'][SIM]['test_until'], 1030)

    def test_concurrent_writers_keep_both_changes(self):
        self.policy.update(SIM, 'mode', 'forward')
        original_load = self.policy.load
        start = threading.Barrier(3)
        errors = []
        def slow_load():
            state = original_load()
            time.sleep(.03)
            return state
        def change(field, value):
            start.wait()
            try:
                self.policy.update(SIM, field, value)
            except Exception as exc:
                errors.append(type(exc).__name__)
        with patch.object(self.policy, 'load', side_effect=slow_load):
            first = threading.Thread(target=change, args=('mode', 'direct'))
            second = threading.Thread(target=change, args=('test', 22))
            first.start()
            second.start()
            start.wait()
            first.join()
            second.join()
        self.assertEqual(errors, [])
        item = self.policy.load()['sims'][SIM]
        self.assertEqual((item['mode'], item['test_until']), ('direct', 1022))
        self.assertEqual(os.stat(str(self.policy.path) + '.lock').st_mode & 0o777, 0o600)

    def test_directory_sync_failure_reports_uncertain_result(self):
        original_fsync = os.fsync
        calls = 0
        def fail_directory(fd):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError('sync failed')
            return original_fsync(fd)
        with patch('inbound_call.os.fsync', side_effect=fail_directory):
            with self.assertRaises(PolicyWriteUncertain):
                self.policy.update(SIM, 'mode', 'direct')
        self.assertEqual(self.policy.load()['sims'][SIM]['mode'], 'direct')

    def test_policy_rejects_fifo_and_symlink(self):
        os.mkfifo(self.policy.path)
        self.assertIsNone(self.policy.load())
        self.policy.path.unlink()
        target = Path(self.temp.name) / 'other.json'
        target.write_text('{"version":2,"sims":{}}')
        self.policy.path.symlink_to(target)
        self.assertIsNone(self.policy.load())


class AgiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        checker = patch.object(InboundCallPolicy, '_check_directory')
        checker.start()
        self.addCleanup(checker.stop)
        self.policy = InboundCallPolicy(Path(self.temp.name) / 'state.json', clock=lambda: 1000)
        self.policy.update(SIM, 'mode', 'direct')

    def run_call(self, metadata, caller, results='', policy=None, state='Ring', pipe_response='200 result=1 (/private/tmp/sms)\n'):
        source = io.StringIO('agi_request: test\nagi_uniqueid: 123.456\n\n200 result=1 (' + json.dumps(metadata) + ')\n'
                             '200 result=1 (' + state + ')\n'
                             '200 result=1 (' + caller + ')\n' + pipe_response + results)
        sink = io.StringIO()
        with patch('inbound_call_agi.forward_event') as event:
            outcome = handle_call(source, sink, policy=policy or self.policy)
        return outcome, sink.getvalue(), event

    def test_forward_routes_do_not_answer(self):
        for metadata, caller in (({'name': 'quectel0'}, '0211234567'),
                                 ({'iccid': OTHER.split(':')[1]}, '0211234567'),
                                 ({'iccid': SIM.split(':')[1]}, 'unknown')):
            outcome, commands, _ = self.run_call(metadata, caller)
            self.assertEqual(outcome, 'forward')
            self.assertNotIn('ANSWER', commands)
        self.policy.path.write_text('{}')
        outcome, commands, _ = self.run_call({'iccid': SIM.split(':')[1]}, '0211234567')
        self.assertEqual(outcome, 'forward')
        self.assertNotIn('ANSWER', commands)
        outcome, commands, _ = self.run_call({'iccid': SIM.split(':')[1]}, '0211234567', state='Up')
        self.assertEqual(outcome, 'forward')
        self.assertNotIn('ANSWER', commands)

    def test_imsi_policy_with_iccid_present_and_conflict_falls_back(self):
        other_policy = InboundCallPolicy(Path(self.temp.name) / 'imsi.json', clock=lambda: 1000)
        other_policy.update(IMSI, 'mode', 'direct')
        metadata = {'iccid': SIM.split(':')[1], 'imsi': IMSI.split(':')[1]}
        result, commands, _ = self.run_call(metadata, '0211234567', '200 result=0\n' * 4, policy=other_policy)
        self.assertEqual(result, 'sent')
        self.assertIn('ANSWER', commands)
        self.assertEqual(commands.count('EXEC SendDTMF ###,1500,100'), 1)
        other_policy.update(SIM, 'mode', 'direct')
        result, commands, _ = self.run_call(metadata, '0211234567', policy=other_policy)
        self.assertEqual(result, 'forward')
        self.assertNotIn('ANSWER', commands)

    def test_legacy_protected_mode_follows_3cx_without_answer(self):
        legacy = {'version': 1, 'sims': {SIM: {'mode': 'protected', 'salt': 'a' * 32,
                  'digest': 'b' * 64, 'length': 4, 'volume': 10, 'test_until': 0, 'number': PHONE}}}
        self.policy.path.write_text(json.dumps(legacy))
        os.chmod(self.policy.path, 0o640)
        result, commands, _ = self.run_call({'iccid': SIM.split(':')[1]}, '0211234567')
        self.assertEqual(result, 'forward')
        self.assertNotIn('ANSWER', commands)
        self.assertNotIn('SendDTMF', commands)

    def test_direct_sends_three_fixed_digits_with_spacing(self):
        results = '200 result=0\n' * 4
        outcome, commands, event = self.run_call({'iccid': SIM.split(':')[1]}, '0211234567', results)
        self.assertEqual(outcome, 'sent')
        send_commands = [line for line in commands.splitlines() if line.startswith('EXEC SendDTMF ')]
        self.assertEqual(send_commands, ['EXEC SendDTMF ###,1500,100'])
        digits, gap, duration = send_commands[0].removeprefix('EXEC SendDTMF ').split(',')
        self.assertEqual(len(digits), 3)
        gap, duration = int(gap), int(duration)
        ends = [duration]
        starts = [0]
        for _ in digits[1:]:
            starts.append(ends[-1] + gap)
            ends.append(starts[-1] + duration)
        self.assertEqual([start - end for start, end in zip(starts[1:], ends[:-1])], [1500, 1500])
        self.assertNotIn('HANGUP\n', commands)
        event.assert_called_once()
        self.assertEqual(event.call_args.args[1], {'kind': 'inbound_call', 'event_id': '123.456',
                          'sim_id': SIM, 'number': '', 'caller': '0211234567', 'status': 'accepted'})
        self.assertEqual(event.call_args.kwargs['timeout'], .2)

    def test_notification_failure_and_missing_pipe_do_not_change_call(self):
        metadata = {'iccid': SIM.split(':')[1]}
        for failure in (TimeoutError('no reader'), RuntimeError('transport failed')):
            with patch('inbound_call_agi.forward_event', side_effect=failure) as event:
                source = io.StringIO('agi_request: test\nagi_uniqueid: 123.456\n\n'
                                     '200 result=1 (' + json.dumps(metadata) + ')\n'
                                     '200 result=1 (Ring)\n200 result=1 (0211234567)\n'
                                     '200 result=1 (/private/tmp/sms)\n' + '200 result=0\n' * 4)
                sink = io.StringIO()
                self.assertEqual(handle_call(source, sink, policy=self.policy), 'sent')
                event.assert_called_once()
                self.assertEqual(sink.getvalue().count('EXEC SendDTMF ###,1500,100'), 1)
        result, commands, event = self.run_call(metadata, '0211234567', '200 result=0\n' * 4,
                                                 pipe_response='200 result=0\n')
        self.assertEqual(result, 'sent')
        self.assertEqual(commands.count('EXEC SendDTMF ###,1500,100'), 1)
        event.assert_not_called()

    def test_send_failure_never_emits_success_notification(self):
        result, commands, event = self.run_call({'iccid': SIM.split(':')[1]}, '0211234567',
                                                 '200 result=0\n' * 3 + '200 result=-1\n')
        self.assertEqual(result, 'ended')
        self.assertEqual(commands.count('EXEC SendDTMF ###,1500,100'), 1)
        event.assert_not_called()

    def test_success_event_uses_existing_fifo_once(self):
        pipe = str(Path(self.temp.name) / 'sms_pipe')
        os.mkfifo(pipe, 0o600)
        reader = os.open(pipe, os.O_RDONLY | os.O_NONBLOCK)
        try:
            source = io.StringIO('agi_request: test\nagi_uniqueid: 123.456\n\n'
                                 '200 result=1 (' + json.dumps({'iccid': SIM.split(':')[1]}) + ')\n'
                                 '200 result=1 (Ring)\n200 result=1 (0211234567)\n'
                                 '200 result=1 (' + pipe + ')\n' + '200 result=0\n' * 4)
            sink = io.StringIO()
            self.assertEqual(handle_call(source, sink, policy=self.policy), 'sent')
            raw = os.read(reader, 4096)
            self.assertLess(len(raw), 4096)
            records = [json.loads(line) for line in raw.splitlines() if line]
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0]['event_id'], '123.456')
            self.assertEqual(records[0]['status'], 'accepted')
        finally:
            os.close(reader)

    def test_direct_waits_for_senddtmf_completion_before_returning(self):
        sink = io.StringIO()
        waiting = threading.Event()
        resume = threading.Event()
        class WaitingSource(io.StringIO):
            def readline(self, *args):
                if sink.getvalue().endswith('EXEC SendDTMF ###,1500,100\n'):
                    waiting.set()
                    if not resume.wait(2):
                        raise TimeoutError('SendDTMF response withheld')
                return super().readline(*args)
        source = WaitingSource('agi_request: test\nagi_uniqueid: 123.456\n\n200 result=1 (' +
                               json.dumps({'iccid': SIM.split(':')[1]}) + ')\n'
                               '200 result=1 (Ring)\n200 result=1 (0211234567)\n'
                               '200 result=1 (/private/tmp/sms)\n' +
                               '200 result=0\n' * 4)
        result = []
        worker = threading.Thread(target=lambda: result.append(handle_call(source, sink, policy=self.policy)))
        worker.start()
        try:
            self.assertTrue(waiting.wait(2))
            self.assertTrue(worker.is_alive())
            self.assertEqual(result, [])
        finally:
            resume.set()
            worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertEqual(result, ['sent'])
        self.assertEqual(sink.getvalue().count('EXEC SendDTMF ###,1500,100'), 1)

    def test_selection_answer_and_send_disconnect_failures(self):
        metadata = {'iccid': SIM.split(':')[1]}
        result, commands, _ = self.run_call(metadata, '0211234567', '200 result=-1\n')
        self.assertEqual(result, 'forward')
        self.assertIn('SET VARIABLE INBOUND_CALL_SELECTED 1', commands)
        self.assertNotIn('ANSWER', commands)
        result, commands, _ = self.run_call(metadata, '0211234567', '200 result=0\n200 result=-1\n')
        self.assertEqual(result, 'forward')
        self.assertIn('ANSWER', commands)
        self.assertNotIn('SendDTMF', commands)
        result, commands, _ = self.run_call(metadata, '0211234567', '200 result=0\n' * 2 + '200 result=-1\n')
        self.assertEqual(result, 'ended')
        self.assertNotIn('SendDTMF', commands)
        result, commands, _ = self.run_call(metadata, '0211234567', '200 result=0\n' * 3 + '200 result=-1\n')
        self.assertEqual(result, 'ended')
        self.assertEqual(commands.count('EXEC SendDTMF ###,1500,100'), 1)

    def test_dialplan_preserves_existing_routes_and_answer_boundary(self):
        source = (Path(__file__).resolve().parents[1] / 'asterisk-dialplan-integrated.conf').read_text()
        from_3cx = source.split('[from-3cx]', 1)[1].split('[pbx-rtp-jb]', 1)[0]
        jitter = source.split('[pbx-rtp-jb]', 1)[1].split('[incoming-mobile]', 1)[0]
        mobile = source.split('[incoming-mobile]', 1)[1].split('[general-sms]', 1)[0]
        self.assertLess(mobile.index('AGI(inbound_call_agi.py)'), mobile.index('Dial(PJSIP/${EXTEN}@10002,'))
        self.assertLess(from_3cx.index('Gosub(pbx-rtp-jb,s,1)'), from_3cx.index('Dial(Quectel/i:<QUECTEL_IMEI>/${EXTEN})'))
        self.assertIn('Set(JITTERBUFFER(adaptive)=default)', jitter)
        self.assertIn('Return()', jitter)
        self.assertIn('Dial(PJSIP/${EXTEN}@10002,,b(pbx-rtp-jb^s^1))', mobile)
        self.assertNotIn('EAGI_AUDIO_FORMAT', mobile)
        self.assertIn('INBOUND_CALL_SELECTED', mobile)
        self.assertIn('CHANNEL(state)', mobile)
        self.assertIn('AGI(sms_ingress_agi.py)', mobile)
        self.assertIn('exten => ussd', mobile)


class BotTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def bot_classes():
        import sys
        modules = {}
        for name in ('telethon', 'telethon.tl', 'telethon.tl.functions',
                     'telethon.tl.functions.messages', 'telethon.tl.types',
                     'telethon.tl.custom', 'dotenv'):
            modules[name] = types.ModuleType(name)
            modules[name].__path__ = []
        modules['telethon'].TelegramClient = object
        modules['telethon'].events = object()
        modules['telethon.tl.functions.messages'].SendMessageRequest = object
        modules['telethon.tl.types'].UpdateNewMessage = object
        modules['telethon.tl.types'].Message = object
        modules['telethon.tl.custom'].Button = object
        modules['dotenv'].load_dotenv = lambda: None
        with patch.dict(sys.modules, modules):
            spec = importlib.util.spec_from_file_location('asterisk_bot_for_inbound_test',
                                                        Path(__file__).resolve().parents[1] / 'asterisk_bot.py')
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            return module.AsteriskBot, module.Config

    async def test_authorization_and_removed_modes(self):
        AsteriskBot, Config = self.bot_classes()
        with tempfile.TemporaryDirectory() as directory:
            bot = AsteriskBot.__new__(AsteriskBot)
            bot.inbound_call = InboundCallPolicy(Path(directory) / 'state.json', clock=lambda: 1000)
            bot.send_message = AsyncMock()
            with patch.object(Config, 'AUTHORIZED_USERS', {1}), patch.object(InboundCallPolicy, '_check_directory'):
                await bot.handle_inbound_call_command(2, '/inbound_call secret ' + SIM + ' 12#', 2)
                self.assertFalse(bot.inbound_call.path.exists())
                await bot.handle_inbound_call_command(1, '/inbound_call mode ' + SIM + ' protected', 1)
                await bot.handle_inbound_call_command(1, '/inbound_call secret ' + SIM + ' 12#', 1)
                await bot.handle_inbound_call_command(1, '/inbound_call volume ' + SIM + ' 10', 1)
                self.assertFalse(bot.inbound_call.path.exists())
                await bot.handle_inbound_call_command(1, '/inbound_call mode ' + SIM + ' direct', 1)
                await bot.handle_inbound_call_command(1, '/inbound_call status', 1)
                self.assertIn('测试：关闭', bot.send_message.call_args.args[1])
                await bot.handle_inbound_call_command(1, '/inbound_call test ' + SIM + ' 30', 1)
                await bot.handle_inbound_call_command(1, '/inbound_call status', 1)
                self.assertIn('测试：开启', bot.send_message.call_args.args[1])
            self.assertEqual(bot.inbound_call.load()['sims'][SIM]['mode'], 'direct')
            for call in bot.send_message.call_args_list:
                self.assertNotIn('12#', str(call))
            self.assertIn('inbound_call', [name for name, _ in AsteriskBot.COMMANDS])

    async def test_call_event_delivery_is_authorized_validated_and_deduplicated(self):
        AsteriskBot, Config = self.bot_classes()
        bot = AsteriskBot.__new__(AsteriskBot)
        bot.inbound_call_seen = {}
        bot.sms_outbox = asyncio.Queue(maxsize=64)
        bot.sms_queue_dropped = 0
        bot.inbound_call_queue_dropped = 0
        bot.send_message = AsyncMock(return_value=object())
        event = {'kind': 'inbound_call', 'event_id': '123.456', 'sim_id': SIM,
                 'number': PHONE, 'caller': '0211234567', 'status': 'accepted'}
        with patch.object(Config, 'AUTHORIZED_USERS', {1, 2}):
            await bot.process_sms_data(json.dumps(event))
            await bot.process_sms_data(json.dumps(event))
            self.assertEqual(bot.sms_outbox.qsize(), 1)
            await bot.deliver_sms_item(bot.sms_outbox.get_nowait())
        self.assertEqual([call.args[0] for call in bot.send_message.await_args_list], [1, 2])
        for call in bot.send_message.await_args_list:
            self.assertIn(PHONE, call.args[1])
            self.assertIn('0211234567', call.args[1])
            self.assertIn('Asterisk 已接受发送 ### 指令', call.args[1])
            self.assertNotIn('已收到', call.args[1])
            self.assertIsNone(call.kwargs['parse_mode'])

    async def test_call_event_recipients_survive_authorization_changes_during_send(self):
        AsteriskBot, Config = self.bot_classes()
        bot = AsteriskBot.__new__(AsteriskBot)
        event = {'kind': 'inbound_call', 'event_id': '123.456', 'sim_id': SIM,
                 'number': PHONE, 'caller': '', 'status': 'accepted'}
        users = {1, 2, 3}
        original = tuple(users)
        sending = asyncio.Event()
        resume = asyncio.Event()

        async def send(user, message, **kwargs):
            if user == original[0]:
                sending.set()
                await resume.wait()
            return object()

        bot.send_message = AsyncMock(side_effect=send)
        with patch.object(Config, 'AUTHORIZED_USERS', users):
            delivery = asyncio.create_task(bot.deliver_sms_item(event))
            try:
                await asyncio.wait_for(sending.wait(), timeout=1)
                users.remove(original[1])
                users.update({4, 5})
                resume.set()
                await asyncio.wait_for(delivery, timeout=1)
            finally:
                delivery.cancel()
                await asyncio.gather(delivery, return_exceptions=True)
        self.assertEqual([call.args[0] for call in bot.send_message.await_args_list],
                         [original[0], original[2]])

    async def test_call_event_rejects_bad_fields_and_handles_full_queue(self):
        AsteriskBot, _ = self.bot_classes()
        bot = AsteriskBot.__new__(AsteriskBot)
        bot.inbound_call_seen = {}
        bot.sms_outbox = asyncio.Queue(maxsize=1)
        bot.sms_queue_dropped = 0
        bot.inbound_call_queue_dropped = 0
        event = {'kind': 'inbound_call', 'event_id': '123.456', 'sim_id': SIM,
                 'number': PHONE, 'caller': '', 'status': 'accepted'}
        for key, value in (('sim_id', 'quectel0'), ('number', 'Unknown'), ('caller', '<html>'),
                           ('status', 'delivered')):
            invalid = dict(event, **{key: value})
            with patch('builtins.print'):
                await bot.process_sms_data(json.dumps(invalid))
        self.assertTrue(bot.sms_outbox.empty())
        bot.sms_outbox.put_nowait({'notice': 'start', 'device_id': 'quectel0'})
        with patch('builtins.print'):
            await bot.process_sms_data(json.dumps(event))
        self.assertEqual(bot.inbound_call_queue_dropped, 1)
        self.assertEqual(bot.sms_queue_dropped, 0)
        self.assertNotIn(event['event_id'], bot.inbound_call_seen)
        bot.sms_outbox.get_nowait()
        await bot.process_sms_data(json.dumps(event))
        self.assertEqual(bot.sms_outbox.qsize(), 1)
        self.assertIn(event['event_id'], bot.inbound_call_seen)

    async def test_call_event_without_number_omits_caller_and_delivery_error_isolated(self):
        AsteriskBot, Config = self.bot_classes()
        bot = AsteriskBot.__new__(AsteriskBot)
        event = {'kind': 'inbound_call', 'event_id': '123.456', 'sim_id': SIM,
                 'number': '', 'caller': '', 'status': 'accepted'}
        bot.send_message = AsyncMock(side_effect=[ConnectionError('failed'), object()])
        with patch.object(Config, 'AUTHORIZED_USERS', [1, 2]), patch('builtins.print'):
            await bot.deliver_sms_item(event)
        self.assertEqual(bot.send_message.await_count, 2)
        for call in bot.send_message.await_args_list:
            self.assertIn('SIM 尾号：7890', call.args[1])
            self.assertNotIn('来电号码', call.args[1])
        bot.send_message = AsyncMock(return_value=None)
        with patch.object(Config, 'AUTHORIZED_USERS', [1]), patch('builtins.print') as log:
            await bot.deliver_sms_item(event)
        bot.send_message.assert_awaited_once()
        log.assert_called_once_with('来电通知投递未确认；未自动重发')

    async def test_phone_selection_revalidates_line_and_survives_slot_swap(self):
        AsteriskBot, Config = self.bot_classes()
        with tempfile.TemporaryDirectory() as directory:
            bot = AsteriskBot.__new__(AsteriskBot)
            bot.inbound_call = InboundCallPolicy(Path(directory) / 'state.json', clock=lambda: 1000)
            bot.send_message = AsyncMock()
            bot.execute_command = AsyncMock()
            bot.execute_asterisk_cli = AsyncMock(side_effect=[
                device_rows('quectel0', 'quectel1'), device_state('quectel0'),
                device_state('quectel1', 'Unknown', '', '', 'Not initialized'),
                device_rows('quectel0', 'quectel1'), device_state('quectel0'),
                device_state('quectel1', 'Unknown', '', '', 'Not initialized')])
            with patch.object(Config, 'AUTHORIZED_USERS', {1}), patch.object(InboundCallPolicy, '_check_directory'):
                await bot.handle_inbound_call_command(1, '/inbound_call lines', 2)
                bot.execute_asterisk_cli.assert_not_awaited()
                await bot.handle_inbound_call_command(1, '/inbound_call mode ' + PHONE + ' direct', 1)
                state = bot.inbound_call.load()['sims']
                self.assertEqual(list(state), [SIM])
                self.assertEqual((state[SIM]['number'], state[SIM]['mode']), (PHONE, 'direct'))
                self.assertEqual(bot.execute_asterisk_cli.await_count, 6)
                self.assertTrue(all(call.args[0] == 'quectel show devices' or
                                    call.args[0] in ('quectel show device state quectel0', 'quectel show device state quectel1')
                                    for call in bot.execute_asterisk_cli.await_args_list))
                bot.execute_command.assert_not_awaited()
                bot.execute_asterisk_cli = AsyncMock(side_effect=[
                    device_rows('quectel0', 'quectel1'),
                    device_state('quectel0', 'Unknown', '', '', 'Not initialized'),
                    device_state('quectel1'),
                    device_rows('quectel0', 'quectel1'),
                    device_state('quectel0', 'Unknown', '', '', 'Not initialized'),
                    device_state('quectel1')])
                await bot.handle_inbound_call_command(1, '/inbound_call test ' + PHONE + ' 30', 1)
                self.assertEqual(list(bot.inbound_call.load()['sims']), [SIM])
                self.assertEqual(bot.inbound_call.load()['sims'][SIM]['test_until'], 1030)
                await bot.handle_inbound_call_command(1, '/inbound_call status', 1)
                self.assertIn(PHONE, bot.send_message.call_args.args[1])
                bot.execute_asterisk_cli = AsyncMock(side_effect=[
                    device_rows('quectel0', 'quectel1'), device_state('quectel0', 'Unknown', '', '', 'Not initialized'),
                    device_state('quectel1'),
                    device_rows('quectel0', 'quectel1'), device_state('quectel0', 'Unknown', '', '', 'Not initialized'),
                    device_state('quectel1')])
                await bot.handle_inbound_call_command(1, '/inbound_call lines', 1)
                self.assertIn(PHONE + '：可设置', bot.send_message.call_args.args[1])
                self.assertIn('quectel0：设备未连接或未就绪', bot.send_message.call_args.args[1])

    async def test_two_distinct_phone_lines_keep_separate_sim_policies(self):
        AsteriskBot, Config = self.bot_classes()
        second_phone = '+8613900139000'
        second_imsi = 'imsi:223456789012345'
        scan = [device_rows('quectel0', 'quectel1'), device_state('quectel0'),
                device_state('quectel1', second_phone, OTHER, second_imsi),
                device_rows('quectel0', 'quectel1'), device_state('quectel0'),
                device_state('quectel1', second_phone, OTHER, second_imsi)]
        with tempfile.TemporaryDirectory() as directory:
            bot = AsteriskBot.__new__(AsteriskBot)
            bot.inbound_call = InboundCallPolicy(Path(directory) / 'state.json')
            bot.send_message = AsyncMock()
            bot.execute_asterisk_cli = AsyncMock(side_effect=scan * 2)
            with patch.object(Config, 'AUTHORIZED_USERS', {1}), patch.object(InboundCallPolicy, '_check_directory'):
                await bot.handle_inbound_call_command(1, '/inbound_call mode ' + PHONE + ' direct', 1)
                await bot.handle_inbound_call_command(1, '/inbound_call mode ' + second_phone + ' forward', 1)
            sims = bot.inbound_call.load()['sims']
            self.assertEqual(set(sims), {SIM, OTHER})
            self.assertEqual((sims[SIM]['number'], sims[SIM]['mode']), (PHONE, 'direct'))
            self.assertEqual((sims[OTHER]['number'], sims[OTHER]['mode']), (second_phone, 'forward'))

    async def test_phone_selection_rejects_unknown_duplicate_incomplete_and_changed_state(self):
        AsteriskBot, Config = self.bot_classes()
        cases = [
            ('unknown', [device_rows('quectel0'), device_state('quectel0'),
                         device_rows('quectel0'), device_state('quectel0')], '+8613700137000'),
            ('duplicate', [device_rows('quectel0', 'quectel1'), device_state('quectel0'),
                           device_state('quectel1', PHONE, OTHER, 'imsi:223456789012345'),
                           device_rows('quectel0', 'quectel1'), device_state('quectel0'),
                           device_state('quectel1', PHONE, OTHER, 'imsi:223456789012345')], PHONE),
            ('missing identity', [device_rows('quectel0'), device_state('quectel0', sim='', imsi=''),
                                  device_rows('quectel0'), device_state('quectel0', sim='', imsi='')], PHONE),
            ('missing number', [device_rows('quectel0', 'quectel1'), device_state('quectel0'),
                                device_state('quectel1', 'Unknown', OTHER, 'imsi:223456789012345'),
                                device_rows('quectel0', 'quectel1'), device_state('quectel0'),
                                device_state('quectel1', 'Unknown', OTHER, 'imsi:223456789012345')], PHONE),
            ('changed', [device_rows('quectel0'), device_state('quectel0'),
                         device_rows('quectel0'), device_state('quectel0', '+8613900139000')], PHONE),
            ('wrong device', [device_rows('quectel0'), device_state('quectel1'),
                              device_rows('quectel0'), device_state('quectel1')], PHONE),
            ('cli error', ['Command failed'], PHONE),
            ('duplicate identity', [device_rows('quectel0', 'quectel1'), device_state('quectel0'),
                                    device_state('quectel1', '+8613900139000'),
                                    device_rows('quectel0', 'quectel1'), device_state('quectel0'),
                                    device_state('quectel1', '+8613900139000')], PHONE),
            ('stopped selected', [device_rows('quectel0'), device_state('quectel0', state='Stopped'),
                                  device_rows('quectel0'), device_state('quectel0', state='Stopped')], PHONE),
        ]
        for name, responses, selected in cases:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                bot = AsteriskBot.__new__(AsteriskBot)
                bot.inbound_call = InboundCallPolicy(Path(directory) / 'state.json')
                bot.send_message = AsyncMock()
                bot.execute_asterisk_cli = AsyncMock(side_effect=responses)
                with patch.object(Config, 'AUTHORIZED_USERS', {1}), patch.object(InboundCallPolicy, '_check_directory'):
                    await bot.handle_inbound_call_command(1, '/inbound_call mode ' + selected + ' direct', 1)
                self.assertFalse(bot.inbound_call.path.exists())
                self.assertIn('设置未保存', bot.send_message.call_args.args[1])

    async def test_line_list_does_not_offer_selection_with_invalid_policy(self):
        AsteriskBot, Config = self.bot_classes()
        with tempfile.TemporaryDirectory() as directory:
            bot = AsteriskBot.__new__(AsteriskBot)
            bot.inbound_call = InboundCallPolicy(Path(directory) / 'state.json')
            bot.inbound_call.path.write_text('invalid')
            bot.inbound_call.path.chmod(0o640)
            bot.send_message = AsyncMock()
            bot.execute_asterisk_cli = AsyncMock(side_effect=[device_rows('quectel0'), device_state('quectel0'),
                                                              device_rows('quectel0'), device_state('quectel0')])
            with patch.object(Config, 'AUTHORIZED_USERS', {1}):
                await bot.handle_inbound_call_command(1, '/inbound_call lines', 1)
            self.assertIn('暂无法按号码设置', bot.send_message.call_args.args[1])
            self.assertEqual(bot.inbound_call.path.read_text(), 'invalid')


if __name__ == '__main__':
    unittest.main()
