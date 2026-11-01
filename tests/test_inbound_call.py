import asyncio
import fcntl
import importlib.util
import io
import json
import math
import os
import random
import select
import struct
import tempfile
import threading
import time
import types
import unittest
import wave
from pathlib import Path
from unittest.mock import AsyncMock, patch

from inbound_call import InboundCallPolicy, PolicyWriteUncertain, matches_secret, normalize_caller, parse_live_line_state, sim_identities, subscriber_number
from inbound_call_eagi import CallEnded, FREQUENCIES, RATE, ToneCollector, _write_replay, collect, handle_call


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


def audio(symbols, amplitude=1800, noise=0, duration=.16, gap=.12, lead=0):
    import math
    rng = random.Random(42)
    samples = [0] * int(RATE * lead)
    for symbol in symbols:
        low, high = FREQUENCIES[symbol]
        for index in range(int(RATE * duration)):
            value = amplitude * (math.sin(2 * math.pi * low * index / RATE) +
                                 math.sin(2 * math.pi * high * index / RATE))
            value += rng.uniform(-noise, noise)
            samples.append(int(value))
        samples.extend(int(rng.uniform(-noise, noise)) for _ in range(int(RATE * gap)))
    return struct.pack('<' + str(len(samples)) + 'h', *samples)


def telephone_audio(symbols, noise=1000, twist=.45, lead=0, seed=17):
    import math
    rng = random.Random(seed)
    samples = [0.0] * int(lead * RATE)
    for symbol in symbols:
        low, high = FREQUENCIES[symbol]
        offset = len(samples)
        for index in range(800):
            n = offset + index
            envelope = min(1, index / 70, (799 - index) / 70)
            samples.append(envelope * (1250 * math.sin(2 * math.pi * low * 1.003 * n / RATE) +
                                       1250 * twist * math.sin(2 * math.pi * high * 1.003 * n / RATE)) +
                           1000 * math.sin(2 * math.pi * 440 * n / RATE) + rng.uniform(-noise, noise))
        offset = len(samples)
        for index in range(640):
            n = offset + index
            samples.append(1000 * math.sin(2 * math.pi * 440 * n / RATE) + rng.uniform(-noise, noise))
    lowpass = highpass = previous_lowpass = 0.0
    output = []
    log_mu = math.log1p(255)
    for sample in samples:
        lowpass += .73 * (sample - lowpass)
        highpass = .81 * (highpass + lowpass - previous_lowpass)
        previous_lowpass = lowpass
        scaled = max(-1, min(1, highpass / 32768))
        encoded = math.copysign(math.log1p(255 * abs(scaled)) / log_mu, scaled)
        encoded = round((encoded + 1) * 127.5) / 127.5 - 1
        decoded = math.copysign(math.expm1(log_mu * abs(encoded)) / 255, encoded)
        output.append(int(max(-32768, min(32767, decoded * 32768))))
    return struct.pack('<' + str(len(output)) + 'h', *output)


def collect_bytes(data, length, pipe_size=None):
    read, write = os.pipe()
    if pipe_size is not None and hasattr(fcntl, 'F_SETPIPE_SZ'):
        fcntl.fcntl(write, fcntl.F_SETPIPE_SZ, pipe_size)
        assert fcntl.fcntl(write, fcntl.F_GETPIPE_SZ) <= pipe_size
    errors = []
    stopped = threading.Event()
    os.set_blocking(write, False)
    def produce():
        try:
            offset = 0
            while offset < len(data) and not stopped.is_set():
                if not select.select([], [write], [], .05)[1]:
                    continue
                try:
                    offset += os.write(write, data[offset:offset + 2048])
                except BlockingIOError:
                    continue
        except BrokenPipeError:
            pass
        except Exception as exc:
            errors.append(exc)
        finally:
            os.close(write)
    worker = threading.Thread(target=produce)
    worker.start()
    try:
        return collect(read, length, timeout=2)
    finally:
        stopped.set()
        os.close(read)
        worker.join(timeout=2)
        if worker.is_alive():
            raise AssertionError('audio producer did not stop')
        if errors:
            raise errors[0]


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
    def test_telephone_degradation_preserves_complete_sequence(self):
        for noise, twist in ((1000, .45), (2000, .6), (2500, 1)):
            for lead in (0, .005, .015):
                with self.subTest(noise=noise, twist=twist, lead=lead):
                    data = telephone_audio('12#2', noise=noise, twist=twist, lead=lead)
                    self.assertEqual(collect_bytes(data, 4), '12#2')
        with patch('inbound_call_eagi.MIN_TONE_POWER_RATIO', 26):
            with self.assertRaises(CallEnded):
                collect_bytes(telephone_audio('12#2', noise=1000, twist=.45), 4)

    def test_telephone_degradation_does_not_complete_on_partial_or_fake_audio(self):
        with self.assertRaises(CallEnded):
            collect_bytes(telephone_audio('12#', noise=2000, twist=.6), 4)
        rng = random.Random(41)
        for kind in ('white', 'correlated', 'speech', 'near_tones'):
            history = 0.0
            samples = []
            for index in range(RATE * 5):
                if kind == 'white':
                    value = rng.uniform(-4000, 4000)
                elif kind == 'correlated':
                    history = .94 * history + rng.uniform(-800, 800)
                    value = history
                elif kind == 'speech':
                    envelope = .5 + .5 * math.sin(2 * math.pi * 4 * index / RATE)
                    value = envelope * (2200 * math.sin(2 * math.pi * 440 * index / RATE) +
                                        1000 * math.sin(2 * math.pi * 880 * index / RATE) +
                                        600 * math.sin(2 * math.pi * 1760 * index / RATE)) + rng.uniform(-1300, 1300)
                else:
                    value = (1600 * math.sin(2 * math.pi * 730 * index / RATE) +
                             1600 * math.sin(2 * math.pi * 1270 * index / RATE) + rng.uniform(-300, 300))
                samples.append(int(max(-32768, min(32767, value))))
            fake = struct.pack('<' + str(len(samples)) + 'h', *samples)
            with self.subTest(kind=kind), self.assertRaises(CallEnded):
                collect_bytes(fake, 1)

    def test_eighty_ms_tones_and_gaps_at_frame_offsets(self):
        for lead in (0, .005, .01, .015, .02, .025, .035):
            with self.subTest(lead=lead):
                self.assertEqual(collect_bytes(audio('11#2', duration=.08, gap=.08, lead=lead), 4), '11#2')
                self.assertEqual(collect_bytes(audio('11#2', duration=.08, gap=.04, lead=lead), 4), '11#2')

    def test_short_interruption_does_not_make_two_repeated_digits(self):
        interrupted = (audio('1', duration=.2, gap=0) + b'\0' * (RATE * 2 // 50) +
                       audio('1', duration=.2, gap=.08))
        with self.assertRaises(CallEnded):
            collect_bytes(interrupted, 2)

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

    def test_speech_like_multifrequency_and_large_pipe_payload_do_not_complete(self):
        import math
        rng = random.Random(31)
        samples = [int(1000 * math.sin(2 * math.pi * 440 * n / RATE) +
                       700 * math.sin(2 * math.pi * 880 * n / RATE) +
                       350 * math.sin(2 * math.pi * 1760 * n / RATE) +
                       rng.uniform(-500, 500)) for n in range(RATE)]
        fake = struct.pack('<' + str(len(samples)) + 'h', *samples)
        with self.assertRaises(CallEnded):
            collect_bytes(fake, 1, pipe_size=4096)
        large = audio('1234', duration=.08, gap=.08) + fake * 8
        self.assertGreater(len(large), 65536)
        self.assertEqual(collect_bytes(large, 4, pipe_size=4096), '1234')

    def test_collection_deadline_with_open_silent_pipe(self):
        read, write = os.pipe()
        try:
            self.assertIsNone(collect(read, 1, timeout=.02))
        finally:
            os.close(read)
            os.close(write)

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

    def test_stale_replay_disappearing_during_cleanup_is_benign(self):
        with tempfile.TemporaryDirectory() as directory:
            stale = Path(directory) / 'tone-stale.wav'
            stale.write_bytes(b'old')
            age = time.time() - 180
            os.utime(stale, (age, age))
            real_unlink = os.unlink
            def raced_unlink(path):
                if path == str(stale):
                    real_unlink(path)
                    raise FileNotFoundError(path)
                real_unlink(path)
            with patch('inbound_call_eagi.os.unlink', side_effect=raced_unlink):
                fresh = _write_replay('1', 10, directory)
            self.assertFalse(stale.exists())
            self.assertTrue(Path(fresh).exists())
            Path(fresh).unlink()

    def test_stale_replay_cleanup_keeps_permission_error(self):
        with tempfile.TemporaryDirectory() as directory:
            stale = Path(directory) / 'tone-stale.wav'
            stale.write_bytes(b'old')
            age = time.time() - 180
            os.utime(stale, (age, age))
            with patch('inbound_call_eagi.os.unlink', side_effect=PermissionError('denied')):
                with self.assertRaises(PermissionError):
                    _write_replay('1', 10, directory)


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

    def test_direct_sends_three_fixed_digits_with_spacing(self):
        results = '200 result=0\n' * 4
        outcome, commands, sleep = self.run_call({'iccid': SIM.split(':')[1]}, '0211234567', results)
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
        sleep.assert_not_called()
        self.assertNotIn('HANGUP\n', commands)

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
        source = WaitingSource('agi_request: test\n\n200 result=1 (' +
                               json.dumps({'iccid': SIM.split(':')[1]}) + ')\n'
                               '200 result=1 (Ring)\n200 result=1 (0211234567)\n' +
                               '200 result=0\n' * 4)
        result = []
        worker = threading.Thread(target=lambda: result.append(handle_call(source, sink, policy=self.policy,
                                                                            tone_dir=self.tone_dir)))
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
            result, commands, _ = self.run_call(metadata, '0211234567', '200 result=0\n' * 5)
            self.assertEqual(result, 'sent')
            self.assertEqual(commands.count('EXEC SendDTMF ###,1500,100'), 1)
            self.assertNotIn('12#', commands)
            result, commands, _ = self.run_call(metadata, '0211234567', '200 result=0\n' * 4 + '200 result=-1\n')
            self.assertEqual(result, 'ended')
            self.assertEqual(commands.count('EXEC SendDTMF ###,1500,100'), 1)

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
        self.assertEqual(commands.count('EXEC SendDTMF ###,1500,100'), 1)
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

    async def test_authorization_setting_and_no_secret_in_reply(self):
        AsteriskBot, Config = self.bot_classes()
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
