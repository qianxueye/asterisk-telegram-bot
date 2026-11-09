#!/usr/bin/env python3
"""Answer selected mobile calls and send three fixed DTMF events."""
import json
import re
import signal
import sys

from inbound_call import InboundCallPolicy, sim_identities


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
    if name not in ('QUECTEL', 'CALLERID(num)', 'CHANNEL(state)'):
        raise ValueError('variable name')
    response = agi('GET FULL VARIABLE ${' + name + '}', source, sink)
    match = re.fullmatch(r'200 result=1 \((.*)\)\n', response)
    if not match:
        raise ValueError('variable missing')
    return match.group(1)


def handle_call(source=sys.stdin, sink=sys.stdout, policy=None):
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
        if policy.snapshot(identities, caller) is None:
            return 'forward'
        agi('SET VARIABLE INBOUND_CALL_SELECTED 1', source, sink)
        agi('ANSWER', source, sink)
    except (OSError, ValueError, TypeError, CallEnded):
        return 'forward'

    try:
        agi('SET AUTOHANGUP 60', source, sink)
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
