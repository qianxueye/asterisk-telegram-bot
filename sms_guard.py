"""Per-device OTP admission; persisted state contains counts and event IDs only."""
import copy
import json
import math
import os
import re
import tempfile
import time
from pathlib import Path


OTP = re.compile(r'验证码|校验码|验证代码|动态密码|动态口令|一次性密码|'
                 r'\b(?:verification|security|authentication|login|one[- ]time)\s+(?:code|password)\b|\bOTP\b', re.I)
CODE = re.compile(r'(?<![A-Za-z0-9])[A-Za-z0-9]{4,8}(?![A-Za-z0-9])')
SIM = re.compile(r'sim:[0-9]{14,24}\Z')
DEVICE = re.compile(r'quectel[0-9]{1,2}\Z')


class SmsFloodGuard:
    def __init__(self, path, clock=time.time, minute=15, five_minutes=30):
        self.path = Path(path)
        self.clock = clock
        self.minute = minute
        self.five_minutes = five_minutes
        self.state = {'version': 1, 'default_mode': 'auto', 'devices': {}, 'seen': {}, 'overrides': {}, 'last_clock': 0}
        self.last_saved = 0
        self.storage_failed = False
        if self.path.exists():
            try:
                if self.path.stat().st_size > 1048576:
                    raise ValueError('guard state size')
                data = json.loads(self.path.read_text())
                self.validate(data)
                self.state = data
            except (OSError, ValueError, TypeError, KeyError):
                self.storage_failed = True

    @staticmethod
    def validate(data):
        if not isinstance(data, dict) or set(data) != {'version', 'default_mode', 'devices', 'seen', 'overrides', 'last_clock'}:
            raise ValueError('guard schema')
        if not all(isinstance(data[key], dict) for key in ('devices', 'seen', 'overrides')):
            raise ValueError('guard maps')
        if not isinstance(data['last_clock'], (int, float)) or not math.isfinite(data['last_clock']):
            raise ValueError('guard clock')
        if data['version'] != 1 or data['default_mode'] not in ('auto', 'manual', 'off'):
            raise ValueError('guard state')
        if len(data['devices']) > 100 or len(data['seen']) > 1024:
            raise ValueError('guard capacity')
        for device, record in data['devices'].items():
            if not isinstance(record, dict) or set(record) != {'device', 'mode', 'active', 'started', 'dropped', 'last_seen', 'buckets'} or not isinstance(record['buckets'], dict):
                raise ValueError('guard record')
            if not (DEVICE.fullmatch(device) or SIM.fullmatch(device)) or not DEVICE.fullmatch(record['device']) or record['mode'] not in ('auto', 'manual', 'off'):
                raise ValueError('guard device')
            if len(record['buckets']) > 300 or not isinstance(record['active'], bool):
                raise ValueError('guard counters')
            for key, count in record['buckets'].items():
                int(key)
                if not isinstance(count, int) or not 0 <= count <= 30:
                    raise ValueError('guard count')
            for field in ('started', 'dropped', 'last_seen'):
                if not isinstance(record[field], (int, float)) or not math.isfinite(record[field]) or record[field] < 0:
                    raise ValueError('guard metadata')
        if len(data['overrides']) > 100 or any(not DEVICE.fullmatch(key) or value not in ('auto', 'manual', 'off') for key, value in data['overrides'].items()):
            raise ValueError('guard overrides')
        for key, timestamp in data['seen'].items():
            if not isinstance(key, str) or len(key) > 160 or not isinstance(timestamp, (int, float)) or not math.isfinite(timestamp):
                raise ValueError('guard event')

    def save(self, force=False):
        now = self.clock()
        if not force and now < self.last_saved + 1:
            return
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix='.sms-guard-', dir=self.path.parent)
        try:
            with os.fdopen(fd, 'w') as stream:
                json.dump(self.state, stream, separators=(',', ':'))
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
            self.last_saved = now
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def record(self, device, sim_id=''):
        if not DEVICE.fullmatch(device):
            raise ValueError('unknown device')
        key = 'sim:' + sim_id if re.fullmatch(r'[0-9]{14,24}', sim_id) else device
        if key not in self.state['devices'] and len(self.state['devices']) >= 100:
            oldest = min(self.state['devices'], key=lambda value: self.state['devices'][value]['last_seen'])
            del self.state['devices'][oldest]
        record = self.state['devices'].setdefault(key, {
            'device': device, 'mode': self.state['overrides'].get(device, self.state['default_mode']), 'active': False, 'started': 0,
            'dropped': 0, 'last_seen': 0, 'buckets': {}})
        record['device'] = device
        record['mode'] = self.state['overrides'].get(device, self.state['default_mode'])
        return record

    def prune(self, now):
        if now < self.state['last_clock']:
            self.storage_failed = True
            return
        self.state['last_clock'] = now
        self.state['seen'] = {key: value for key, value in self.state['seen'].items()
                              if now - 300 < value <= now}
        for record in self.state['devices'].values():
            record['buckets'] = {key: value for key, value in record['buckets'].items()
                                 if now - 300 < int(key) <= now}

    def set_mode(self, mode, device=None):
        if mode not in ('auto', 'manual', 'off'):
            raise ValueError('unknown mode')
        before = copy.deepcopy(self.state)
        try:
            if device is None:
                self.state['default_mode'] = mode
                self.state['overrides'].clear()
                records = self.state['devices'].values()
            else:
                self.record(device)
                self.state['overrides'][device] = mode
                records = [record for record in self.state['devices'].values() if record['device'] == device]
            for record in records:
                record.update(mode=mode, active=False, started=0, dropped=0)
            self.state['last_clock'] = int(self.clock())
            self.prune(self.state['last_clock'])
            self.save(force=True)
            self.storage_failed = False
        except OSError:
            self.state = before
            raise

    def admit(self, device, body, event_id, sim_id=''):
        now = int(self.clock())
        self.prune(now)
        record = self.record(device, sim_id)
        if event_id in self.state['seen']:
            return 'duplicate', None
        if len(self.state['seen']) >= 1024:
            del self.state['seen'][next(iter(self.state['seen']))]
        self.state['seen'][event_id] = now
        record['last_seen'] = now
        transition = None
        five = sum(record['buckets'].values())
        if record['active'] and now - record['started'] >= 300 and five < 5:
            record['active'] = False
            transition = ('end', record['dropped'])
        is_otp = bool(OTP.search(body)) and any(any(char.isdigit() for char in token) for token in CODE.findall(body))
        if not is_otp:
            if transition:
                try:
                    self.save(force=True)
                except OSError:
                    self.storage_failed = True
            return 'allow', transition
        key = str(now)
        record['buckets'][key] = min(30, record['buckets'].get(key, 0) + 1)
        minute = sum(count for second, count in record['buckets'].items() if int(second) > now - 60)
        five = sum(record['buckets'].values())
        if record['mode'] == 'auto' and not record['active'] and (minute >= self.minute or five >= self.five_minutes):
            record.update(active=True, started=now, dropped=0)
            transition = ('start', 0)
        drop = record['mode'] != 'off' and (self.storage_failed or record['mode'] == 'manual' or record['active'])
        if drop:
            record['dropped'] += 1
        try:
            self.save(force=transition is not None)
        except OSError:
            self.storage_failed = True
            drop = record['mode'] != 'off'
        return ('drop' if drop else 'allow'), transition

    def recover(self):
        now = int(self.clock())
        old_size = len(self.state['seen']) + sum(len(record['buckets']) for record in self.state['devices'].values())
        self.prune(now)
        result = []
        for device, record in self.state['devices'].items():
            if record['active'] and now - record['started'] >= 300 and sum(record['buckets'].values()) < 5:
                record['active'] = False
                result.append((record['device'], record['dropped']))
        new_size = len(self.state['seen']) + sum(len(record['buckets']) for record in self.state['devices'].values())
        if result or new_size != old_size:
            self.save(force=True)
        return result
