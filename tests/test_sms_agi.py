import io
import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from sms_ingress_agi import forward, main, variable


class AgiTests(unittest.TestCase):
    def test_full_variable_preserves_long_json_quotes_newlines_and_parentheses(self):
        body = '中文 (原文) "引号"\n' * 500
        encoded = json.dumps({'msg': body}, ensure_ascii=False)
        self.assertGreater(len(encoded), 1024)
        sink = io.StringIO()
        self.assertEqual(variable('SMS', io.StringIO('200 result=1 ('+encoded+')\n'), sink), encoded)
        self.assertEqual(sink.getvalue(), 'GET FULL VARIABLE ${SMS}\n')
        source = io.StringIO('agi_test: true\n\n200 result=1 ('+encoded+')\n'
                             '200 result=1 ({"name":"quectel0","iccid":"12345678901234567890"})\n'
                             '200 result=1 (/private/tmp/fifo)\n200 result=1 (123.456)\n')
        with patch('sms_ingress_agi.forward') as send:
            main(source, io.StringIO())
        self.assertEqual(send.call_args.args[1]['content'], body)
        self.assertEqual(send.call_args.args[1]['event_id'], '123.456')

    def test_concurrent_large_fifo_records_do_not_interleave(self):
        with tempfile.TemporaryDirectory() as directory:
            pipe = str(Path(directory)/'sms')
            os.mkfifo(pipe, 0o600)
            fd = os.open(pipe, os.O_RDONLY | os.O_NONBLOCK)
            collected = bytearray()
            failed = []
            def writer(number):
                try:
                    forward(pipe, {'event_id': str(number), 'content': '中文(正文)'+str(number)*15000})
                except Exception as exc:
                    failed.append(type(exc).__name__)
            threads = [threading.Thread(target=writer, args=(number,)) for number in range(4)]
            for thread in threads:
                thread.start()
            deadline = time.monotonic()+4
            while any(thread.is_alive() for thread in threads) and time.monotonic() < deadline:
                try:
                    collected.extend(os.read(fd, 65536))
                except BlockingIOError:
                    time.sleep(.001)
            for thread in threads:
                thread.join(timeout=.1)
            while True:
                try:
                    chunk = os.read(fd, 65536)
                    if not chunk:
                        break
                    collected.extend(chunk)
                except BlockingIOError:
                    break
            os.close(fd)
            self.assertFalse(any(thread.is_alive() for thread in threads))
            self.assertEqual(failed, [])
            records = [json.loads(line) for line in collected.decode().splitlines() if line]
            self.assertEqual({record['event_id'] for record in records}, {'0','1','2','3'})
            for record in records:
                self.assertEqual(record['content'], '中文(正文)'+record['event_id']*15000)

    def test_no_reader_full_fifo_and_regular_file_are_bounded(self):
        with tempfile.TemporaryDirectory() as directory:
            pipe = str(Path(directory)/'sms')
            os.mkfifo(pipe, 0o600)
            start = time.monotonic()
            with self.assertRaises(OSError):
                forward(pipe, {'content': 'body'}, timeout=.05)
            self.assertLess(time.monotonic()-start, .5)
            fd = os.open(pipe, os.O_RDONLY | os.O_NONBLOCK)
            filler = os.open(pipe, os.O_WRONLY | os.O_NONBLOCK)
            try:
                while True:
                    os.write(filler, b'x'*4096)
            except BlockingIOError:
                pass
            finally:
                os.close(filler)
            with self.assertRaises(TimeoutError):
                forward(pipe, {'content': 'x'*200000}, timeout=.05)
            os.close(fd)
            os.unlink(pipe)
            Path(pipe).write_text('original')
            with self.assertRaises(ValueError):
                forward(pipe, {'content': 'body'}, timeout=.05)
            self.assertEqual(Path(pipe).read_text(), 'original')

    def test_templates_never_expand_body_or_base64(self):
        for name in ('asterisk-dialplan.conf', 'asterisk-dialplan-integrated.conf'):
            source = Path(name).read_text()
            self.assertIn('AGI(sms_ingress_agi.py)', source)
            for forbidden in ('SMS_BODY', 'SMS_CONTENT', 'SMS_JSON', 'JSON_DECODE(SMS,msg)', 'BASE64_ENCODE'):
                self.assertNotIn(forbidden, source)
