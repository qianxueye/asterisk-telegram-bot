#!/usr/bin/env python3
"""Answer selected mobile calls and detect acoustic DTMF from EAGI fd 3."""
import json
import math
import os
import re
import select
import signal
import struct
import sys
import tempfile
import time
import wave

from inbound_call import InboundCallPolicy, matches_secret, sim_identities


RATE = 8000
FRAME = 320
HOP = 80
ROWS = (697, 770, 852, 941)
COLS = (1209, 1336, 1477, 1633)
SYMBOLS = ('123A', '456B', '789C', '*0#D')
TONE_DIR = '/var/lib/asterisk/sounds/inbound-dtmf-private'
FREQUENCIES = {SYMBOLS[row][col]: (ROWS[row], COLS[col]) for row in range(4) for col in range(4)}
WINDOW = tuple(.5 - .5 * math.cos(2 * math.pi * n / (FRAME - 1)) for n in range(FRAME))
MIN_TONE_POWER_RATIO = 6


class CallEnded(Exception):
    pass


def _signal_end(signum, frame):
    raise CallEnded()


def _power(samples, frequency):
    coefficient = 2 * math.cos(2 * math.pi * frequency / RATE)
    previous = older = 0.0
    for sample, window in zip(samples, WINDOW):
        current = sample * window + coefficient * previous - older
        older, previous = previous, current
    return previous * previous + older * older - coefficient * previous * older


def detect_tone(frame):
    if len(frame) != FRAME * 2:
        return None
    samples = struct.unpack('<320h', frame)
    energy = sum((sample * window) ** 2 for sample, window in zip(samples, WINDOW))
    if energy < 320 * 110 ** 2:
        return None
    row = sorted((( _power(samples, frequency), index) for index, frequency in enumerate(ROWS)), reverse=True)
    col = sorted((( _power(samples, frequency), index) for index, frequency in enumerate(COLS)), reverse=True)
    strongest_row, strongest_col = row[0][0], col[0][0]
    if (strongest_row < row[1][0] * 3.5 or strongest_col < col[1][0] * 3.5 or
            strongest_row < energy * MIN_TONE_POWER_RATIO or strongest_col < energy * MIN_TONE_POWER_RATIO or
            not .12 <= strongest_row / strongest_col <= 8):
        return None
    return SYMBOLS[row[0][1]][col[0][1]]


class ToneCollector:
    def __init__(self):
        self.candidate = None
        self.hits = 0
        self.first_hit = 0
        self.index = 0
        self.quiet = 2
        self.armed = True

    def feed(self, frame):
        self.index += 1
        tone = detect_tone(frame)
        if tone is None:
            self.quiet += 1
            if self.quiet >= 2 and not self.armed:
                self.armed = True
                self.candidate = None
                self.hits = 0
            return None
        self.quiet = 0
        if tone != self.candidate or self.index - self.first_hit > 8:
            self.candidate, self.hits, self.first_hit = tone, 1, self.index
        else:
            self.hits += 1
        if self.armed and self.hits >= 3 and self.index - self.first_hit >= 3:
            self.armed = False
            return tone
        return None


def collect(fd, length, timeout=15, clock=time.monotonic):
    deadline = clock() + timeout
    detector = ToneCollector()
    buffer = b''
    symbols = []
    while len(symbols) < length:
        remaining = deadline - clock()
        if remaining <= 0:
            return None
        ready, _, _ = select.select([fd], [], [], remaining)
        if not ready:
            return None
        chunk = os.read(fd, 4096)
        if not chunk:
            raise CallEnded()
        buffer += chunk
        while len(buffer) >= FRAME * 2:
            symbol = detector.feed(buffer[:FRAME * 2])
            buffer = buffer[HOP * 2:]
            if symbol:
                symbols.append(symbol)
                if len(symbols) == length:
                    return ''.join(symbols)
        if len(buffer) > FRAME * 2:
            raise ValueError('audio framing')
    return ''.join(symbols)


def agi(command, source=sys.stdin, sink=sys.stdout):
    sink.write(command + '\n')
    sink.flush()
    response = source.readline(4096)
    match = re.match(r'200 result=(-?\d+)(?:\s|$)', response)
    if not match or int(match.group(1)) < 0:
        raise CallEnded()
    return response


def variable(name, source=sys.stdin, sink=sys.stdout):
    if name not in ('QUECTEL', 'CALLERID(num)', 'CHANNEL(state)'):
        raise ValueError('variable name')
    response = agi('GET FULL VARIABLE ${' + name + '}', source, sink)
    match = re.fullmatch(r'200 result=1 \((.*)\)\n', response)
    if not match:
        raise ValueError('variable missing')
    return match.group(1)


def _write_replay(symbols, volume, directory=TONE_DIR):
    if not os.path.isdir(directory) or os.stat(directory).st_mode & 0o077:
        raise ValueError('tone directory permissions')
    cutoff = time.time() - 120
    with os.scandir(directory) as entries:
        for index, entry in enumerate(entries):
            if index >= 1024:
                raise ValueError('tone directory capacity')
            if entry.name.startswith('tone-') and entry.name.endswith('.wav'):
                try:
                    if entry.is_file(follow_symlinks=False) and entry.stat(follow_symlinks=False).st_mtime < cutoff:
                        os.unlink(entry.path)
                except FileNotFoundError:
                    continue
    fd, path = tempfile.mkstemp(prefix='tone-', suffix='.wav', dir=directory)
    try:
        with os.fdopen(fd, 'wb') as output:
            with wave.open(output, 'wb') as wav:
                wav.setnchannels(1)
                wav.setsampwidth(2)
                wav.setframerate(RATE)
                pcm = bytearray()
                scale = 15000 * volume / 100
                for symbol in symbols:
                    first, second = FREQUENCIES[symbol]
                    for n in range(960):
                        envelope = min(1.0, n / 80, (959 - n) / 80)
                        sample = int(scale * envelope * (math.sin(2 * math.pi * first * n / RATE) +
                                                         math.sin(2 * math.pi * second * n / RATE)))
                        pcm.extend(struct.pack('<h', sample))
                    pcm.extend(b'\0' * 1600)
                wav.writeframes(pcm)
        os.chmod(path, 0o600)
        return path
    except BaseException:
        os.unlink(path)
        raise


def handle_call(source=sys.stdin, sink=sys.stdout, audio_fd=3, policy=None, tone_dir=TONE_DIR):
    for _ in range(100):
        line = source.readline(4096)
        if line == '\n':
            break
        if not line or len(line) >= 4096:
            return 'forward'
    else:
        return 'forward'
    policy = policy or InboundCallPolicy()
    try:
        identities = sim_identities(json.loads(variable('QUECTEL', source, sink)))
        if not identities:
            return 'forward'
        if variable('CHANNEL(state)', source, sink) == 'Up':
            return 'forward'
        try:
            caller = variable('CALLERID(num)', source, sink)
        except ValueError:
            caller = ''
        item = policy.snapshot(identities, caller)
        if item is None:
            return 'forward'
        agi('SET VARIABLE INBOUND_CALL_SELECTED 1', source, sink)
        agi('ANSWER', source, sink)
    except (OSError, ValueError, TypeError, json.JSONDecodeError, CallEnded):
        return 'forward'

    try:
        agi('SET AUTOHANGUP 60', source, sink)
        if item['mode'] == 'protected':
            symbols = collect(audio_fd, item['length'])
            if symbols is None:
                return 'timeout'
            matched = matches_secret(item, symbols)
            path = None
            try:
                path = _write_replay(symbols, item['volume'], tone_dir)
                agi('STREAM FILE ' + path[:-4] + ' ""', source, sink)
            finally:
                if path is not None:
                    os.unlink(path)
            if not matched:
                return 'mismatch'
        agi('EXEC SendDTMF ###,1500,100', source, sink)
        return 'sent'
    except (OSError, ValueError, CallEnded):
        return 'ended'


def main():
    signal.signal(signal.SIGHUP, _signal_end)
    signal.signal(signal.SIGALRM, _signal_end)
    signal.alarm(60)
    try:
        handle_call()
    except (OSError, ValueError, CallEnded):
        pass
    finally:
        signal.alarm(0)


if __name__ == '__main__':
    main()
