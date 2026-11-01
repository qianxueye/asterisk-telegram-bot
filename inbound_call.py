"""Per-SIM incoming call policy shared by the Bot and Asterisk EAGI."""
import copy
import fcntl
import hashlib
import hmac
import json
import math
import os
import re
import secrets
import stat
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path


SIM_ID = re.compile(r'(?:iccid:[0-9]{18,22}|imsi:[0-9]{14,16})\Z')
DTMF = re.compile(r'[0-9*#ABCD]+\Z')
CALLER = re.compile(r'021[0-9]{7,8}\Z')
MODES = ('forward', 'direct', 'protected')
DEFAULT_PATH = '/var/lib/asterisk-telegram-bot/inbound-call.json'


class PolicyWriteUncertain(OSError):
    pass


def sim_identity(metadata):
    if not isinstance(metadata, dict):
        return None
    for field in ('iccid', 'imsi'):
        value = metadata.get(field)
        if value is not None and value != '':
            return field + ':' + value if isinstance(value, str) and SIM_ID.fullmatch(field + ':' + value) else None
    return None


def normalize_caller(value):
    if not isinstance(value, str):
        return None
    number = re.sub(r'[\s()-]', '', value)
    if number.startswith('+86'):
        number = number[3:]
    elif number.startswith('0086'):
        number = number[4:]
    return number if CALLER.fullmatch(number) else None


class InboundCallPolicy:
    def __init__(self, path=DEFAULT_PATH, clock=time.time):
        self.path = Path(path)
        self.clock = clock

    def load(self):
        try:
            fd = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(fd) as stream:
                details = os.fstat(stream.fileno())
                if not stat.S_ISREG(details.st_mode) or details.st_size > 65536 or details.st_mode & 0o777 != 0o640:
                    return None
                data = json.load(stream)
            if not isinstance(data, dict) or data.get('version') != 1 or not isinstance(data.get('sims'), dict) or len(data['sims']) > 20:
                return None
            for sim, item in data['sims'].items():
                if not SIM_ID.fullmatch(sim) or not isinstance(item, dict) or set(item) != {'mode', 'salt', 'digest', 'length', 'volume', 'test_until'}:
                    return None
                if item['mode'] not in MODES or type(item['length']) is not int or item['length'] < 0:
                    return None
                if type(item['volume']) is not int or not 1 <= item['volume'] <= 30:
                    return None
                if type(item['test_until']) not in (int, float) or not math.isfinite(item['test_until']) or item['test_until'] < 0:
                    return None
                if not isinstance(item['salt'], str) or not isinstance(item['digest'], str):
                    return None
                if item['salt'] or item['digest']:
                    if not (isinstance(item['salt'], str) and isinstance(item['digest'], str) and
                            re.fullmatch(r'[0-9a-f]{32}', item['salt']) and re.fullmatch(r'[0-9a-f]{64}', item['digest']) and item['length'] > 0):
                        return None
                elif item['length']:
                    return None
            return data
        except (OSError, ValueError, TypeError):
            return None

    def snapshot(self, sim, caller):
        state = self.load()
        if state is None or sim not in state['sims']:
            return None
        item = state['sims'][sim]
        if item['mode'] == 'forward':
            return None
        if item['mode'] == 'protected' and not item['digest']:
            return None
        if self.clock() >= item['test_until'] and normalize_caller(caller) is None:
            return None
        return copy.deepcopy(item)

    def update(self, sim, field, value):
        if not SIM_ID.fullmatch(sim):
            raise ValueError('SIM identity')
        self._check_directory()
        with self._writer_lock():
            return self._update_locked(sim, field, value)

    @contextmanager
    def _writer_lock(self):
        fd = os.open(str(self.path) + '.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode) or os.fstat(fd).st_mode & 0o077:
                raise PermissionError('policy lock permissions')
            deadline = time.monotonic() + 3
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError('policy writer busy')
                    time.sleep(.01)
            yield
        finally:
            os.close(fd)

    def _update_locked(self, sim, field, value):
        state = self.load()
        if state is None:
            if self.path.exists():
                raise ValueError('policy file')
            state = {'version': 1, 'sims': {}}
        state = copy.deepcopy(state)
        if sim not in state['sims'] and len(state['sims']) >= 20:
            raise ValueError('SIM capacity')
        item = state['sims'].setdefault(sim, {'mode': 'forward', 'salt': '', 'digest': '', 'length': 0, 'volume': 10, 'test_until': 0})
        if field == 'mode' and value in MODES:
            if value == 'protected' and not item['digest']:
                raise ValueError('secret required')
            item['mode'] = value
        elif field == 'secret' and isinstance(value, str) and DTMF.fullmatch(value):
            salt = secrets.token_bytes(16)
            item.update(salt=salt.hex(), digest=hashlib.pbkdf2_hmac('sha256', value.encode(), salt, 200000).hex(), length=len(value))
        elif field == 'volume' and isinstance(value, int) and 1 <= value <= 30:
            item['volume'] = value
        elif field == 'test' and isinstance(value, int) and 0 <= value <= 900:
            item['test_until'] = self.clock() + value if value else 0
        else:
            raise ValueError('policy setting')
        fd, temporary = tempfile.mkstemp(prefix='.inbound-call-', dir=self.path.parent)
        replaced = False
        try:
            with os.fdopen(fd, 'w') as stream:
                json.dump(state, stream, separators=(',', ':'))
                stream.flush()
                os.fchmod(stream.fileno(), 0o640)
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
            replaced = True
            directory = os.open(self.path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        except OSError as exc:
            if replaced:
                raise PolicyWriteUncertain('policy durability') from exc
            raise
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        return item

    def _check_directory(self):
        directory_mode = self.path.parent.stat().st_mode
        if not (directory_mode & stat.S_ISGID) or directory_mode & 0o077 != 0o050:
            raise PermissionError('policy directory permissions')


def matches_secret(item, symbols):
    if len(symbols) != item['length'] or not DTMF.fullmatch(symbols):
        return False
    candidate = hashlib.pbkdf2_hmac('sha256', symbols.encode(), bytes.fromhex(item['salt']), 200000)
    return hmac.compare_digest(candidate, bytes.fromhex(item['digest']))
