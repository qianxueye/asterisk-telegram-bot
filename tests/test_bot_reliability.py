import asyncio
import contextlib
import gc
import io
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from telethon import types
from telethon.network.mtprotosender import MTProtoSender
from asterisk_bot import AsteriskBot
from config import Config


class ReliabilityTests(unittest.IsolatedAsyncioTestCase):
    def bot(self):
        b = AsteriskBot.__new__(AsteriskBot)
        b.running = True
        b.pending_replies = {}
        b.pending_sms_sends = {}
        b.processes = []
        return b

    def test_real_aligned_health_and_fail_closed_states(self):
        text = ('Current device state    : Start\nDesired device state    : Start\n'
                'Voice                   : Yes\nCalls/Channels          : 0\n'
                'GSM Registration Status : Registered, home network')
        self.assertTrue(AsteriskBot.parse_uac_health(text))
        for replacement in ('Not registered', 'Unknown', 'Unregistered', 'Searching', ''):
            self.assertFalse(AsteriskBot.parse_uac_health(text.replace('Registered, home network', replacement)))
        self.assertTrue(AsteriskBot.parse_uac_health(text.replace('home network', 'roaming')))
        self.assertFalse(AsteriskBot.parse_uac_health(text.replace(': 0', ': 1')))
        self.assertFalse(AsteriskBot.parse_uac_health(text+'\nVoice: No'))
        self.assertFalse(AsteriskBot.parse_uac_health('No more connections allowed'))

    async def test_both_command_scopes_match_help_and_start(self):
        b = self.bot(); b.client = AsyncMock(return_value=True)
        await b.sync_bot_commands()
        requests = [call.args[0] for call in b.client.await_args_list]
        self.assertEqual([type(r.scope) for r in requests],
                         [types.BotCommandScopeDefault, types.BotCommandScopeUsers])
        names = [x[0] for x in b.COMMANDS]
        self.assertEqual(len(names), len(set(names)))
        for r in requests:
            self.assertEqual([c.command for c in r.commands], names)
            self.assertEqual(r.lang_code, '')
        for name in names:
            self.assertIn('/'+name, b.format_help_message())
        b.send_message = AsyncMock()
        with patch.object(Config, 'AUTHORIZED_USERS', {1}):
            await b.handle_start_command(1, 1)
        for name in names:
            self.assertIn('/'+name, b.send_message.await_args.args[1])
        self.assertIn('uac_recover', names)
        self.assertIn('add_user', names)

    async def test_network_failure_never_resends_or_logs_success(self):
        b = self.bot()
        b.client = SimpleNamespace(send_message=AsyncMock(side_effect=ConnectionError('do not expose secret')))
        b.get_device_list = AsyncMock(return_value=[])
        buf = io.StringIO()
        with patch.object(Config, 'AUTHORIZED_USERS', {1}), contextlib.redirect_stdout(buf):
            await b.push_sms_to_users('quectel1', 'sender', 'test', 'timestamp')
        self.assertEqual(b.client.send_message.await_count, 1)
        self.assertNotIn('已推送给用户', buf.getvalue())
        self.assertNotIn('do not expose secret', buf.getvalue())
        self.assertEqual(next(iter(b.pending_replies.values()))['delivery_status'], {1: 'unconfirmed'})
        b.send_message = AsyncMock()
        with patch.object(Config, 'AUTHORIZED_USERS', {1}):
            await b.handle_pending_sms_command(1, '/pending_sms', 1)
        self.assertIn('1 条收到的短信未确认投递', b.send_message.await_args.args[1])

    async def test_plain_parse_mode_is_explicit_and_cancel_propagates(self):
        b = self.bot(); b.client = SimpleNamespace(send_message=AsyncMock(return_value='ok'))
        self.assertEqual(await b.send_message(1, '**literal**', parse_mode=None), 'ok')
        self.assertIsNone(b.client.send_message.await_args.kwargs['parse_mode'])
        b.client.send_message.side_effect = asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            await b.send_message(1, 'test')

    async def test_single_disconnect_waiter_does_not_leak_errors(self):
        b=self.bot(); loop=asyncio.get_running_loop(); errors=[]; reads=[]
        previous=loop.get_exception_handler()
        loop.set_exception_handler(lambda loop,ctx: errors.append(ctx))
        class Client:
            def __init__(self):self._disconnected=loop.create_future()
            @property
            def disconnected(self):
                reads.append(1)
                return MTProtoSender.disconnected.fget(self)
            async def disconnect(self):b.running=False
        b.client=Client(); b.connect_telegram_with_retry=AsyncMock(return_value=True)
        original=asyncio.wait_for
        async def accelerated(future,timeout):return await original(future,min(timeout,.001))
        timer=loop.call_later(.04,lambda:b.client._disconnected.set_exception(ConnectionError('synthetic')))
        try:
            with patch('asyncio.wait_for',accelerated),contextlib.redirect_stdout(io.StringIO()):
                await b.run_telegram_until_stopped()
            gc.collect();await asyncio.sleep(.01)
            self.assertEqual(len(reads),1)
            self.assertEqual(errors,[])
        finally:
            timer.cancel();loop.set_exception_handler(previous)

    async def test_timeout_reaps_real_child_and_allows_next_command(self):
        b=self.bot();b.COMMAND_TIMEOUT_SECONDS=.1;b.PROCESS_STOP_SECONDS=.05
        with tempfile.TemporaryDirectory() as temp:
            pidfile=Path(temp)/'pid'
            code='import os,time; open('+repr(str(pidfile))+',"w").write(str(os.getpid())); time.sleep(30)'
            result=await asyncio.wait_for(b.run_bounded_command([sys.executable,'-c',code]),2)
            self.assertIn('超时',result)
            self.assertEqual(b.processes,[])
            pid=int(pidfile.read_text())
            with self.assertRaises(ProcessLookupError):os.kill(pid,0)
            self.assertEqual((await b.run_bounded_command([sys.executable,'-c','print("ok")'])).strip(),'ok')

    async def test_cancel_reaps_child_and_releases_semaphore(self):
        b=self.bot();b.COMMAND_TIMEOUT_SECONDS=5;b.PROCESS_STOP_SECONDS=.05
        task=asyncio.create_task(b.run_bounded_command([sys.executable,'-c','import time; time.sleep(30)']))
        for _ in range(100):
            if b.processes:break
            await asyncio.sleep(.01)
        self.assertEqual(len(b.processes),1);pid=b.processes[0].pid
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):await task
        self.assertEqual(b.processes,[])
        with self.assertRaises(ProcessLookupError):os.kill(pid,0)
        self.assertEqual(b.command_slots._value,b.COMMAND_CONCURRENCY)

    async def test_waiting_commands_do_not_spawn_and_cli_is_literal(self):
        b=self.bot();b.COMMAND_TIMEOUT_SECONDS=.02;b.command_slots=asyncio.Semaphore(0)
        with patch('asyncio.create_subprocess_exec',new_callable=AsyncMock) as create:
            result=await b.run_bounded_command(['not-executed'])
            self.assertIn('未启动进程',result);create.assert_not_called()
        b.run_bounded_command=AsyncMock(return_value='ok')
        with patch.object(Config,'ASTERISK_COMMAND_PREFIX','sudo asterisk -rx'):
            await b.execute_asterisk_cli('literal; $message')
        b.run_bounded_command.assert_awaited_once_with(['sudo','asterisk','-rx','literal; $message'])

    async def test_term_ignoring_child_is_killed_and_reaped(self):
        b=self.bot();b.COMMAND_TIMEOUT_SECONDS=.2;b.PROCESS_STOP_SECONDS=.02
        with tempfile.TemporaryDirectory() as temp:
            pidfile=Path(temp)/'pid'
            code=('import os,signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);'
                  'open('+repr(str(pidfile))+',"w").write(str(os.getpid()));time.sleep(30)')
            result=await asyncio.wait_for(b.run_bounded_command([sys.executable,'-c',code]),2)
            self.assertIn('超时',result)
            with self.assertRaises(ProcessLookupError):os.kill(int(pidfile.read_text()),0)
            self.assertEqual(b.processes,[])

    async def test_cancel_during_spawn_takes_ownership_of_child(self):
        b=self.bot();b.PROCESS_STOP_SECONDS=.02
        actual=asyncio.create_subprocess_exec;started=asyncio.Event();release=asyncio.Event();child=[]
        async def delayed(*args,**kwargs):
            p=await actual(*args,**kwargs);child.append(p);started.set()
            await release.wait();return p
        with patch('asyncio.create_subprocess_exec',delayed):
            task=asyncio.create_task(b.run_bounded_command([sys.executable,'-c','import time;time.sleep(30)']))
            await asyncio.wait_for(started.wait(),2)
            task.cancel();release.set()
            with self.assertRaises(asyncio.CancelledError):await task
        self.assertIsNotNone(child[0].returncode)
        self.assertEqual(b.processes,[])

    async def test_two_slots_bound_real_concurrency(self):
        b=self.bot();b.COMMAND_TIMEOUT_SECONDS=2
        actual=asyncio.create_subprocess_exec;peak=[]
        async def track(*args,**kwargs):
            p=await actual(*args,**kwargs)
            peak.append(len(b.processes)+1);return p
        with patch('asyncio.create_subprocess_exec',track):
            results=await asyncio.gather(*(b.run_bounded_command(
                [sys.executable,'-c','import time;time.sleep(.03);print("ok")']) for _ in range(6)))
        self.assertTrue(all(r.strip()=='ok' for r in results))
        self.assertLessEqual(max(peak),2)
        self.assertEqual(b.processes,[])
