#!/usr/bin/env python3
"""Answer selected mobile calls and send three fixed DTMF events."""
import json
import os
import re
import signal
import sys

from inbound_call import InboundCallPolicy, normalize_caller, sim_identities
from sms_ingress_agi import forward as forward_event


class CallEnded(Exception):
    pass


def _signal_end(signum, frame):
    raise CallEnded()


def agi(command, source=sys.stdin, sink=sys.stdout):
    sink.write(command + '\n')
    sink.flush()
    response = source.readline(4096)
    match = re.match(r'200 result=(-?\d+)(?:\s|$)', response)
    if not match or int(match.group(1)) < 0:
        raise CallEnded()
    return response


def variable(name, source=sys.stdin, sink=sys.stdout):
    if name not in ('QUECTEL', 'CALLERID(num)', 'CHANNEL(state)', 'SMS_PIPE'):
        raise ValueError('variable name')
    response = agi('GET FULL VARIABLE ${' + name + '}', source, sink)
    match = re.fullmatch(r'200 result=1 \((.*)\)\n', response)
    if not match:
        raise ValueError('variable missing')
    return match.group(1)


def handle_call(source=sys.stdin, sink=sys.stdout, policy=None):
    call_id = ''
    for _ in range(100):
        line = source.readline(4096)
        if line == '\n':
            break
        if not line or len(line) >= 4096:
            return 'forward'
        if line.startswith('agi_uniqueid: '):
            call_id = line[len('agi_uniqueid: '):].strip()
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
        try:
            pipe = variable('SMS_PIPE', source, sink)
        except ValueError:
            pipe = ''
        agi('SET VARIABLE INBOUND_CALL_SELECTED 1', source, sink)
        agi('ANSWER', source, sink)
    except (OSError, ValueError, TypeError, CallEnded):
        return 'forward'

    try:
        agi('SET AUTOHANGUP 60', source, sink)
        agi('EXEC SendDTMF ###,1500,100', source, sink)
        if (re.fullmatch(r'/[A-Za-z0-9_./-]{1,255}', pipe) and os.path.normpath(pipe) == pipe and
                re.fullmatch(r'[A-Za-z0-9_.-]{1,100}', call_id)):
            try:
                forward_event(pipe, {'kind': 'inbound_call', 'event_id': call_id,
                                     'sim_id': identities[0], 'number': item['number'],
                                     'caller': normalize_caller(caller) or '', 'status': 'accepted'}, timeout=.2)
            except CallEnded:
                raise
            except Exception:
                pass
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
