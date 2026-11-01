import asyncio
import importlib.util
import io
import json
import os
import random
import struct
import tempfile
import threading
import time
import types
import unittest
import wave
from pathlib import Path
from unittest.mock import AsyncMock, patch

from inbound_call import InboundCallPolicy, PolicyWriteUncertain, matches_secret, normalize_caller, sim_identity
from inbound_call_eagi import CallEnded, FREQUENCIES, RATE, ToneCollector, _write_replay, collect, handle_call


SIM = 'iccid:12345678901234567890'
OTHER = 'iccid:22345678901234567890'


def audio(symbols, amplitude=1800, noise=0, duration=.16, gap=.12):
    import math
    rng = random.Random(42)
    samples = []
    for symbol in symbols:
        low, high = FREQUENCIES[symbol]
        for index in range(int(RATE * duration)):
            value = amplitude * (math.sin(2 * math.pi * low * index / RATE) +
                                 math.sin(2 * math.pi * high * index / RATE))
            value += rng.uniform(-noise, noise)
            samples.append(int(value))
        samples.extend(int(rng.uniform(-noise, noise)) for _ in range(int(RATE * gap)))
    return struct.pack('<' + str(len(samples)) + 'h', *samples)


def collect_bytes(data, length):
    read, write = os.pipe()
    try:
        os.write(write, data)
        os.close(write)
        write = None
        return collect(read, length, timeout=.2)
    finally:
        os.close(read)
        if write is not None:
            os.close(write)


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
        self.assertEqual(sim_identity({'iccid': SIM.split(':')[1], 'imsi': '123456789012345'}), SIM)
        self.assertIsNone(sim_identity({'iccid': 'invalid', 'imsi': '123456789012345'}))
        self.assertIsNone(sim_identity({'name': 'quectel1'}))
        for value in ('0211234567', '02112345678', '+86 02112345678', '0086-0211234567'):
            self.assertEqual(normalize_caller(value), value.replace('+86', '').replace('0086', '').replace(' ', '').replace('-', ''))
        for value in ('021123456', '021123456789', '021123456x', '+1 0211234567', '8610211234567', '0211234567#'):
            self.assertIsNone(normalize_caller(value))

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

    def test_secret_hash_permissions_and_fail_safe_config(self):
        self.policy.update(SIM, 'secret', '12#*')
        self.policy.update(SIM, 'mode', 'protected')
        item = self.policy.snapshot(SIM, '0211234567')
        self.assertTrue(matches_secret(item, '12#*'))
        self.assertFalse(matches_secret(item, '12#0'))
        self.assertFalse(matches_secret(item, '12#'))
        self.assertNotIn('12#*', self.policy.path.read_text())
        self.assertEqual(self.policy.path.stat().st_mode & 0o777, 0o640)
        self.policy.path.write_text('{bad')
        self.assertIsNone(self.policy.snapshot(SIM, '0211234567'))
        with self.assertRaises(ValueError):
            self.policy.update(SIM, 'mode', 'direct')

    def test_no_arbitrary_secret_length_limit(self):
        self.policy.update(SIM, 'secret', '12#' * 100)
        self.policy.update(SIM, 'mode', 'protected')
        item = self.policy.snapshot(SIM, '0211234567')
        self.assertEqual(item['length'], 300)
        self.assertTrue(matches_secret(item, '12#' * 100))

    def test_reader_observes_atomic_snapshots_during_updates(self):
        self.policy.update(SIM, 'mode', 'direct')
        errors = []
        def writer():
            for value in range(1, 31):
                self.policy.update(SIM, 'volume', value)
        worker = threading.Thread(target=writer)
        worker.start()
        while worker.is_alive():
            if self.policy.snapshot(SIM, '0211234567') is None:
                errors.append('invalid snapshot')
        worker.join()
        self.assertEqual(errors, [])
        self.assertEqual(self.policy.load()['sims'][SIM]['volume'], 30)

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
            second = threading.Thread(target=change, args=('volume', 22))
            first.start()
            second.start()
            start.wait()
            first.join()
            second.join()
        self.assertEqual(errors, [])
        item = self.policy.load()['sims'][SIM]
        self.assertEqual((item['mode'], item['volume']), ('direct', 22))
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
        target.write_text('{"version":1,"sims":{}}')
        self.policy.path.symlink_to(target)
        self.assertIsNone(self.policy.load())


class AudioTests(unittest.TestCase):
    def test_noisy_low_level_and_repeated_digits(self):
        self.assertEqual(collect_bytes(audio('12#2', amplitude=700, noise=250), 4), '12#2')
        self.assertEqual(collect_bytes(audio('11', amplitude=1400, noise=100), 2), '11')

    def test_noise_speech_like_wrong_frequency_and_partial_never_complete(self):
        with self.assertRaises(CallEnded):
            collect_bytes(audio('123', noise=100), 4)
        rng = random.Random(12)
        noise = struct.pack('<3200h', *(rng.randint(-1800, 1800) for _ in range(3200)))
        detector = ToneCollector()
        self.assertEqual([detector.feed(noise[i:i+640]) for i in range(0, len(noise), 640)], [None] * 10)
        with self.assertRaises(CallEnded):
            collect_bytes(audio('1', duration=.04), 1)

    def test_disconnect_and_private_replay_file(self):
        read, write = os.pipe()
        os.close(write)
        with self.assertRaises(CallEnded):
            collect(read, 1, timeout=.01)
        os.close(read)
        with tempfile.TemporaryDirectory() as directory:
            path = _write_replay('12#', 10, directory)
            self.assertNotIn('12#', path)
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
            with wave.open(path, 'rb') as wav:
                data = wav.readframes(wav.getnframes())
            self.assertEqual(collect_bytes(data, 3), '12#')
            low = _write_replay('1', 5, directory)
            high = _write_replay('1', 30, directory)
            with wave.open(low, 'rb') as wav:
                quiet = struct.unpack('<' + str(wav.getnframes()) + 'h', wav.readframes(wav.getnframes()))
            with wave.open(high, 'rb') as wav:
                loud = struct.unpack('<' + str(wav.getnframes()) + 'h', wav.readframes(wav.getnframes()))
            self.assertGreater(max(loud), max(quiet) * 5)


class EagiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        checker = patch.object(InboundCallPolicy, '_check_directory')
        checker.start()
        self.addCleanup(checker.stop)
        self.policy = InboundCallPolicy(Path(self.temp.name) / 'state.json', clock=lambda: 1000)
        self.tone_dir = str(Path(self.temp.name) / 'tones')
        os.mkdir(self.tone_dir, 0o700)
        self.policy.update(SIM, 'mode', 'direct')

    def run_call(self, metadata, caller, results='', policy=None, state='Ring'):
        source = io.StringIO('agi_request: test\n\n200 result=1 (' + json.dumps(metadata) + ')\n'
                             '200 result=1 (' + state + ')\n'
                             '200 result=1 (' + caller + ')\n' + results)
        sink = io.StringIO()
        with patch('inbound_call_eagi.time.sleep') as sleep:
            outcome = handle_call(source, sink, policy=policy or self.policy, tone_dir=self.tone_dir)
        return outcome, sink.getvalue(), sleep

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

    def test_direct_sends_three_fixed_digits_with_spacing(self):
        results = '200 result=0\n' * 6
        outcome, commands, sleep = self.run_call({'iccid': SIM.split(':')[1]}, '0211234567', results)
        self.assertEqual(outcome, 'sent')
        self.assertEqual(commands.count('EXEC SendDTMF #,1500,100'), 3)
        self.assertEqual([call.args for call in sleep.call_args_list], [(1.5,), (1.5,)])

    def test_protected_full_mismatch_timeout_and_match(self):
        self.policy.update(SIM, 'secret', '12#')
        self.policy.update(SIM, 'mode', 'protected')
        metadata = {'iccid': SIM.split(':')[1]}
        with patch('inbound_call_eagi.collect', return_value=None):
            result, commands, _ = self.run_call(metadata, '0211234567', '200 result=0\n' * 3)
            self.assertEqual(result, 'timeout')
            self.assertNotIn('SendDTMF', commands)
        with patch('inbound_call_eagi.collect', return_value='12*'):
            result, commands, _ = self.run_call(metadata, '0211234567', '200 result=0\n' * 4)
            self.assertEqual(result, 'mismatch')
            self.assertIn('STREAM FILE', commands)
            self.assertNotIn('12*', commands)
            self.assertNotIn('SendDTMF', commands)
        with patch('inbound_call_eagi.collect', return_value='12#'):
            result, commands, _ = self.run_call(metadata, '0211234567', '200 result=0\n' * 7)
            self.assertEqual(result, 'sent')
            self.assertEqual(commands.count('EXEC SendDTMF #,1500,100'), 3)
            self.assertNotIn('12#', commands)

    def test_selection_answer_disconnect_and_playback_failures(self):
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
        self.assertEqual(commands.count('EXEC SendDTMF #,1500,100'), 1)
        self.policy.update(SIM, 'secret', '12#')
        self.policy.update(SIM, 'mode', 'protected')
        with patch('inbound_call_eagi.collect', side_effect=CallEnded):
            result, commands, _ = self.run_call(metadata, '0211234567', '200 result=0\n' * 3)
            self.assertEqual(result, 'ended')
            self.assertNotIn('SendDTMF', commands)
        with patch('inbound_call_eagi.collect', return_value='12#'):
            result, commands, _ = self.run_call(metadata, '0211234567', '200 result=0\n' * 3 + '200 result=-1\n')
            self.assertEqual(result, 'ended')
            self.assertIn('STREAM FILE', commands)
            self.assertNotIn('SendDTMF', commands)
            self.assertEqual(os.listdir(self.tone_dir), [])

    def test_dialplan_preserves_existing_routes_and_answer_boundary(self):
        source = (Path(__file__).resolve().parents[1] / 'asterisk-dialplan-integrated.conf').read_text()
        mobile = source.split('[incoming-mobile]', 1)[1].split('[general-sms]', 1)[0]
        self.assertLess(mobile.index('EAGI(inbound_call_eagi.py)'), mobile.index('Dial(PJSIP/${EXTEN}@10002)'))
        self.assertIn('INBOUND_CALL_SELECTED', mobile)
        self.assertIn('CHANNEL(state)', mobile)
        self.assertIn('AGI(sms_ingress_agi.py)', mobile)
        self.assertIn('exten => ussd', mobile)


class BotTests(unittest.IsolatedAsyncioTestCase):
    async def test_authorization_setting_and_no_secret_in_reply(self):
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
            AsteriskBot = module.AsteriskBot
            Config = module.Config
        with tempfile.TemporaryDirectory() as directory:
            bot = AsteriskBot.__new__(AsteriskBot)
            bot.inbound_call = InboundCallPolicy(Path(directory) / 'state.json', clock=lambda: 1000)
            bot.send_message = AsyncMock()
            with patch.object(Config, 'AUTHORIZED_USERS', {1}), patch.object(InboundCallPolicy, '_check_directory'):
                await bot.handle_inbound_call_command(2, '/inbound_call secret ' + SIM + ' 12#', 2)
                self.assertFalse(bot.inbound_call.path.exists())
                await bot.handle_inbound_call_command(1, '/inbound_call mode ' + SIM + ' protected', 1)
                self.assertIn('请先设置', bot.send_message.call_args.args[1])
                await bot.handle_inbound_call_command(1, '/inbound_call secret ' + SIM + ' 12#', 1)
                await bot.handle_inbound_call_command(1, '/inbound_call mode ' + SIM + ' protected', 1)
                await bot.handle_inbound_call_command(1, '/inbound_call status', 1)
                self.assertIn('测试：关闭', bot.send_message.call_args.args[1])
                await bot.handle_inbound_call_command(1, '/inbound_call test ' + SIM + ' 30', 1)
                await bot.handle_inbound_call_command(1, '/inbound_call status', 1)
                self.assertIn('测试：开启', bot.send_message.call_args.args[1])
            self.assertEqual(bot.inbound_call.load()['sims'][SIM]['mode'], 'protected')
            for call in bot.send_message.call_args_list:
                self.assertNotIn('12#', str(call))
            self.assertIn('inbound_call', [name for name, _ in AsteriskBot.COMMANDS])


if __name__ == '__main__':
    unittest.main()
