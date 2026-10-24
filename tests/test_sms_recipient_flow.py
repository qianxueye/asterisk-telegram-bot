import ast
import asyncio
import contextlib
import io
from pathlib import Path
import re
import secrets
import time
from types import SimpleNamespace
import unicodedata
import unittest
from unittest.mock import AsyncMock


class Buttons:
    @staticmethod
    def inline(text, data):
        return SimpleNamespace(text=text, data=data.encode())


def flow_class():
    path = Path(__file__).resolve().parents[1] / 'asterisk_bot.py'
    source = ast.parse(path.read_text())
    methods = {
        'normalize_sms_recipient', 'handle_send_sms_phone', 'handle_send_sms_command',
        'show_sms_send_confirmation', 'handle_sms_send_confirmation', 'handle_send_sms_content',
        'handle_callback_action', 'handle_cancel_callback', 'is_active_flow', 'cancel_button',
        'escape_html', 'format_sms_content_block', 'create_flow_id',
        'handle_message_command', 'handle_user_state_message',
    }
    cls = next(node for node in source.body if isinstance(node, ast.ClassDef) and node.name == 'AsteriskBot')
    cls.body = [node for node in cls.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name in methods]
    namespace = {'re': re, 'time': time, 'secrets': secrets, 'unicodedata': unicodedata,
                 'Button': Buttons, 'Config': SimpleNamespace(is_authorized=lambda user: user == 1)}
    exec(compile(ast.fix_missing_locations(ast.Module(body=[cls], type_ignores=[])), str(path), 'exec'), namespace)
    return namespace['AsteriskBot']


Flow = flow_class()


class RecipientFlowTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.bot = Flow()
        self.bot.user_states = {}
        self.bot.send_message = AsyncMock(return_value=True)
        self.bot.send_sms = AsyncMock(return_value=True)
        self.event = SimpleNamespace(edit=AsyncMock(), answer=AsyncMock())

    def test_normalizes_domestic_and_explicit_international_numbers(self):
        for value, expected in [
            ('13800138000', '+8613800138000'), ('138 0013-8000', '+8613800138000'),
            ('１３８００１３８０００', '+8613800138000'), ('＋８６ １３８００１３８０００', '+8613800138000'),
            ('8613800138000', '+8613800138000'), ('008613800138000', '+8613800138000'),
            ('+8613800138000', '+8613800138000'), ('+14155552671', '+14155552671'),
            ('+85291234567', '+85291234567'), ('0014155552671', '+14155552671'),
        ]:
            with self.subTest(value=value):
                self.assertEqual(self.bot.normalize_sms_recipient(value), expected)

    def test_rejects_ambiguous_or_malformed_input(self):
        for value in [None, 13800138000, '', ' ', '1380013800', '138001380000', '013800138000',
                      '+861380013800', '+868613800138000', '+86+8613800138000', '+008613800138000',
                      '10086', '138OO138000', '13800138000,13900139000', '13800\n138000',
                      '13800138000;reboot', '13800138000 ext 2', '+123', '+0123456789',
                      '+1234567890123456', '9'*81]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.bot.normalize_sms_recipient(value)

    async def prepare(self):
        state = {'action': 'send_sms_phone', 'device_id': 'quectel1',
                 'sms_content': '<synthetic>', 'flow_id': 'old'}
        self.bot.user_states[1] = state
        await self.bot.handle_send_sms_phone(10, 1, '13800138000', state)
        self.bot.send_sms.assert_not_awaited()
        return self.bot.user_states[1]['flow_id']

    async def test_invalid_number_keeps_content_for_correction(self):
        state = {'action': 'send_sms_phone', 'device_id': 'quectel1',
                 'sms_content': '<synthetic>', 'flow_id': 'old'}
        self.bot.user_states[1] = state
        await self.bot.handle_send_sms_phone(10, 1, 'wrong', state)
        self.assertIs(self.bot.user_states[1], state)
        self.assertIn('短信内容已保留', self.bot.send_message.await_args.args[1])
        self.bot.send_sms.assert_not_awaited()

    async def test_both_manual_entrypoints_require_confirmation(self):
        token = await self.prepare()
        message = self.bot.send_message.await_args.args[1]
        self.assertIn('+8613800138000', message)
        self.assertIn('&lt;synthetic&gt;', message)
        self.assertEqual(self.bot.user_states[1]['action'], 'send_sms_confirm')
        await self.bot.handle_message_command(10, 1, '/send_sms   quectel1 13800138000 <command>')
        self.assertNotEqual(self.bot.user_states[1]['flow_id'], token)
        self.assertEqual(self.bot.user_states[1]['sms_content'], '<command>')
        self.bot.send_sms.assert_not_awaited()

    async def test_invalid_command_number_keeps_content(self):
        await self.bot.handle_send_sms_command(10, '/send_sms quectel1 wrong <command>', 1)
        self.assertEqual(self.bot.user_states[1]['action'], 'send_sms_phone')
        self.assertEqual(self.bot.user_states[1]['sms_content'], '<command>')
        self.bot.send_sms.assert_not_awaited()

    async def test_confirmation_is_bound_and_single_use(self):
        token = await self.prepare()
        for user, chat, supplied in [(2, 10, token), (1, 11, token), (1, 10, 'stale')]:
            await self.bot.handle_callback_action(self.event, 'sms_send:confirm:'+supplied, user, chat)
        self.bot.send_sms.assert_not_awaited()

        async def delayed_submission(*args, **kwargs):
            await asyncio.sleep(0)
            return True

        self.bot.send_sms.side_effect = delayed_submission
        await asyncio.gather(*[self.bot.handle_callback_action(
            self.event, 'sms_send:confirm:'+token, 1, 10) for _ in range(2)])
        self.bot.send_sms.assert_awaited_once_with(10, 'quectel1', '+8613800138000', '<synthetic>', silent=False)
        self.assertNotIn(1, self.bot.user_states)

    async def test_edit_and_cancel_invalidate_old_confirmation(self):
        token = await self.prepare()
        await self.bot.handle_callback_action(self.event, 'sms_send:edit:'+token, 1, 10)
        state = self.bot.user_states[1]
        self.assertEqual(state['action'], 'send_sms_phone')
        self.assertNotEqual(state['flow_id'], token)
        self.assertEqual(state['sms_content'], '<synthetic>')
        await self.bot.handle_callback_action(self.event, 'sms_send:confirm:'+token, 1, 10)
        self.bot.send_sms.assert_not_awaited()
        await self.bot.handle_user_state_message(10, 1, '13900139000')
        new_token = self.bot.user_states[1]['flow_id']
        await self.bot.handle_callback_action(self.event, 'cancel:'+token, 1, 10)
        self.assertIn(1, self.bot.user_states)
        await self.bot.handle_callback_action(self.event, 'cancel:'+new_token, 1, 10)
        self.assertNotIn(1, self.bot.user_states)
        await self.bot.handle_callback_action(self.event, 'sms_send:confirm:'+new_token, 1, 10)
        self.bot.send_sms.assert_not_awaited()

    async def test_uncertain_submission_is_not_retried(self):
        token = await self.prepare()
        self.bot.send_sms.return_value = False
        for _ in range(2):
            await self.bot.handle_callback_action(self.event, 'sms_send:confirm:'+token, 1, 10)
        self.bot.send_sms.assert_awaited_once()
        self.assertNotIn(1, self.bot.user_states)
        self.assertIn('未能确认提交', self.event.edit.await_args.args[0])

    async def test_recipient_edits_cannot_move_a_private_draft_to_another_chat(self):
        token = await self.prepare()
        state = self.bot.user_states[1]
        self.bot.send_message.reset_mock()
        await self.bot.handle_user_state_message(11, 1, '13900139000')
        self.assertIs(self.bot.user_states[1], state)
        self.assertEqual(state['chat_id'], 10)
        self.assertEqual(state['flow_id'], token)
        message = self.bot.send_message.await_args.args[1]
        self.assertNotIn('synthetic', message)
        self.assertNotIn('+8613800138000', message)
        self.bot.send_sms.assert_not_awaited()

    async def test_long_unicode_preview_is_bounded_complete_and_confirmed(self):
        body = ('😀<>&' * 900) + '尾部'
        await self.bot.show_sms_send_confirmation(10, 1, 'quectel1', '+8613800138000', body)
        calls = self.bot.send_message.await_args_list
        chunks = calls[:-1]
        self.assertEqual(''.join(call.args[1] for call in chunks), body)
        for call in chunks:
            self.assertLessEqual(len(call.args[1].encode('utf-16-le')), 6000)
            self.assertIsNone(call.kwargs['parse_mode'])
            self.assertNotIn('buttons', call.kwargs)
        self.assertIn('buttons', calls[-1].kwargs)
        self.assertEqual(self.bot.user_states[1]['action'], 'send_sms_confirm')
        self.assertEqual(self.bot.user_states[1]['sms_content'], body)
        self.bot.send_sms.assert_not_awaited()

    async def test_long_menu_content_keeps_the_number_prompt_bounded(self):
        body = '😀<>&' * 900
        state = {'action': 'send_sms', 'device_id': 'quectel1', 'flow_id': 'old', 'chat_id': 10}
        self.bot.user_states[1] = state
        await self.bot.handle_send_sms_content(10, 1, body, state)
        self.assertLess(len(self.bot.send_message.await_args.args[1]), 500)
        self.assertEqual(self.bot.user_states[1]['sms_content'], body)
        self.assertEqual(self.bot.user_states[1]['chat_id'], 10)
        self.bot.send_sms.assert_not_awaited()

    async def test_partial_or_uncertain_preview_never_enables_submission(self):
        for body, results in [('x'*4000, [True, None, True]), ('short', [None, True])]:
            with self.subTest(length=len(body)):
                self.bot.send_message = AsyncMock(side_effect=results)
                await self.bot.show_sms_send_confirmation(10, 1, 'quectel1', '+8613800138000', body)
                state = self.bot.user_states[1]
                self.assertEqual(state['action'], 'send_sms_phone')
                self.assertEqual(state['sms_content'], body)
                await self.bot.handle_callback_action(self.event, 'sms_send:confirm:'+state['flow_id'], 1, 10)
                self.bot.send_sms.assert_not_awaited()

    async def test_callback_ui_failures_do_not_discard_a_confirmed_submission(self):
        for failed in ('answer', 'edit'):
            with self.subTest(failed=failed):
                token = await self.prepare()
                self.event = SimpleNamespace(edit=AsyncMock(), answer=AsyncMock())
                getattr(self.event, failed).side_effect = ConnectionError('synthetic UI failure')
                self.bot.send_sms.reset_mock()
                with contextlib.redirect_stdout(io.StringIO()):
                    await self.bot.handle_callback_action(self.event, 'sms_send:confirm:'+token, 1, 10)
                self.bot.send_sms.assert_awaited_once()
                self.assertNotIn(1, self.bot.user_states)
                self.event.answer.side_effect = None
                self.event.edit.side_effect = None
                await self.bot.handle_callback_action(self.event, 'sms_send:confirm:'+token, 1, 10)
                self.bot.send_sms.assert_awaited_once()
                self.bot.send_sms.reset_mock()

    async def test_empty_content_is_not_accepted(self):
        state = {'action': 'send_sms', 'device_id': 'quectel1', 'flow_id': 'old'}
        self.bot.user_states[1] = state
        await self.bot.handle_send_sms_content(10, 1, ' \n', state)
        self.assertIs(self.bot.user_states[1], state)
        self.bot.send_sms.assert_not_awaited()

    async def test_send_boundary_rejects_bad_addresses_and_preserves_reply_addresses(self):
        path = Path(__file__).resolve().parents[1] / 'asterisk_bot.py'
        node = next(node for node in ast.walk(ast.parse(path.read_text()))
                    if isinstance(node, ast.AsyncFunctionDef) and node.name == 'send_sms')
        namespace = {'re': re, 'time': time, 'secrets': secrets}
        exec(compile(ast.fix_missing_locations(ast.Module(body=[node], type_ignores=[])),
                     str(path), 'exec'), namespace)
        self.bot.get_device_list = AsyncMock(return_value=[{'id': 'quectel1', 'number': 'synthetic'}])
        self.bot.execute_asterisk_cli = AsyncMock(return_value='[quectel1] SMS queued for send')
        self.bot.is_sms_send_successful = lambda result, device: True
        self.bot.pending_sms_sends = {}
        for value in ['10086;reboot', '13800138000\nother', '+86 13800138000', None]:
            self.assertFalse(await namespace['send_sms'](self.bot, 10, 'quectel1', value, 'synthetic'))
        self.bot.execute_asterisk_cli.assert_not_awaited()
        self.bot.get_device_list.assert_not_awaited()
        with contextlib.redirect_stdout(io.StringIO()):
            for value in ['10086', '+8613800138000']:
                self.assertTrue(await namespace['send_sms'](self.bot, 10, 'quectel1', value, 'synthetic'))
                self.assertEqual(self.bot.execute_asterisk_cli.await_args.args[0],
                                 'quectel sms send quectel1 '+value+' synthetic')


if __name__ == '__main__':
    unittest.main()
