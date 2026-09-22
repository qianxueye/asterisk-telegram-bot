"""Offline SMS presentation tests using the real, pinned Telethon HTML parser.

Import Telethon before the legacy offline tests can install their lightweight
stub. No Telegram client, FIFO, subprocess, or network connection is created.
"""
import html
import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from telethon.extensions import html as telegram_html
from telethon.helpers import add_surrogate, del_surrogate
from telethon.tl.types import MessageEntityBold, MessageEntityCode, MessageEntityPre

from asterisk_bot import AsteriskBot
from config import Config


class SmsAssertions:
    timestamp = '2031-02-03 04:05:06'

    def make_bot(self):
        bot = AsteriskBot.__new__(AsteriskBot)
        bot.client = SimpleNamespace(send_message=AsyncMock(return_value=object()))
        bot.get_device_list = AsyncMock(return_value=[
            {'id': 'quectel0', 'number': '测试线路 <A>&B'},
        ])
        bot.pending_replies = {}
        bot.user_states = {}
        return bot

    def sms_info(self, content, **overrides):
        info = {
            'device': 'quectel0',
            'device_display': '测试设备 <A>&B',
            'sender': '通知 <sender>&',
            'content': content,
            'timestamp': self.timestamp,
        }
        info.update(overrides)
        return info

    def assert_plain_incoming(self, formatted, info):
        text, entities = telegram_html.parse(formatted)
        content = info.get('content', '')
        body = '(空内容)' if content is None or content == '' else content
        device = info.get('device_display') or info.get('device', 'Unknown')
        prefix = '📩 新短信\n\n'
        footer = (
            f"\n\n设备 {device}\n"
            f"来自 {info.get('sender', 'Unknown')}\n"
            f"时间 {info.get('timestamp', '')}"
        )
        self.assertEqual(text, prefix + body + footer)
        self.assertTrue(formatted.startswith('📩 <b>新短信</b>\n\n'))

        # Telegram entity offsets count UTF-16 code units, including emoji.
        body_start = len(add_surrogate(prefix))
        body_end = body_start + len(add_surrogate(body))
        body_entities = [entity for entity in entities
                         if entity.offset < body_end
                         and entity.offset + entity.length > body_start]
        self.assertFalse(any(isinstance(entity, (MessageEntityCode, MessageEntityPre))
                             for entity in body_entities))
        if content is not None and content != '':
            # HTML-looking SMS content and numbers must not inject formatting.
            self.assertEqual(body_entities, [])
            self.assertIn(html.escape(content, quote=False), formatted)
        bold_text = [del_surrogate(add_surrogate(text)[entity.offset:entity.offset + entity.length])
                     for entity in entities if isinstance(entity, MessageEntityBold)]
        self.assertEqual(bold_text, ['新短信'])
        return text, entities


class SmsFormattingTests(SmsAssertions, unittest.TestCase):
    def test_body_preserves_multiline_crlf_whitespace_and_unicode(self):
        bot = self.make_bot()
        contents = [
            '第一行\n第二行\n第三行',
            '第一行\r\n第二行\r\n',
            '  \t前导空格\n末尾空格 \t  ',
            '\n\n  前后空行  \r\n\n',
            '  \t\r\n  ',
            '中文 café العربية 👩🏽\u200d💻 🛰️\n下一行 🌙',
        ]
        for content in contents:
            with self.subTest(content=repr(content)):
                info = self.sms_info(content)
                self.assert_plain_incoming(bot.format_original_sms_message(info), info)

    def test_body_and_all_metadata_are_escaped_without_html_injection(self):
        bot = self.make_bot()
        info = self.sms_info(
            '<b>不要加粗</b> & <pre>不要变代码</pre>\n'
            '<a href="https://example.invalid/secret">普通原文</a>',
            device_display='<i>设备</i> & "别名"',
            sender='<a href="https://example.invalid/sender">来源</a>',
            timestamp='<b>2031-02-03</b> & 04:05:06',
        )
        self.assert_plain_incoming(bot.format_original_sms_message(info), info)

    def test_codes_and_unrelated_numbers_remain_unclassified_plain_text(self):
        bot = self.make_bot()
        for content in ('739204', '验证码 739204；订单 271828，金额 161803.50',
                        'Ticket 271828 closes at 18:30; total 42.'):
            with self.subTest(content=content):
                info = self.sms_info(content)
                self.assert_plain_incoming(bot.format_original_sms_message(info), info)

    def test_empty_content_has_visible_placeholder_before_metadata(self):
        bot = self.make_bot()
        for content in ('', None):
            with self.subTest(content=content):
                info = self.sms_info(content)
                self.assert_plain_incoming(bot.format_original_sms_message(info), info)

    def test_old_records_and_empty_display_name_fall_back_to_device(self):
        bot = self.make_bot()
        for display in (None, ''):
            info = self.sms_info('旧记录正文', device='legacy <device>&')
            if display is None:
                del info['device_display']
            else:
                info['device_display'] = display
            with self.subTest(display=display):
                self.assert_plain_incoming(bot.format_original_sms_message(info), info)

    def test_outgoing_content_block_and_silent_sms_keep_code_presentation(self):
        bot = self.make_bot()
        content = '保留 <原文> & 换行\n第二行'
        self.assertEqual(bot.format_sms_content_block(content),
                         '<pre>' + html.escape(content, quote=False) + '</pre>')
        _, entities = telegram_html.parse(bot.format_sms_content_block(content))
        self.assertTrue(any(isinstance(entity, MessageEntityPre) for entity in entities))
        silent = bot.format_silent_sms_message('quectel0', '测试来源', self.timestamp,
                                              content, 'Type 0')
        text, entities = telegram_html.parse(silent)
        self.assertIn('SILENT SMS 检测', text)
        code_texts = [del_surrogate(add_surrogate(text)[entity.offset:entity.offset + entity.length])
                      for entity in entities if isinstance(entity, MessageEntityCode)]
        self.assertIn(repr(content), code_texts)


class SmsFlowTests(SmsAssertions, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.bot = self.make_bot()
        self.users = {101, 202}
        authorization = patch.object(Config, 'AUTHORIZED_USERS', self.users)
        authorization.start()
        self.addCleanup(authorization.stop)
        quiet = patch('builtins.print')
        quiet.start()
        self.addCleanup(quiet.stop)

    def event(self):
        return SimpleNamespace(edit=AsyncMock(), answer=AsyncMock())

    async def push_normal(self, content='  正文 <标签>&\r\nhttps://example.invalid/sms  '):
        with patch.object(self.bot, 'format_original_sms_message',
                          wraps=self.bot.format_original_sms_message) as formatter:
            await self.bot.push_sms_to_users('quectel0', '测试来源 <S>&', content,
                                             self.timestamp, is_silent=False)
            formatter.assert_called_once()
        self.assertEqual(len(self.bot.pending_replies), 1)
        sms_id, info = next(iter(self.bot.pending_replies.items()))
        self.assertEqual(info['content'], content)
        self.assertEqual(info['device_display'], 'quectel0 测试线路 <A>&B')
        self.assertEqual(info['device'], 'quectel0')
        expected = self.bot.format_original_sms_message(info)
        calls = self.bot.client.send_message.await_args_list
        self.assertEqual({call.kwargs['entity'] for call in calls}, self.users)
        for call in calls:
            self.assertEqual(call.kwargs['message'], expected)
            self.assertEqual(call.kwargs['parse_mode'], 'html')
            self.assertIs(call.kwargs['link_preview'], False)
            self.assertEqual(call.kwargs['buttons'][0][0].data,
                             ('reply_sms:' + sms_id).encode())
        self.assert_plain_incoming(expected, info)
        return sms_id, info, expected

    async def test_normal_push_and_cancel_restore_are_identical(self):
        sms_id, info, expected = await self.push_normal()
        raw = dict(info)
        event = self.event()
        await self.bot.handle_reply_sms_callback(event, sms_id, 101, 101)
        state = self.bot.user_states[101]
        self.assertEqual(state['original_message'], expected)
        event.edit.reset_mock()
        await self.bot.handle_cancel_reply_callback(event, 101, 101, state['flow_id'])
        event.edit.assert_awaited_once()
        self.assertEqual(event.edit.await_args.args[0], expected)
        self.assertIs(event.edit.await_args.kwargs['link_preview'], False)
        self.assertEqual(self.bot.pending_replies[sms_id], raw)
        self.assertNotIn(101, self.bot.user_states)

    async def test_submitted_reply_restores_original_plain_body_and_metadata(self):
        sms_id, info, expected = await self.push_normal()
        event = self.event()
        await self.bot.handle_reply_sms_callback(event, sms_id, 101, 101)
        state = self.bot.user_states[101]
        self.bot.client.send_message.reset_mock()
        self.bot.send_sms = AsyncMock(return_value=True)
        await self.bot.handle_reply_sms_content(101, 101, '测试回复 <内容>', state)
        self.bot.send_sms.assert_awaited_once_with(
            101, 'quectel0', info['sender'], '测试回复 <内容>', silent=True)
        calls = self.bot.client.send_message.await_args_list
        self.assertEqual(len(calls), 2)
        self.assertIn('<pre>', calls[0].kwargs['message'])
        self.assertIs(calls[0].kwargs['link_preview'], True)
        self.assertEqual(calls[1].kwargs['message'], expected)
        self.assertIs(calls[1].kwargs['link_preview'], False)
        self.assertNotIn(101, self.bot.user_states)
        self.assertNotIn(sms_id, self.bot.pending_replies)

    async def test_cancel_restores_legacy_pending_record_without_display_name(self):
        info = self.sms_info('旧记录 <原文> &', device='legacy <D>')
        del info['device_display']
        self.bot.pending_replies['legacy_sms'] = info
        self.bot.user_states[101] = {'sms_id': 'legacy_sms', 'flow_id': 'legacy_flow'}
        event = self.event()
        await self.bot.handle_cancel_reply_callback(event, 101, 101, 'legacy_flow')
        restored = event.edit.await_args.args[0]
        self.assert_plain_incoming(restored, info)
        self.assertIs(event.edit.await_args.kwargs['link_preview'], False)
        self.assertEqual(self.bot.pending_replies['legacy_sms'], info)

    async def test_tp_pid_classified_normal_branch_uses_common_plain_formatter(self):
        content = '普通通知 <正文> & https://example.invalid/details'
        instant = datetime(2031, 2, 3, 4, 5, 6)
        with patch('asterisk_bot.datetime') as clock, patch.object(
                self.bot, 'format_original_sms_message',
                wraps=self.bot.format_original_sms_message) as formatter:
            clock.now.return_value = instant
            await self.bot.send_silent_sms_content_notification(
                'quectel0', '测试来源', content, '0x00', is_silent=False)
            formatter.assert_called_once()
            info = formatter.call_args.args[0]
        original = self.bot.format_original_sms_message(info)
        self.assert_plain_incoming(original, info)
        for call in self.bot.client.send_message.await_args_list:
            self.assertTrue(call.kwargs['message'].startswith(original))
            self.assertIn('检测到TP-PID但内容正常', call.kwargs['message'])
            self.assertIs(call.kwargs['link_preview'], False)
        self.assertEqual(self.bot.client.send_message.await_count, len(self.users))

    async def test_silent_push_keeps_report_and_no_reply_button(self):
        await self.bot.push_sms_to_users('quectel0', '测试来源', '', self.timestamp,
                                        is_silent=True, reason='Type 0')
        self.assertEqual(self.bot.client.send_message.await_count, len(self.users))
        for call in self.bot.client.send_message.await_args_list:
            self.assertIn('SILENT SMS 检测', call.kwargs['message'])
            self.assertIsNone(call.kwargs['buttons'])
            self.assertIs(call.kwargs['link_preview'], True)

    async def test_send_wrapper_preserves_preview_default_and_each_fallback(self):
        for parse_mode in ('html', None):
            for failure_count in range(2) if parse_mode else range(1):
                with self.subTest(parse_mode=parse_mode, failure_count=failure_count):
                    result = object()
                    client_send = AsyncMock(side_effect=[
                        *[__import__('telethon').errors.EntityBoundsInvalidError(request=None) for _ in range(failure_count)],
                        result,
                    ])
                    self.bot.client.send_message = client_send
                    buttons = [[object()]]
                    returned = await self.bot.send_message(
                        101, 'https://example.invalid/message', parse_mode=parse_mode,
                        buttons=buttons, link_preview=False)
                    self.assertIs(returned, result)
                    self.assertEqual(client_send.await_count, failure_count + 1)
                    for call in client_send.await_args_list:
                        self.assertIs(call.kwargs['link_preview'], False)
                        self.assertIs(call.kwargs['buttons'], buttons)
        self.bot.client.send_message = AsyncMock(return_value='sent')
        self.assertEqual(await self.bot.send_message(101, '其他通知'), 'sent')
        self.assertIs(self.bot.client.send_message.await_args.kwargs['link_preview'], True)


if __name__ == '__main__':
    unittest.main()
