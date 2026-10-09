#!/usr/bin/env python3
"""Read SMS channel variables without expanding the body in dialplan arguments."""
import errno
import fcntl
import json
import os
import re
import signal
import stat
import sys
import time
from datetime import datetime

MAX_FRAME = 262144


def variable(name, source, sink):
    sink.write('GET FULL VARIABLE ${' + name + '}\n')
    sink.flush()
    line = source.readline(MAX_FRAME * 2)
    if not line.startswith('200 result=1 (') or not line.endswith(')\n'):
        raise ValueError('AGI variable unavailable')
    return line[len('200 result=1 ('):-2]


def forward(pipe, envelope, timeout=2):
    payload = ('\n' + json.dumps(envelope, ensure_ascii=True, separators=(',', ':')) + '\n').encode()
    if len(payload) > MAX_FRAME:
        raise ValueError('SMS frame too large')
    deadline = time.monotonic() + timeout
    lockfd = os.open(pipe + '.writer.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    fd = None
    try:
        while True:
            try:
                fcntl.flock(lockfd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise TimeoutError('SMS writer busy')
                time.sleep(.01)
        while fd is None:
            try:
                fd = os.open(pipe, os.O_WRONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
            except OSError as exc:
                if exc.errno != errno.ENXIO or time.monotonic() >= deadline:
                    raise
                time.sleep(.01)
        if not stat.S_ISFIFO(os.fstat(fd).st_mode):
            raise ValueError('SMS destination is not FIFO')
        while payload:
            try:
                written = os.write(fd, payload[:4096])
                payload = payload[written:]
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise TimeoutError('SMS pipe full')
                time.sleep(.01)
    finally:
        if fd is not None:
            os.close(fd)
        os.close(lockfd)


def main(source=sys.stdin, sink=sys.stdout):
    for line in source:
        if line == '\n':
            break
    sms = json.loads(variable('SMS', source, sink))
    device = json.loads(variable('QUECTEL', source, sink))
    pipe = variable('SMS_PIPE', source, sink)
    event = variable('UNIQUEID', source, sink)
    name = device.get('name', '')
    if not re.fullmatch(r'quectel[0-9]{1,2}', name) or not re.fullmatch(r'[A-Za-z0-9_.-]{1,100}', event):
        raise ValueError('SMS identity')
    body = sms.get('msg', '')
    if not isinstance(body, str):
        raise ValueError('SMS body type')
    forward(pipe, {'device_id': name, 'sim_id': device.get('iccid') or device.get('imsi') or '',
                   'event_id': event, 'sender_number': sms.get('from', ''), 'content': body,
                   'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S')})


if __name__ == '__main__':
    signal.alarm(5)
    try:
        main()
    except Exception as exc:
        print('SMS ingress failed: ' + type(exc).__name__, file=sys.stderr)
        sys.exit(1)
