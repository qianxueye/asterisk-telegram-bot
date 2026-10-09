#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Asterisk Telegram Bot
使用Telethon API实现的Telegram Bot，用于控制Asterisk系统
优化版本 - 提高性能和SMS检测准确性
"""

import asyncio
import subprocess
import re
import json
import os
import time
import signal
import sys
import shlex
import base64
import stat
import secrets
from datetime import datetime, timedelta
from collections import defaultdict
from typing import Dict, List, Optional
from telethon import TelegramClient, events
from telethon.tl.functions.messages import SendMessageRequest
from telethon.tl.types import UpdateNewMessage, Message
from telethon.tl.custom import Button
from config import Config
from sms_guard import SmsFloodGuard, DEVICE


class AsteriskBot:
    COMMANDS = (
        ('start', '开始使用'), ('help', '查看帮助'),
        ('send_sms', '发送短信'), ('devices', '查看设备列表'),
        ('device_settings', '查看设备设置'), ('device_state', '查看设备状态'),
        ('device_statistics', '查看设备统计'), ('recover_numbers', '恢复设备本机号码'),
        ('uac_recover', '检查与恢复 UAC 音频'), ('pending_sms', '查看待确认发送记录'),
        ('sms_notifications', '设置短信发送通知'), ('sms_guard', '短信防护'),
        ('add_user', '添加授权用户'), ('remove_user', '移除授权用户'),
        ('test_sms_log', '测试短信日志解析'), ('check_sms_logs', '查看近期短信日志'),
    )
    COMMAND_TIMEOUT_SECONDS = 15
    COMMAND_CONCURRENCY = 2
    PROCESS_STOP_SECONDS = 2

    def __init__(self):
        self.bot_token = Config.BOT_TOKEN
        self.sms_pipe_path = Config.SMS_PIPE_PATH
        self.log_file = self.detect_asterisk_log_file()
        self.silent_sms_queue = defaultdict(list)
        self.suspected_sms_queue = defaultdict(list)  # 疑似Silent SMS队列
        self.log_cache = []  # 日志缓存，用于快速查找
        self.log_cache_max_size = 1000  # 最大缓存行数
        self.log_cache_max_age = 300  # 缓存最大年龄（秒）
        self.user_states = {}  # 用户状态管理
        self.pending_replies = {}  # 待回复的SMS
        self.running = False
        self.client = None
        self.command_slots = asyncio.Semaphore(self.COMMAND_CONCURRENCY)
        self.processes = []  # 跟踪所有子进程
        self.sms_notifications = True  # SMS通知开关
        self.pending_sms_sends = {}  # 待确认的SMS发送请求
        self.phone_recovery_failures = defaultdict(int)
        self.last_phone_recovery_notice = {}
        self.uac_device_locks = defaultdict(asyncio.Lock)
        self.uac_error_events = defaultdict(list)
        self.uac_recovery_state = self.load_uac_recovery_state()
        self.sms_guard = SmsFloodGuard(os.getenv('SMS_GUARD_STATE_FILE', os.path.join(os.path.dirname(os.path.abspath(__file__)), '.sms_guard_state.json')))
        self.sms_outbox = asyncio.Queue(maxsize=64)
        self.sms_queue_dropped = 0
        self.background_tasks = []
        self.event_handlers_ready = False
        self.setup_sms_pipe()
        self.setup_signal_handlers()
        
        # 初始化Telethon客户端
        proxy = self.get_proxy_config()
        self.client = TelegramClient('asterisk_bot_session', api_id=Config.API_ID, api_hash=Config.API_HASH, proxy=proxy)
    
    def get_proxy_config(self):
        """获取代理配置"""
        if not Config.PROXY_TYPE or not Config.PROXY_HOST or not Config.PROXY_PORT:
            print("未配置代理，使用直连")
            return None
            
        print(f"配置代理: {Config.PROXY_TYPE}://{Config.PROXY_HOST}:{Config.PROXY_PORT}")
        
        if Config.PROXY_TYPE.lower() in ['http', 'socks4', 'socks5']:
            import socks
            proxy_types = {
                'http': socks.HTTP,
                'socks4': socks.SOCKS4,
                'socks5': socks.SOCKS5,
            }
            proxy_type = proxy_types[Config.PROXY_TYPE.lower()]
            proxy = {
                'proxy_type': proxy_type,
                'addr': Config.PROXY_HOST,
                'port': Config.PROXY_PORT,
                'rdns': Config.PROXY_RDNS,
            }
            if Config.PROXY_USERNAME:
                proxy['username'] = Config.PROXY_USERNAME
            if Config.PROXY_PASSWORD:
                proxy['password'] = Config.PROXY_PASSWORD
            return proxy
        elif Config.PROXY_TYPE.lower() == 'mtproto':
            # MTProto代理配置
            from telethon.network.connection.tcpmtproxy import ConnectionTcpMTProxy
            return {
                'connection': ConnectionTcpMTProxy,
                'proxy_ip': Config.PROXY_HOST,
                'proxy_port': Config.PROXY_PORT,
                'secret': Config.PROXY_PASSWORD if Config.PROXY_PASSWORD else None
            }
        
        return None
    
    def detect_asterisk_log_file(self):
        """自动检测Asterisk日志文件"""
        if Config.ASTERISK_LOG_FILE:
            if os.path.exists(Config.ASTERISK_LOG_FILE):
                print(f"📋 使用配置的Asterisk日志文件: {Config.ASTERISK_LOG_FILE}")
                return Config.ASTERISK_LOG_FILE
            print(f"⚠️ 配置的Asterisk日志文件不存在: {Config.ASTERISK_LOG_FILE}")

        possible_log_files = [
            '/var/log/asterisk/full.log',      # chan_quectel发送成功/失败写在verbose日志
            '/var/log/asterisk/full',
            '/var/log/asterisk/messages.log',
            '/var/log/asterisk/messages',
            '/var/log/asterisk/asterisk.log',
            '/var/log/asterisk.log'
        ]
        
        for log_file in possible_log_files:
            if os.path.exists(log_file):
                print(f"📋 检测到Asterisk日志文件: {log_file}")
                return log_file
        
        # 如果都没找到，返回默认路径
        default_log = '/var/log/asterisk/full.log'
        print(f"⚠️ 未找到Asterisk日志文件，使用默认路径: {default_log}")
        return default_log
    
    def setup_sms_pipe(self):
        """设置SMS管道"""
        try:
            pipe_dir = os.path.dirname(self.sms_pipe_path)
            if pipe_dir:
                os.makedirs(pipe_dir, exist_ok=True)

            if os.path.exists(self.sms_pipe_path) and not stat.S_ISFIFO(os.stat(self.sms_pipe_path).st_mode):
                os.unlink(self.sms_pipe_path)

            if not os.path.exists(self.sms_pipe_path):
                os.mkfifo(self.sms_pipe_path, 0o600)
                print(f"📡 创建SMS管道: {self.sms_pipe_path}")
            os.chmod(self.sms_pipe_path, 0o600)
        except Exception as e:
            print(f"创建SMS管道失败: {str(e)}")
    
    def setup_signal_handlers(self):
        """设置信号处理器"""
        def signal_handler(signum, frame):
            print(f"\n🛑 收到信号 {signum}，正在关闭...")
            self.running = False
            try:
                loop = asyncio.get_running_loop()
                if self.client and self.client.is_connected():
                    disconnect_result = self.client.disconnect()
                    if asyncio.isfuture(disconnect_result) or asyncio.iscoroutine(disconnect_result):
                        asyncio.ensure_future(disconnect_result)
            except RuntimeError:
                pass
            
            # 如果是第二次收到SIGINT，强制退出
            if signum == signal.SIGINT and hasattr(self, '_interrupt_count'):
                self._interrupt_count += 1
                if self._interrupt_count >= 2:
                    print("🚨 强制退出...")
                    sys.exit(1)
            else:
                self._interrupt_count = 1
        
        # 注册信号处理器
        signal.signal(signal.SIGINT, signal_handler)
        signal.signal(signal.SIGTERM, signal_handler)
    
    async def cleanup_processes(self):
        """清理所有子进程"""
        print("🧹 正在清理子进程...")
        for process in self.processes:
            if process.returncode is None:
                try:
                    process.terminate()
                    await asyncio.wait_for(process.wait(), timeout=5)
                    print(f"✅ 进程 {process.pid} 已终止")
                except asyncio.TimeoutError:
                    process.kill()
                    print(f"⚠️ 强制终止进程 {process.pid}")
                except Exception as e:
                    print(f"❌ 清理进程时出错: {str(e)}")
        self.processes.clear()
    
    def escape_html(self, text: str) -> str:
        """转义HTML特殊字符"""
        text = '' if text is None else str(text)
        text = text.replace('&', '&amp;')
        text = text.replace('<', '&lt;')
        text = text.replace('>', '&gt;')
        return text

    def create_flow_id(self) -> str:
        """生成短流程ID，用于过滤过期按钮回调。"""
        return f"{int(time.time())}-{secrets.token_hex(3)}"

    @staticmethod
    def is_valid_quectel_device(device_id: str) -> bool:
        """Only module-generated quectel device identifiers may reach the CLI."""
        return bool(re.fullmatch(r'quectel[0-9]+', device_id or ''))

    def load_uac_recovery_state(self) -> dict:
        """Load recovery rate-limit state. A corrupt file must never disable safety limits."""
        try:
            with open(Config.UAC_RECOVERY_STATE_FILE, 'r', encoding='utf-8') as state_file:
                state = json.load(state_file)
            return state if isinstance(state, dict) and isinstance(state.get('devices', {}), dict) else {'devices': {}}
        except FileNotFoundError:
            return {'devices': {}}
        except (OSError, ValueError, TypeError) as e:
            print(f"⚠️ 无法读取UAC恢复状态，使用安全的新状态: {e}")
            return {'devices': {}}

    def save_uac_recovery_state(self) -> None:
        """Atomically persist state with owner-only permissions."""
        state_path = Config.UAC_RECOVERY_STATE_FILE
        directory = os.path.dirname(state_path) or '.'
        try:
            os.makedirs(directory, mode=0o700, exist_ok=True)
            temporary = f"{state_path}.tmp-{os.getpid()}"
            flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
            fd = os.open(temporary, flags, 0o600)
            with os.fdopen(fd, 'w', encoding='utf-8') as state_file:
                json.dump(self.uac_recovery_state, state_file, ensure_ascii=False, sort_keys=True)
                state_file.flush()
                os.fsync(state_file.fileno())
            os.replace(temporary, state_path)
            os.chmod(state_path, 0o600)
        except OSError as e:
            print(f"❌ 无法保存UAC恢复状态: {e}")

    def uac_state_for(self, device_id: str) -> dict:
        return self.uac_recovery_state.setdefault('devices', {}).setdefault(device_id, {})

    def cancel_button(self, flow_id: str = None, label: str = "❌ 取消"):
        data = f"cancel:{flow_id}" if flow_id else "cancel"
        return Button.inline(label, data)

    def cancel_reply_button(self, flow_id: str = None):
        data = f"cancel_reply:{flow_id}" if flow_id else "cancel_reply"
        return Button.inline("❌ 取消回复", data)

    def is_active_flow(self, user_id: int, flow_id: str = None) -> bool:
        """没有flow_id的旧按钮不能取消一个新的带token流程。"""
        user_state = self.user_states.get(user_id)
        if not user_state:
            return True
        active_flow = user_state.get('flow_id')
        if not flow_id:
            return not active_flow
        return active_flow == flow_id

    def format_sms_content_block(self, content: str) -> str:
        if content is None or content == "":
            return "<i>(空内容)</i>"
        return f"<pre>{self.escape_html(content)}</pre>"

    def preview_text(self, text: str, limit: int = 80) -> str:
        text = '' if text is None else str(text)
        text = text.replace('\r', ' ').replace('\n', ' ')
        if len(text) > limit:
            return text[:limit - 1] + '…'
        return text
    
    def convert_to_html(self, text: str) -> str:
        """将Markdown格式转换为HTML格式"""
        # 转换粗体：**text** -> <b>text</b>
        text = re.sub(r'\*\*([^*]+)\*\*', r'<b>\1</b>', text)
        
        # 转换斜体：*text* -> <i>text</i> (但不在**text**内部)
        text = re.sub(r'(?<!\*)\*([^*]+)\*(?!\*)', r'<i>\1</i>', text)
        
        # 转换下划线：__text__ -> <u>text</u>
        text = re.sub(r'__([^_]+)__', r'<u>\1</u>', text)
        
        # 转换删除线：~~text~~ -> <s>text</s>
        text = re.sub(r'~~([^~]+)~~', r'<s>\1</s>', text)
        
        # 转换代码：`text` -> <code>text</code>
        text = re.sub(r'`([^`]+)`', r'<code>\1</code>', text)
        
        # 转换代码块：```text``` -> <pre>text</pre>
        text = re.sub(r'```([^`]+)```', r'<pre>\1</pre>', text)
        
        return text
    
    def clean_markdown_text(self, text: str) -> str:
        """清理Markdown文本，确保格式正确并增强可读性"""
        # Telethon使用标准的Markdown格式：**text** 表示粗体，*text* 表示斜体
        # 直接返回文本，让Telethon自己处理Markdown解析
        return text
    
    def enhance_message_formatting(self, text: str) -> str:
        """增强消息格式化，添加更多样式提升可读性"""
        # 1. 为重要信息添加代码格式（直接使用HTML格式）
        # 2. 为状态信息添加适当的格式化
        # 3. 为错误信息添加删除线或特殊标记
        
        # 增强时间显示
        text = re.sub(r'(\d{2}:\d{2}:\d{2})', r'<code>\1</code>', text)
        text = re.sub(r'(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})', r'<code>\1</code>', text)
        
        # 增强ID显示
        text = re.sub(r'(SMS ID: )([^<\n]+)', r'\1<code>\2</code>', text)
        text = re.sub(r'(设备: )([^<\n]+)', r'\1<code>\2</code>', text)
        text = re.sub(r'(参考ID: )([^<\n]+)', r'\1<code>\2</code>', text)
        text = re.sub(r'(状态码: )([^<\n]+)', r'\1<code>\2</code>', text)
        
        # 增强电话号码显示
        text = re.sub(r'(接收方: )([^<\n]+)', r'\1<code>\2</code>', text)
        text = re.sub(r'(发送方: )([^<\n]+)', r'\1<code>\2</code>', text)
        text = re.sub(r'(手机号: )([^<\n]+)', r'\1<code>\2</code>', text)
        
        # 为重要状态添加斜体（使用HTML格式）
        text = re.sub(r'(状态: )(Free|Busy|Ringing|Offline|Unknown)', r'\1<i>\2</i>', text)
        text = re.sub(r'(状态: )(成功|失败|进行中|已确认|等待中)', r'\1<i>\2</i>', text)
        
        # 为错误信息添加特殊标记
        text = re.sub(r'(❌ [^<\n]+)(?![<])', r'<s>\1</s>', text)
        
        return text
    
    def format_silent_sms_message(self, device_display, sender_number, timestamp, content, reason, analysis=None):
        """格式化Silent SMS检测消息"""
        content_repr = self.escape_html(repr(content))
        reason_text = self.escape_html(reason)
        message = f"""
🔇 <b>SILENT SMS 检测</b>

📱 设备: <code>{self.escape_html(device_display)}</code>
📞 发送方: <code>{self.escape_html(sender_number)}</code>
⏰ 时间: <code>{self.escape_html(timestamp)}</code>
🔍 类型: <b>Type 0 / 空内容可疑短信</b>
📝 原始内容: <code>{content_repr}</code>
📊 内容长度: <code>{len(content)} 字符</code>
🎯 检测原因: <code>{reason_text}</code>"""

        # 添加分析详情
        if analysis:
            message += f"""
📈 <b>上下文分析</b>:"""
            if analysis.get('tp_pid_found'):
                message += f"\n· TP-PID标识: <b>已检测到</b>"
            if analysis.get('related_sms_count', 0) > 0:
                message += f"\n· 相关SMS数量: <code>{analysis['related_sms_count']}</code>"
            if analysis.get('sender_history'):
                message += f"\n· 发送者历史: <code>{len(analysis['sender_history'])}条记录</code>"
            if analysis.get('device_activity'):
                message += f"\n· 设备活动: <code>{len(analysis['device_activity'])}条记录</code>"

        message += f"""

⚠️ <b>注意</b>: 仅在TP-PID为Type 0或内容确实不可见时标记，普通TP-PID notice不再误判"""
        
        return message
    
    def format_original_sms_message(self, sms_info: dict) -> str:
        """普通来信正文优先展示；代码块仅保留给发送/回复编辑流程。"""
        device_id = sms_info.get('device', 'Unknown')
        device_display = sms_info.get('device_display') or device_id
        sender = sms_info.get('sender', 'Unknown')
        content = sms_info.get('content', '')
        timestamp = sms_info.get('timestamp', '')
        body = "<i>(空内容)</i>" if content is None or content == "" else self.escape_html(content)

        # Keep the body between the short heading and metadata so HTML parsing
        # does not strip whitespace belonging to the original SMS.
        return (
            "📩 <b>新短信</b>\n\n"
            f"{body}\n\n"
            f"设备 <code>{self.escape_html(device_display)}</code>\n"
            f"来自 <code>{self.escape_html(sender)}</code>\n"
            f"时间 <code>{self.escape_html(timestamp)}</code>"
        )
    
    def command_help_lines(self):
        return "\n".join(
            f"<code>/{name}</code> - {description}"
            for name, description in self.COMMANDS
        )

    def format_help_message(self):
        return ("🤖 <b>Asterisk Telegram Bot 帮助</b>\n\n"
                + self.command_help_lines()
                + "\n\n收到短信后可使用回复按钮。管理和设备操作仍需授权。")

    async def sync_bot_commands(self):
        """Keep default/private menus consistent without altering user authorization."""
        from telethon import functions, types
        commands = [types.BotCommand(name, description) for name, description in self.COMMANDS]
        for scope in (types.BotCommandScopeDefault(), types.BotCommandScopeUsers()):
            await asyncio.wait_for(self.client(functions.bots.SetBotCommandsRequest(
                scope=scope, lang_code='', commands=commands)), timeout=10)
        print("✅ Telegram默认和私聊命令菜单已同步")

    def test_message_formatting(self, text: str) -> str:
        """测试消息格式化（用于调试）"""
        print(f"原始文本: {repr(text)}")
        has_markdown = any(marker in text for marker in ['**', '*', '__', '_', '`', '```'])
        print(f"包含Markdown: {has_markdown}")
        
        if has_markdown:
            cleaned_text = self.clean_markdown_text(text)
            print(f"清理后文本: {repr(cleaned_text)}")
            print(f"显示效果:")
            print(cleaned_text)
            return cleaned_text
        
        return text
    
    async def send_message(self, chat_id: int, text: str, parse_mode: str = 'html', buttons=None, link_preview: bool = True):
        """Return a confirmed message or None; never retry uncertain network delivery."""
        from telethon.errors import EntityBoundsInvalidError, EntitiesTooLongError
        try:
            return await self.client.send_message(
                entity=chat_id, message=text, parse_mode=parse_mode,
                buttons=buttons, link_preview=link_preview)
        except (EntityBoundsInvalidError, EntitiesTooLongError) as exc:
            # These local/explicit entity rejections precede successful delivery.
            # Only a formatting rejection can be retried with escaped text.
            if parse_mode:
                try:
                    return await self.client.send_message(
                        entity=chat_id, message=self.escape_html(text), parse_mode='html',
                        buttons=buttons, link_preview=link_preview)
                except Exception as fallback_error:
                    print(f"❌ Telegram投递未确认: {type(fallback_error).__name__}")
                    return None
            print(f"❌ Telegram投递未确认: {type(exc).__name__}")
        except Exception as exc:
            print(f"❌ Telegram投递未确认: {type(exc).__name__}；未自动重发")
        return None

    async def setup_event_handlers(self):
        """设置Telethon事件处理器"""
        @self.client.on(events.NewMessage)
        async def handle_new_message(event):
            """处理新消息事件"""
            try:
                message = event.message
                chat_id = message.chat_id
                user_id = message.sender_id
                text = message.text
                
                print("💬 收到Telegram消息")
                
                # 检查用户权限
                if not Config.is_authorized(user_id):
                    await self.send_message(chat_id, "❌ 您没有权限使用此机器人")
                    return
                
                # 检查用户状态
                if text and text.split()[0].split('@')[0] == '/sms_guard':
                    await self.handle_sms_guard_command(chat_id, text, user_id)
                elif user_id in self.user_states:
                    await self.handle_user_state_message(chat_id, user_id, text)
                else:
                    # 处理命令
                    await self.handle_message_command(chat_id, user_id, text)
                    
            except Exception as e:
                print(f"处理消息事件时出错: {str(e)}")
        
        @self.client.on(events.CallbackQuery)
        async def handle_callback_query(event):
            """处理回调查询（按钮点击）"""
            try:
                user_id = event.sender_id
                chat_id = event.chat_id
                data = event.data.decode('utf-8')
                
                print(f"🔘 收到按钮点击: {data} (用户: {user_id})")
                
                # 处理不同类型的回调（权限检查在handle_callback_action中统一处理）
                await self.handle_callback_action(event, data, user_id, chat_id)
                
            except Exception as e:
                print(f"处理回调查询时出错: {str(e)}")
                await event.answer("❌ 处理请求时出错")
    
    async def handle_message_command(self, chat_id: int, user_id: int, text: str):
        """处理消息命令"""
        try:
            if text.startswith('/start'):
                await self.handle_start_command(chat_id, user_id)
            elif text.startswith('/help'):
                await self.handle_help_command(chat_id, user_id)
            elif text and text.split()[0].split('@')[0] == '/sms_guard':
                await self.handle_sms_guard_command(chat_id, text, user_id)
            elif text.startswith('/send_sms'):
                await self.handle_send_sms_command(chat_id, text)
            elif text.startswith('/devices'):
                await self.handle_devices_command(chat_id)
            elif text.startswith('/device_settings'):
                await self.handle_device_settings_command(chat_id, text)
            elif text.startswith('/device_state'):
                await self.handle_device_state_command(chat_id, text)
            elif text.startswith('/device_statistics'):
                await self.handle_device_statistics_command(chat_id, text)
            elif text.startswith('/recover_numbers'):
                await self.handle_recover_numbers_command(chat_id, user_id)
            elif text.startswith('/uac_recover'):
                await self.handle_uac_recover_command(chat_id, user_id)
            elif text.startswith('/add_user'):
                await self.handle_add_user_command(chat_id, text, user_id)
            elif text.startswith('/remove_user'):
                await self.handle_remove_user_command(chat_id, text, user_id)
            elif text.startswith('/sms_notifications'):
                await self.handle_sms_notifications_command(chat_id, text, user_id)
            elif text.startswith('/test_sms_log'):
                await self.handle_test_sms_log_command(chat_id, text, user_id)
            elif text.startswith('/check_sms_logs'):
                await self.handle_check_sms_logs_command(chat_id, text, user_id)
            elif text.startswith('/pending_sms'):
                await self.handle_pending_sms_command(chat_id, text, user_id)
            else:
                await self.send_message(chat_id, "❌ 未知命令，请使用 /help 查看可用命令")
                
        except Exception as e:
            print(f"处理命令时出错: {str(e)}")
    
    async def handle_user_state_message(self, chat_id: int, user_id: int, text: str):
        """处理用户状态消息"""
        try:
            user_state = self.user_states.get(user_id)
            if not user_state:
                await self.handle_message_command(chat_id, user_id, text)
                return
            
            action = user_state.get('action')
            
            if action == 'reply_sms':
                await self.handle_reply_sms_content(chat_id, user_id, text, user_state)
            elif action == 'send_sms':
                await self.handle_send_sms_content(chat_id, user_id, text, user_state)
            elif action == 'send_sms_phone':
                await self.handle_send_sms_phone(chat_id, user_id, text, user_state)
            else:
                # 未知状态，清除并处理为普通命令
                del self.user_states[user_id]
                await self.handle_message_command(chat_id, user_id, text)
                
        except Exception as e:
            print(f"处理用户状态消息时出错: {str(e)}")
            # 清除错误状态
            if user_id in self.user_states:
                del self.user_states[user_id]
            await self.send_message(chat_id, "❌ 处理消息时出错，已重置状态")
    
    async def handle_reply_sms_content(self, chat_id: int, user_id: int, content: str, user_state: dict):
        """处理回复SMS内容"""
        try:
            sms_info = user_state.get('sms_info')
            device_id = sms_info.get('device')
            sender = sms_info.get('sender')
            sms_id = user_state.get('sms_id')
            
            # 获取原始消息信息用于恢复
            original_message = user_state.get('original_message')
            original_buttons = user_state.get('original_buttons')
            
            # 发送SMS（静默模式，避免重复消息）
            success = await self.send_sms(chat_id, device_id, sender, content, silent=True)
            
            if success:
                await self.send_message(
                    chat_id,
                    f"📤 <b>回复已提交</b>\n\n"
                    f"接收方 <code>{self.escape_html(sender)}</code>\n"
                    f"设备 <code>{self.escape_html(device_id)}</code>\n"
                    f"状态 <code>等待模块回执</code>\n\n"
                    f"{self.format_sms_content_block(content)}"
                )
            else:
                # 发送失败消息
                await self.send_message(chat_id, f"❌ 回复发送失败到 <code>{self.escape_html(sender)}</code>")
            
            # 恢复原始消息
            if original_message and sms_id in self.pending_replies:
                try:
                    # 查找原始消息并恢复
                    original_sms_info = self.pending_replies[sms_id]
                    restored_message = self.format_original_sms_message(original_sms_info)
                    restored_buttons = [
                        [Button.inline("💬 回复", f"reply_sms:{sms_id}")]
                    ]
                    
                    # 尝试编辑消息恢复原始内容
                    await self.send_message(chat_id, restored_message, buttons=restored_buttons, link_preview=False)
                    
                except Exception as restore_error:
                    print(f"恢复原始消息时出错: {str(restore_error)}")
            
            # 清除用户状态
            del self.user_states[user_id]
            
            # 清理待回复记录
            if sms_id in self.pending_replies:
                del self.pending_replies[sms_id]
            
        except Exception as e:
            print(f"处理回复SMS内容时出错: {str(e)}")
            await self.send_message(chat_id, f"❌ 发送回复失败: {str(e)}")
    
    async def handle_send_sms_content(self, chat_id: int, user_id: int, content: str, user_state: dict):
        """处理发送SMS内容"""
        try:
            device_id = user_state.get('device_id')
            flow_id = user_state.get('flow_id') or self.create_flow_id()
            
            # 保存SMS内容到用户状态，等待用户输入手机号
            self.user_states[user_id] = {
                'action': 'send_sms_phone',
                'device_id': device_id,
                'sms_content': content,
                'flow_id': flow_id
            }
            
            # 询问用户输入手机号
            await self.send_message(
                chat_id,
                f"📝 <b>短信内容</b>\n\n"
                f"{self.format_sms_content_block(content)}\n\n"
                f"📱 请输入接收手机号（例如 <code>+8613800138000</code>）：",
                buttons=[[self.cancel_button(flow_id)]]
            )
            
        except Exception as e:
            print(f"处理发送SMS内容时出错: {str(e)}")
            await self.send_message(chat_id, f"❌ 处理发送SMS时出错: {str(e)}")
    
    async def handle_send_sms_phone(self, chat_id: int, user_id: int, phone_number: str, user_state: dict):
        """处理发送SMS手机号输入"""
        try:
            device_id = user_state.get('device_id')
            sms_content = user_state.get('sms_content')
            
            # 验证手机号格式
            if not self.validate_phone_number(phone_number):
                flow_id = user_state.get('flow_id')
                await self.send_message(
                    chat_id,
                    "❌ 手机号格式不正确，请重新输入（例如 <code>+8613800138000</code>）：",
                    buttons=[[self.cancel_button(flow_id)]]
                )
                return
            
            # 清除用户状态
            del self.user_states[user_id]
            
            # 发送SMS（非静默模式，显示设备信息）
            await self.send_sms(chat_id, device_id, phone_number, sms_content, silent=False)
            
        except Exception as e:
            print(f"处理发送SMS手机号时出错: {str(e)}")
            await self.send_message(chat_id, f"❌ 处理手机号时出错: {str(e)}")
    
    async def handle_callback_action(self, event, data: str, user_id: int, chat_id: int):
        """处理回调查询动作"""
        try:
            # 统一权限检查
            if not Config.is_authorized(user_id):
                await event.answer("❌ 您没有权限使用此功能")
                return
            
            # 解析回调数据
            parts = data.split(':')
            action = parts[0]
            
            if action == 'reply_sms':
                # 回复SMS
                sms_id = parts[1] if len(parts) > 1 else None
                await self.handle_reply_sms_callback(event, sms_id, user_id, chat_id)
                
            elif action == 'select_device':
                # 选择设备
                device_id = parts[1] if len(parts) > 1 else None
                await self.handle_select_device_callback(event, device_id, user_id, chat_id)
                
            elif action == 'send_sms_device':
                # 发送SMS选择设备
                device_id = parts[1] if len(parts) > 1 else None
                await self.handle_send_sms_device_callback(event, device_id, user_id, chat_id)
                
            elif action == 'device_settings':
                # 设备设置选择
                device_id = parts[1] if len(parts) > 1 else None
                await self.handle_device_settings_callback(event, device_id, user_id, chat_id)
                
            elif action == 'device_state':
                # 设备状态选择
                device_id = parts[1] if len(parts) > 1 else None
                await self.handle_device_state_callback(event, device_id, user_id, chat_id)
                
            elif action == 'device_statistics':
                # 设备统计选择
                device_id = parts[1] if len(parts) > 1 else None
                await self.handle_device_statistics_callback(event, device_id, user_id, chat_id)

            elif action == 'uac' and len(parts) == 4:
                await self.handle_uac_callback(event, parts[1], parts[2], parts[3], user_id, chat_id)
                
            elif action == 'cancel':
                # 取消操作
                flow_id = parts[1] if len(parts) > 1 else None
                await self.handle_cancel_callback(event, user_id, chat_id, flow_id)
                
            elif action == 'cancel_reply':
                # 取消回复
                flow_id = parts[1] if len(parts) > 1 else None
                await self.handle_cancel_reply_callback(event, user_id, chat_id, flow_id)
                
            else:
                await event.answer("❌ 未知的操作")
                
        except Exception as e:
            print(f"处理回调动作时出错: {str(e)}")
            await event.answer("❌ 处理操作时出错")
    
    async def handle_reply_sms_callback(self, event, sms_id: str, user_id: int, chat_id: int):
        """处理回复SMS回调"""
        try:
            if sms_id and sms_id in self.pending_replies:
                sms_info = self.pending_replies[sms_id]
                
                # 保存原始消息信息
                original_message = self.format_original_sms_message(sms_info)
                original_buttons = [
                    [Button.inline("💬 回复", f"reply_sms:{sms_id}")]
                ]
                flow_id = self.create_flow_id()
                
                # 设置用户状态为等待回复内容
                self.user_states[user_id] = {
                    'action': 'reply_sms',
                    'sms_info': sms_info,
                    'sms_id': sms_id,
                    'flow_id': flow_id,
                    'original_message': original_message,
                    'original_buttons': original_buttons
                }
                
                # 显示原始短信内容
                original_content = sms_info.get('content', '(无内容)')
                original_content_html = self.format_sms_content_block(original_content)
                
                await event.edit(
                    f"📱 <b>准备回复SMS</b>\n\n"
                    f"发送方 <code>{self.escape_html(sms_info['sender'])}</code>\n"
                    f"设备 <code>{self.escape_html(sms_info['device'])}</code>\n"
                    f"时间 <code>{self.escape_html(sms_info['timestamp'])}</code>\n\n"
                    f"{original_content_html}\n\n"
                    f"✏️ 请输入要回复的内容:",
                    parse_mode='html',
                    buttons=[
                        [self.cancel_reply_button(flow_id)]
                    ]
                )
                await event.answer("请发送要回复的内容")
            else:
                await event.answer("❌ SMS信息已过期")
                
        except Exception as e:
            print(f"处理回复SMS回调时出错: {str(e)}")
            await event.answer("❌ 处理回复时出错")
    
    async def handle_select_device_callback(self, event, device_id: str, user_id: int, chat_id: int):
        """处理选择设备回调"""
        try:
            if device_id:
                # 设置用户状态
                self.user_states[user_id] = {
                    'action': 'view_device',
                    'device_id': device_id
                }
                
                # 查询设备详细信息
                await self.query_device_state(chat_id, device_id)
                await event.answer(f"✅ 已选择设备: {device_id}")
            else:
                await event.answer("❌ 设备ID无效")
                
        except Exception as e:
            print(f"处理选择设备回调时出错: {str(e)}")
            await event.answer("❌ 处理设备选择时出错")
    
    async def handle_send_sms_device_callback(self, event, device_id: str, user_id: int, chat_id: int):
        """处理发送SMS设备选择回调"""
        try:
            if device_id:
                flow_id = self.create_flow_id()
                # 设置用户状态为等待SMS内容
                self.user_states[user_id] = {
                    'action': 'send_sms',
                    'device_id': device_id,
                    'flow_id': flow_id
                }
                
                # 获取设备信息
                devices = await self.get_device_list()
                device_info = next((d for d in devices if d['id'] == device_id), None)
                
                if device_info:
                    await event.edit(
                        f"📱 <b>发送短信</b>\n\n"
                        f"设备 <code>{self.escape_html(device_id)}</code>\n"
                        f"号码 <code>{self.escape_html(device_info.get('number', '未知'))}</code>\n"
                        f"状态 <code>{self.escape_html(device_info.get('state', '未知'))}</code>\n\n"
                        f"💬 请输入要发送的SMS内容:",
                        parse_mode='html',
                        buttons=[[self.cancel_button(flow_id)]]
                    )
                    await event.answer(f"✅ 已选择设备: {device_id}")
                else:
                    await event.answer("❌ 设备不存在")
            else:
                await event.answer("❌ 设备ID无效")
                
        except Exception as e:
            print(f"处理发送SMS设备选择回调时出错: {str(e)}")
            await event.answer("❌ 处理设备选择时出错")
    
    async def handle_device_settings_callback(self, event, device_id: str, user_id: int, chat_id: int):
        """处理设备设置选择回调"""
        try:
            if device_id:
                await event.answer(f"正在查询设备 {device_id} 的设置...")
                await self.query_device_settings(chat_id, device_id)
            else:
                await event.answer("❌ 无效的设备ID")
                
        except Exception as e:
            print(f"处理设备设置选择回调时出错: {str(e)}")
            await event.answer("❌ 处理设备设置时出错")
    
    async def handle_device_state_callback(self, event, device_id: str, user_id: int, chat_id: int):
        """处理设备状态选择回调"""
        try:
            if device_id:
                await event.answer(f"正在查询设备 {device_id} 的状态...")
                await self.query_device_state(chat_id, device_id)
            else:
                await event.answer("❌ 无效的设备ID")
                
        except Exception as e:
            print(f"处理设备状态选择回调时出错: {str(e)}")
            await event.answer("❌ 处理设备状态时出错")
    
    async def handle_device_statistics_callback(self, event, device_id: str, user_id: int, chat_id: int):
        """处理设备统计选择回调"""
        try:
            if device_id:
                await event.answer(f"正在查询设备 {device_id} 的统计...")
                await self.query_device_statistics(chat_id, device_id)
            else:
                await event.answer("❌ 无效的设备ID")
                
        except Exception as e:
            print(f"处理设备统计选择回调时出错: {str(e)}")
            await event.answer("❌ 处理设备统计时出错")
    
    async def handle_cancel_callback(self, event, user_id: int, chat_id: int, flow_id: str = None):
        """处理取消操作回调"""
        try:
            if not self.is_active_flow(user_id, flow_id):
                await event.edit("ℹ️ 这条操作已过期", parse_mode='html', buttons=None)
                await event.answer("当前已有新的操作流程")
                return

            # 清除用户状态
            if user_id in self.user_states:
                del self.user_states[user_id]
            
            await event.edit("❌ 操作已取消", parse_mode='html', buttons=None)
            await event.answer("✅ 已取消操作")
            
        except Exception as e:
            print(f"处理取消回调时出错: {str(e)}")
            await event.answer("❌ 取消操作时出错")
    
    async def handle_cancel_reply_callback(self, event, user_id: int, chat_id: int, flow_id: str = None):
        """处理取消回复回调"""
        try:
            if not self.is_active_flow(user_id, flow_id):
                await event.edit("ℹ️ 这条回复操作已过期", parse_mode='html', buttons=None)
                await event.answer("当前已有新的操作流程")
                return

            # 清除用户状态
            if user_id in self.user_states:
                user_state = self.user_states[user_id]
                sms_id = user_state.get('sms_id')
                
                # 恢复原始消息
                if sms_id and sms_id in self.pending_replies:
                    original_sms_info = self.pending_replies[sms_id]
                    restored_message = self.format_original_sms_message(original_sms_info)
                    restored_buttons = [
                        [Button.inline("💬 回复", f"reply_sms:{sms_id}")]
                    ]
                    
                    await event.edit(restored_message, parse_mode='html', buttons=restored_buttons, link_preview=False)
                else:
                    await event.edit("❌ 回复已取消", parse_mode='html', buttons=None)
                
                del self.user_states[user_id]
            
            await event.answer("✅ 已取消回复")
            
        except Exception as e:
            print(f"处理取消回复回调时出错: {str(e)}")
            await event.answer("❌ 取消回复时出错")

    async def get_live_uac_devices(self) -> List[str]:
        """Return only current, syntactically safe chan_quectel device names."""
        devices = await self.get_device_list(allow_recovery=False)
        return [device['id'] for device in devices if self.is_valid_quectel_device(device.get('id'))]

    async def handle_uac_recover_command(self, chat_id: int, user_id: int):
        devices = await self.get_live_uac_devices()
        if not devices:
            await self.send_message(chat_id, "❌ 未发现可用的 Quectel 设备，未执行任何恢复操作。")
            return
        flow_id = self.create_flow_id()
        self.user_states[user_id] = {'action': 'uac_select', 'flow_id': flow_id, 'devices': devices}
        buttons = [[Button.inline(f"🎧 {device}", f"uac:select:{flow_id}:{device}")] for device in devices]
        buttons.append([self.cancel_button(flow_id)])
        await self.send_message(
            chat_id,
            "🎧 <b>UAC 音频恢复</b>\n\n请选择实时检测到的设备。软恢复会等待通话结束；硬重置会让对应 SIM 暂时离线。",
            buttons=buttons
        )

    async def handle_uac_callback(self, event, operation: str, flow_id: str, device_id: str, user_id: int, chat_id: int):
        state = self.user_states.get(user_id, {})
        if state.get('flow_id') != flow_id or device_id not in state.get('devices', []) or not self.is_valid_quectel_device(device_id):
            await event.answer("❌ 操作已过期或设备无效")
            return
        # Re-check the device against the current module list before any CLI action.
        if device_id not in await self.get_live_uac_devices():
            self.user_states.pop(user_id, None)
            await event.edit("❌ 设备已不在实时列表中，未执行恢复。", parse_mode='html', buttons=None)
            await event.answer("设备已过期")
            return
        if operation == 'select' and state.get('action') == 'uac_select':
            state['action'] = 'uac_action'
            await event.edit(
                f"🎧 <b>{device_id} UAC 恢复</b>\n\n"
                "软恢复：执行 <code>quectel restart when convenient</code>，等待现有通话结束。\n\n"
                "硬重置 UAC：将执行 <code>quectel uac apply</code>，它会发送 <code>AT+QPCMV=0</code> 和 <code>AT+CFUN=1,1</code>，对应 SIM 会暂时离线。",
                parse_mode='html',
                buttons=[
                    [Button.inline("🩹 软恢复", f"uac:soft:{flow_id}:{device_id}")],
                    [Button.inline("⚠️ 硬重置 UAC", f"uac:hard:{flow_id}:{device_id}")],
                    [self.cancel_button(flow_id)]
                ]
            )
            await event.answer("请选择恢复方式")
            return
        if operation == 'hard' and state.get('action') == 'uac_action':
            state['action'] = 'uac_hard_confirm'
            await event.edit(
                f"⚠️ <b>确认硬重置 {device_id}</b>\n\n这会发送 <code>AT+QPCMV=0</code> 和 <code>AT+CFUN=1,1</code>；对应 SIM 将暂时离线。",
                parse_mode='html',
                buttons=[[Button.inline("确认硬重置 UAC", f"uac:apply:{flow_id}:{device_id}")], [self.cancel_button(flow_id)]]
            )
            await event.answer("请二次确认")
            return
        expected_action = {'soft': 'uac_action', 'apply': 'uac_hard_confirm'}.get(operation)
        if expected_action != state.get('action'):
            await event.answer("❌ 操作已使用或已过期")
            return
        # Consume the token before awaiting; duplicate taps cannot start another recovery.
        self.user_states.pop(user_id, None)
        hard_reset = operation == 'apply'
        await event.edit("⏳ 已提交恢复请求，正在等待/检查设备状态…", parse_mode='html', buttons=None)
        await event.answer("恢复已提交")
        result = await self.recover_uac(device_id, source='manual', hard_reset=hard_reset)
        await self.send_message(chat_id, self.format_uac_recovery_result(device_id, result, hard_reset))

    def format_uac_recovery_result(self, device_id: str, result: dict, hard_reset: bool) -> str:
        status = '✅ 已恢复' if result.get('healthy') else ('🧪 演练完成' if result.get('dry_run') else '⚠️ 未在 120 秒内恢复')
        action = '硬重置 UAC' if hard_reset else '软恢复'
        return (f"{status} <b>{action}</b>\n\n设备 <code>{device_id}</code>\n"
                f"命令 <code>{self.escape_html(result.get('command', ''))}</code>\n"
                f"结果 <code>{self.escape_html(result.get('detail', ''))}</code>")
    
    async def handle_start_command(self, chat_id: int, user_id: int):
        """处理/start命令"""
        # 检查用户权限
        if not Config.is_authorized(user_id):
            welcome_msg = """
🤖 Asterisk Telegram Bot

欢迎！您已连接到Asterisk Telegram Bot。

请联系管理员获取使用权限。

管理员可以使用以下命令：
/add_user <用户ID> - 添加授权用户
            """
            await self.send_message(chat_id, welcome_msg)
            return
        
        welcome_msg = ("🤖 <b>Asterisk Telegram Bot</b>\n\n欢迎使用！\n\n"
                       + self.command_help_lines()
                       + "\n\n收到短信后可点击回复按钮。")
        await self.send_message(chat_id, welcome_msg)

    async def handle_help_command(self, chat_id: int, user_id: int):
        """处理/help命令"""
        # 检查用户权限
        if not Config.is_authorized(user_id):
            await self.send_message(chat_id, "❌ 您没有权限使用此功能")
            return
        
        # 使用增强的格式化帮助信息
        help_msg = self.format_help_message()
        await self.send_message(chat_id, help_msg)
    
    async def handle_send_sms_command(self, chat_id: int, text: str):
        """处理/send_sms命令"""
        parts = text.split(' ', 3)
        
        if len(parts) < 4:
            # 显示设备选择界面
            await self.show_send_sms_device_selection(chat_id)
            return
        
        device_id = parts[1]
        phone_number = parts[2]
        sms_content = parts[3]
        
        if not self.validate_phone_number(phone_number):
            await self.send_message(chat_id, "❌ 手机号格式不正确")
            return
        
        await self.send_sms(chat_id, device_id, phone_number, sms_content, silent=False)
    
    async def show_send_sms_device_selection(self, chat_id: int):
        """显示发送SMS设备选择界面"""
        try:
            devices = await self.get_device_list()
            if not devices:
                await self.send_message(chat_id, "❌ 没有找到可用设备")
                return
            
            message = "📱 <b>选择要发送SMS的设备:</b>\n\n"
            buttons = []
            
            # 每行最多2个按钮
            for i, device in enumerate(devices):
                device_name = device.get('id', 'unknown')
                device_number = device.get('number', '未知')
                device_state = device.get('state', '未知')
                
                message += f"📱 <b>{device_name}</b>\n"
                message += f"   📞 手机号: <code>{device_number}</code>\n"
                message += f"   📶 状态: <i>{device_state}</i>\n\n"
                
                # 创建按钮行
                if i % 2 == 0:
                    buttons.append([Button.inline(f"📱 {device_name}", f"send_sms_device:{device_name}")])
                else:
                    buttons[-1].append(Button.inline(f"📱 {device_name}", f"send_sms_device:{device_name}"))
            
            # 添加取消按钮
            buttons.append([Button.inline("❌ 取消", "cancel")])
            
            await self.send_message(chat_id, message, buttons=buttons)
            
        except Exception as e:
            print(f"显示发送SMS设备选择界面时出错: {str(e)}")
            await self.send_message(chat_id, f"❌ 获取设备列表时出错: {str(e)}")
    
    async def handle_devices_command(self, chat_id: int):
        """处理/devices命令"""
        await self.show_device_selection(chat_id)
    
    async def show_device_selection(self, chat_id: int):
        """显示设备选择界面"""
        try:
            devices = await self.get_device_list()
            if not devices:
                await self.send_message(chat_id, "❌ 没有找到可用设备")
                return
            
            message = "📱 <b>选择要查看状态的设备:</b>\n\n"
            buttons = []
            
            # 每行最多2个按钮
            for i, device in enumerate(devices):
                device_name = device.get('id', 'unknown')
                device_number = device.get('number', '未知')
                device_state = device.get('state', '未知')
                
                message += f"📱 <b>{device_name}</b>\n"
                message += f"   📞 手机号: <code>{device_number}</code>\n"
                message += f"   📶 状态: <i>{device_state}</i>\n\n"
                
                # 创建按钮行
                if i % 2 == 0:
                    buttons.append([Button.inline(f"📱 {device_name}", f"select_device:{device_name}")])
                else:
                    buttons[-1].append(Button.inline(f"📱 {device_name}", f"select_device:{device_name}"))
            
            # 添加取消按钮
            buttons.append([Button.inline("❌ 取消", "cancel")])
            
            await self.send_message(chat_id, message, buttons=buttons)
            
        except Exception as e:
            print(f"显示设备选择界面时出错: {str(e)}")
            await self.send_message(chat_id, f"❌ 获取设备列表时出错: {str(e)}")
    
    async def handle_device_settings_command(self, chat_id: int, text: str):
        """处理/device_settings命令"""
        parts = text.split()
        
        if len(parts) < 2:
            # 显示设备选择界面
            await self.show_device_settings_selection(chat_id)
            return
        
        device_id = parts[1]
        await self.query_device_settings(chat_id, device_id)
    
    async def show_device_settings_selection(self, chat_id: int):
        """显示设备设置选择界面"""
        try:
            devices = await self.get_device_list()
            if not devices:
                await self.send_message(chat_id, "❌ 没有找到可用设备")
                return
            
            message = "⚙️ <b>选择要查询设置的设备：</b>\n\n"
            buttons = []
            
            for i, device in enumerate(devices):
                device_name = device.get('id', 'Unknown')
                device_number = device.get('number', 'N/A')
                device_state = device.get('state', 'Unknown')
                
                message += f"📱 <b>{device_name}</b>\n"
                message += f"   📞 手机号: <code>{device_number}</code>\n"
                message += f"   📶 状态: <i>{device_state}</i>\n\n"
                
                # 创建按钮行
                if i % 2 == 0:
                    buttons.append([Button.inline(f"⚙️ {device_name}", f"device_settings:{device_name}")])
                else:
                    buttons[-1].append(Button.inline(f"⚙️ {device_name}", f"device_settings:{device_name}"))
            
            # 添加取消按钮
            buttons.append([Button.inline("❌ 取消", "cancel")])
            
            await self.send_message(chat_id, message, buttons=buttons)
            
        except Exception as e:
            print(f"显示设备设置选择界面时出错: {str(e)}")
            await self.send_message(chat_id, f"❌ 获取设备列表时出错: {str(e)}")
    
    async def show_device_state_selection(self, chat_id: int):
        """显示设备状态选择界面"""
        try:
            devices = await self.get_device_list()
            if not devices:
                await self.send_message(chat_id, "❌ 没有找到可用设备")
                return
            
            message = "📊 <b>选择要查看状态的设备:</b>\n\n"
            buttons = []
            
            for i, device in enumerate(devices):
                device_name = device['id']
                device_info = device.get('number', 'Unknown')
                
                # 创建按钮行
                if i % 2 == 0:
                    buttons.append([Button.inline(f"📊 {device_name}", f"device_state:{device_name}")])
                else:
                    buttons[-1].append(Button.inline(f"📊 {device_name}", f"device_state:{device_name}"))
            
            # 添加取消按钮
            buttons.append([Button.inline("❌ 取消", "cancel")])
            
            await self.send_message(chat_id, message, buttons=buttons)
            
        except Exception as e:
            print(f"显示设备状态选择界面时出错: {str(e)}")
            await self.send_message(chat_id, f"❌ 获取设备列表时出错: {str(e)}")
    
    async def show_device_statistics_selection(self, chat_id: int):
        """显示设备统计选择界面"""
        try:
            devices = await self.get_device_list()
            if not devices:
                await self.send_message(chat_id, "❌ 没有找到可用设备")
                return
            
            message = "📈 <b>选择要查看统计的设备:</b>\n\n"
            buttons = []
            
            for i, device in enumerate(devices):
                device_name = device['id']
                device_info = device.get('number', 'Unknown')
                
                # 创建按钮行
                if i % 2 == 0:
                    buttons.append([Button.inline(f"📈 {device_name}", f"device_statistics:{device_name}")])
                else:
                    buttons[-1].append(Button.inline(f"📈 {device_name}", f"device_statistics:{device_name}"))
            
            # 添加取消按钮
            buttons.append([Button.inline("❌ 取消", "cancel")])
            
            await self.send_message(chat_id, message, buttons=buttons)
            
        except Exception as e:
            print(f"显示设备统计选择界面时出错: {str(e)}")
            await self.send_message(chat_id, f"❌ 获取设备列表时出错: {str(e)}")
    
    async def handle_device_state_command(self, chat_id: int, text: str):
        """处理/device_state命令"""
        parts = text.split()
        
        if len(parts) < 2:
            # 显示设备选择界面
            await self.show_device_state_selection(chat_id)
            return
        
        device_id = parts[1]
        await self.query_device_state(chat_id, device_id)
    
    async def handle_device_statistics_command(self, chat_id: int, text: str):
        """处理/device_statistics命令"""
        parts = text.split()
        
        if len(parts) < 2:
            # 显示设备选择界面
            await self.show_device_statistics_selection(chat_id)
            return
        
        device_id = parts[1]
        await self.query_device_statistics(chat_id, device_id)

    async def handle_recover_numbers_command(self, chat_id: int, user_id: int):
        """处理/recover_numbers命令"""
        try:
            if not Config.is_authorized(user_id):
                await self.send_message(chat_id, "❌ 您没有权限使用此命令")
                return

            await self.send_message(chat_id, "🔧 正在检查并恢复Quectel本机号码...")
            unresolved = await self.recover_missing_phone_numbers(notify=False)
            devices = await self.get_device_list(allow_recovery=False)

            lines = ["📱 <b>Quectel本机号码检查结果</b>", ""]
            for device in devices:
                number = device.get('number') or '未获取'
                provider = device.get('provider') or 'UNKNOWN'
                lines.append(
                    f"· <code>{device['id']}</code> provider=<code>{provider}</code> number=<code>{number}</code>"
                )

            if unresolved:
                lines.extend([
                    "",
                    "⚠️ 仍未获取号码的设备:",
                    *[f"· <code>{device_id}</code>" for device_id in unresolved],
                    "",
                    f"已执行 <code>{Config.PHONEBOOK_PREF_COMMAND}</code> 和 <code>AT+CNUM</code>。"
                ])
            else:
                lines.append("\n✅ 没有发现仍缺少本机号码的已注册设备")

            await self.send_message(chat_id, "\n".join(lines))

        except Exception as e:
            print(f"处理恢复号码命令时出错: {str(e)}")
            await self.send_message(chat_id, f"❌ 恢复号码时出错: {str(e)}")
    
    async def handle_add_user_command(self, chat_id: int, text: str, current_user_id: int):
        """处理/add_user命令"""
        parts = text.split(' ', 1)
        
        if len(parts) < 2:
            await self.send_message(chat_id, "❌ 使用方法：/add_user <用户ID>")
            return
        
        try:
            user_id = int(parts[1])
            Config.add_user(user_id)
            await self.send_message(chat_id, f"✅ 用户 {user_id} 已添加到授权列表")
        except ValueError:
            await self.send_message(chat_id, "❌ 用户ID必须是数字")
    
    async def handle_remove_user_command(self, chat_id: int, text: str, current_user_id: int):
        """处理/remove_user命令"""
        parts = text.split(' ', 1)
        
        if len(parts) < 2:
            await self.send_message(chat_id, "❌ 使用方法：/remove_user <用户ID>")
            return
        
        try:
            user_id = int(parts[1])
            if user_id == current_user_id:
                await self.send_message(chat_id, "❌ 不能移除自己的权限")
                return
            
            Config.remove_user(user_id)
            await self.send_message(chat_id, f"✅ 用户 {user_id} 已从授权列表移除")
        except ValueError:
            await self.send_message(chat_id, "❌ 用户ID必须是数字")
    
    async def handle_sms_notifications_command(self, chat_id: int, text: str, user_id: int):
        """处理SMS通知开关命令"""
        try:
            if not Config.is_authorized(user_id):
                await self.send_message(chat_id, "❌ 您没有权限使用此命令")
                return
            
            parts = text.split()
            if len(parts) < 2:
                # 显示当前状态
                status = "开启" if self.sms_notifications else "关闭"
                message = f"📱 <b>SMS发送通知状态</b>\n\n"
                message += f"当前状态: <code>{status}</code>\n\n"
                message += f"使用方法:\n"
                message += f"<code>/sms_notifications on</code> - 开启SMS发送通知\n"
                message += f"<code>/sms_notifications off</code> - 关闭SMS发送通知\n\n"
                message += f"💡 开启后会收到SMS发送成功/失败的通知"
                await self.send_message(chat_id, message)
                return
            
            action = parts[1].lower()
            if action == "on":
                self.sms_notifications = True
                await self.send_message(chat_id, "✅ SMS发送通知已开启")
                print(f"📱 SMS通知已开启 (用户: {user_id})")
            elif action == "off":
                self.sms_notifications = False
                await self.send_message(chat_id, "🔇 SMS发送通知已关闭")
                print(f"📱 SMS通知已关闭 (用户: {user_id})")
            else:
                await self.send_message(chat_id, "❌ 无效参数，请使用 <code>on</code> 或 <code>off</code>")
                
        except Exception as e:
            print(f"处理SMS通知命令时出错: {str(e)}")
            await self.send_message(chat_id, f"❌ 处理命令时出错: {str(e)}")
    
    async def handle_test_sms_log_command(self, chat_id: int, text: str, user_id: int):
        """处理SMS日志测试命令"""
        try:
            if not Config.is_authorized(user_id):
                await self.send_message(chat_id, "❌ 您没有权限使用此命令")
                return
            
            # 从命令中提取要测试的日志行
            parts = text.split(' ', 1)
            if len(parts) < 2:
                await self.send_message(chat_id, "❌ 请提供要测试的日志行\n\n使用方法: <code>/test_sms_log [日志行内容]</code>")
                return
            
            log_line = parts[1]
            result = self.parse_sms_send_result(log_line)
            
            if result:
                message = f"✅ <b>日志解析成功</b>\n\n"
                message += f"📝 原始日志: <code>{log_line}</code>\n\n"
                message += f"📊 解析结果:\n"
                message += f"• 设备: <code>{result['device']}</code>\n"
                message += f"• 消息类型: <code>{result['message_type']}</code>\n"
                message += f"• 成功状态: <code>{result['success']}</code>\n"
                if result['sms_id']:
                    message += f"• SMS ID: <code>{result['sms_id']}</code>\n"
                if result['ref_id']:
                    message += f"• 参考ID: <code>{result['ref_id']}</code>\n"
                if result['phone_number']:
                    message += f"• 电话号码: <code>{result['phone_number']}</code>\n"
                if result['status_code']:
                    message += f"• 状态码: <code>{result['status_code']}</code>\n"
                
                await self.send_message(chat_id, message)
            else:
                await self.send_message(chat_id, f"❌ <b>日志解析失败</b>\n\n📝 原始日志: <code>{log_line}</code>\n\n💡 该日志行不匹配任何SMS发送结果模式")
                
        except Exception as e:
            print(f"处理SMS日志测试命令时出错: {str(e)}")
            await self.send_message(chat_id, f"❌ 处理命令时出错: {str(e)}")
    
    async def handle_check_sms_logs_command(self, chat_id: int, text: str, user_id: int):
        """处理检查SMS日志命令"""
        try:
            if not Config.is_authorized(user_id):
                await self.send_message(chat_id, "❌ 您没有权限使用此命令")
                return
            
            # 检查最近的日志中是否有SMS相关消息
            await self.send_message(chat_id, "🔍 正在检查最近的SMS相关日志...")
            
            try:
                # 使用grep搜索最近的SMS相关日志
                cmd = f"tail -100 {self.log_file} | grep -E '(SMS|CMGS|SMSTEXT)' | tail -10"
                result = await self.execute_command(cmd)
                
                if result and result.strip():
                    message = "📋 <b>最近的SMS相关日志</b>\n\n"
                    lines = result.strip().split('\n')
                    for i, line in enumerate(lines[:10], 1):
                        message += f"{i}. <code>{line}</code>\n"
                    
                    # 测试这些日志是否能被解析
                    message += "\n🧪 <b>解析测试结果</b>\n"
                    parsed_count = 0
                    for line in lines:
                        if self.parse_sms_send_result(line):
                            parsed_count += 1
                    
                    message += f"✅ 可解析: {parsed_count}/{len(lines)} 条日志\n"
                    if parsed_count == 0:
                        message += "\n⚠️ 没有日志能被解析，可能需要调整正则表达式"
                    
                    await self.send_message(chat_id, message)
                else:
                    await self.send_message(chat_id, "❌ 没有找到SMS相关日志\n\n💡 可能原因:\n• 日志文件路径不正确\n• 最近没有SMS活动\n• grep命令执行失败")
                    
            except Exception as e:
                await self.send_message(chat_id, f"❌ 检查日志时出错: {str(e)}")
                
        except Exception as e:
            print(f"处理检查SMS日志命令时出错: {str(e)}")
            await self.send_message(chat_id, f"❌ 处理命令时出错: {str(e)}")
    
    async def handle_pending_sms_command(self, chat_id: int, text: str, user_id: int):
        """处理查看待确认SMS发送请求命令"""
        try:
            if not Config.is_authorized(user_id):
                await self.send_message(chat_id, "❌ 您没有权限使用此命令")
                return
            
            current_time = time.time()
            pending_count = 0
            
            message = "📋 <b>待确认的SMS发送请求</b>\n\n"
            
            for send_id, send_info in self.pending_sms_sends.items():
                if send_info['status'] == 'queued':
                    pending_count += 1
                    elapsed_time = int(current_time - send_info['timestamp'])
                    message += f"🆔 <b>{send_id}</b>\n"
                    message += f"📱 设备: <code>{send_info['device_id']}</code>\n"
                    message += f"📞 接收方: <code>{send_info['phone_number']}</code>\n"
                    message += f"💬 内容: <code>{send_info['content'][:50]}{'...' if len(send_info['content']) > 50 else ''}</code>\n"
                    message += f"⏰ 等待时间: <code>{elapsed_time}秒</code>\n\n"
            
            if pending_count == 0:
                message += "✅ 没有待确认的SMS发送请求"
            else:
                message += f"📊 总计: {pending_count} 个待确认请求"
            
            unconfirmed = [info for info in self.pending_replies.values()
                           if info.get('delivery_status', {}).get(user_id) == 'unconfirmed']
            if unconfirmed:
                message += f"\n\n⚠️ 当前有 {len(unconfirmed)} 条收到的短信未确认投递到本Telegram账号。"
                message += "\n记录在内存中保留最多1小时，重启会清空；请核对原短信，未自动重发。"
            await self.send_message(chat_id, message)

        except Exception as e:
            print(f"处理待确认SMS命令时出错: {str(e)}")
            await self.send_message(chat_id, f"❌ 处理命令时出错: {str(e)}")
    
    def validate_phone_number(self, phone_number: str) -> bool:
        """验证手机号格式"""
        pattern = r'^\+?[1-9]\d{1,14}$'
        return bool(re.match(pattern, phone_number))

    def parse_tp_pid(self, tp_pid) -> Optional[int]:
        """解析TP-PID值，支持0x40和十进制字符串。"""
        if tp_pid is None:
            return None
        try:
            if isinstance(tp_pid, int):
                return tp_pid
            value = str(tp_pid).strip().lower()
            if value.startswith('0x'):
                return int(value, 16)
            return int(value)
        except (TypeError, ValueError):
            return None

    def is_silent_tp_pid(self, tp_pid) -> bool:
        """3GPP TP-PID 0x40 是 Short Message Type 0。"""
        return self.parse_tp_pid(tp_pid) == 0x40
    
    def is_sms_send_successful(self, result: str, device_id: str = None) -> bool:
        """检查SMS发送是否成功（基于Asterisk Quectel模块的实际返回）"""
        if not result:
            return False
            
        result_lower = result.lower().strip()
        
        # CLI命令成功指示器（表示请求已加入队列）
        cli_success_indicators = [
            "sms queued for send",  # 主要成功消息
            "queued for send",
            "message queued"
        ]
        
        # CLI命令错误指示器（基于源码分析）
        cli_error_indicators = [
            "usage: sms send",  # 参数错误
            "device not found",
            "device disconnected", 
            "device disbaled",  # 注意：源码中是拼写错误
            "device disabled",
            "invalid phone number",
            "unknown error",
            "smsdb error",
            "queue error", 
            "pdu building error",
            "unable to allocate memory",
            "cannot parse utf-8",
            "cannot encode gsm7",
            "cannot pack gsm7"
        ]
        
        # 首先检查CLI错误指示器
        for indicator in cli_error_indicators:
            if indicator in result_lower:
                return False
        
        # 然后检查CLI成功指示器
        for indicator in cli_success_indicators:
            if indicator in result_lower:
                return True
        
        # 检查是否包含设备ID且没有错误信息（通常表示命令被接受）
        if device_id and (f"[{device_id}]" in result or device_id in result):
            # 如果包含设备ID但没有明确的错误信息，可能是成功
            has_error = any(error in result_lower for error in ["error", "failed", "invalid", "not found"])
            return not has_error
            
        return False
    
    def parse_sms_send_result(self, log_line: str) -> dict:
        """解析SMS发送结果日志（支持多种格式）"""
        # 匹配实际的发送结果日志
        # 格式: [quectel0][SMS:123 REF:456] Successfully sent message
        # 格式: [quectel0][SMS:123] Error sending message
        # 格式: [quectel0][CMGS] ✓ Sending message in progress
        # 格式: [quectel0][SMSTEXT] ✓ Sending SMS message in progress
        
        patterns = [
            # 成功发送 - 完整格式
            re.compile(r'\[(\w+)\]\[SMS:(\d+)(?:\s+REF:(\d+))?\]\s+Successfully sent message(?:.*?(\d+)\s+parts)?', re.IGNORECASE),
            # 成功发送 - 简单格式
            re.compile(r'\[(\w+)\]\[SMS:(\d+)\]\s+Successfully sent message', re.IGNORECASE),
            # 成功发送 - 分片格式
            re.compile(r'\[(\w+)\]\[SMS:(\d+)\]\s+Successfully sent message part (\d+)/(\d+)', re.IGNORECASE),
            # 发送进行中 - CMGS
            re.compile(r'\[(\w+)\]\[CMGS\]\s+[✓√]\s+Sending message in progress', re.IGNORECASE),
            # 发送进行中 - SMSTEXT
            re.compile(r'\[(\w+)\]\[SMSTEXT\]\s+[✓√]\s+Sending SMS message in progress', re.IGNORECASE),
            # 发送失败 - 标准格式
            re.compile(r'\[(\w+)\]\[SMS:(\d+)\]\s+Error sending message(?:.*?\[([+\d\s]+)\])?', re.IGNORECASE),
            # 发送失败 - SMSTEXT格式
            re.compile(r'\[(\w+)\]\[SMSTEXT\]\s+[✗×]\s+\[SMS:(\d+)\]\s+Error sending message', re.IGNORECASE),
            # 确认失败 - 标准格式
            re.compile(r'\[(\w+)\]\[SMS:(\d+)\]\s+Cannot acknowledge message', re.IGNORECASE),
            # 确认失败 - 简化格式
            re.compile(r'\[(\w+)\]\s+[✗×]\s+Cannot acknowledge message', re.IGNORECASE),
            # 其他简化格式的错误消息
            re.compile(r'\[(\w+)\]\s+[✗×]\s+.*?(error|failed|timeout)', re.IGNORECASE),
            # 状态报告
            re.compile(r'\[(\w+)\]\[SMS:(\d+)\]\s+Got status report from ([+\d\s]+) and status code (\d+)', re.IGNORECASE),
            # 报告成功
            re.compile(r'\[(\w+)\]\[SMS:(\d+)\]\s+Report: success:(\d+)', re.IGNORECASE),
            # 通用SMS相关消息（兜底）
            re.compile(r'\[(\w+)\]\[SMS:(\d+)\].*?(success|error|failed|sent|sending)', re.IGNORECASE),
            # 通用CMGS相关消息（兜底）
            re.compile(r'\[(\w+)\]\[CMGS\].*?(success|error|failed|sent|sending)', re.IGNORECASE),
            # 通用SMSTEXT相关消息（兜底）
            re.compile(r'\[(\w+)\]\[SMSTEXT\].*?(success|error|failed|sent|sending)', re.IGNORECASE)
        ]
        
        for pattern in patterns:
            match = pattern.search(log_line)
            if match:
                device_name = match.group(1)
                sms_id = match.group(2) if len(match.groups()) > 1 else None
                ref_id = None
                status_code = None
                phone_number = None
                
                # 提取额外信息
                if len(match.groups()) > 2:
                    if match.group(3) and match.group(3).isdigit():
                        ref_id = match.group(3)
                    elif '+' in str(match.group(3)):
                        phone_number = match.group(3)
                
                if len(match.groups()) > 3:
                    if match.group(4) and match.group(4).isdigit():
                        status_code = match.group(4)
                
                # 判断消息类型和成功状态
                message_type = "unknown"
                is_success = False
                
                if "successfully sent message" in log_line.lower():
                    message_type = "send_success"
                    is_success = True
                elif "sending message in progress" in log_line.lower():
                    message_type = "sending_progress"
                    is_success = True
                elif "error sending message" in log_line.lower():
                    message_type = "send_error"
                    is_success = False
                elif "cannot acknowledge message" in log_line.lower():
                    message_type = "ack_error"
                    is_success = False
                elif "got status report" in log_line.lower():
                    message_type = "status_report"
                    is_success = status_code == "0" if status_code else False
                elif "report: success" in log_line.lower():
                    message_type = "report_success"
                    is_success = True
                
                return {
                    'device': device_name,
                    'sms_id': sms_id,
                    'ref_id': ref_id,
                    'phone_number': phone_number,
                    'status_code': status_code,
                    'message_type': message_type,
                    'success': is_success,
                    'log_line': log_line.strip()
                }
        
        return None
    
    def test_sms_log_parsing(self):
        """测试SMS日志解析功能"""
        test_logs = [
            "[quectel0][SMS:123 REF:456] Successfully sent message",
            "[quectel0][SMS:123] Error sending message",
            "[quectel0][CMGS] ✓ Sending message in progress",
            "[quectel0][SMSTEXT] ✓ Sending SMS message in progress",
            "[quectel0][SMS:123] Cannot acknowledge message",
            "[quectel0][SMS:123] Got status report from +8613800138000 and status code 0",
            "[quectel0][SMS:123] Report: success:1",
            "[quectel0][SMS:123] Successfully sent message [3 parts]",
            "[quectel0][SMS:123] Successfully sent message part 1/3",
            "[quectel0][SMSTEXT] ✗ [SMS:123] Error sending message",
            "[quectel0] ✗ Cannot acknowledge message",
            "[quectel0] ✗ Connection timeout",
            "[quectel0] ✗ Send failed"
        ]
        
        print("🧪 测试SMS日志解析功能:")
        success_count = 0
        for log_line in test_logs:
            result = self.parse_sms_send_result(log_line)
            if result:
                print(f"✅ 解析成功: {result['message_type']} - {log_line}")
                success_count += 1
            else:
                print(f"❌ 解析失败: {log_line}")
        
        print(f"📊 测试结果: {success_count}/{len(test_logs)} 条日志解析成功")

    def find_pending_sms_send(self, device: str, phone_number: str = None) -> Optional[str]:
        """按设备和号码匹配最近的待发送请求。"""
        now = time.time()
        candidates = []
        for send_id, send_info in self.pending_sms_sends.items():
            if send_info.get('device_id') != device:
                continue
            if send_info.get('status') not in ('queued', 'sent'):
                continue
            if now - send_info.get('timestamp', 0) > 900:
                continue
            if phone_number and send_info.get('phone_number') != phone_number:
                continue
            candidates.append((send_info.get('timestamp', 0), send_id))

        if not candidates:
            return None
        candidates.sort()
        return candidates[0][1]

    def format_sms_send_result_message(self, sms_result: dict, send_info: dict = None) -> str:
        """生成简洁的发送结果通知。"""
        message_type = sms_result.get('message_type')
        success = sms_result.get('success')
        device = sms_result.get('device') or (send_info or {}).get('device_id', 'unknown')
        sms_id = sms_result.get('sms_id')
        ref_id = sms_result.get('ref_id')
        status_code = sms_result.get('status_code')
        phone_number = sms_result.get('phone_number') or (send_info or {}).get('phone_number')

        title_map = {
            'send_success': ('✅', '短信发送成功'),
            'send_error': ('❌', '短信发送失败'),
            'ack_error': ('⚠️', '短信确认失败'),
            'status_report': ('📊' if success else '⚠️', '短信状态报告'),
            'report_success': ('✅', '短信报告成功'),
            'sending_progress': ('📤', '短信发送中'),
        }
        emoji, title = title_map.get(message_type, ('✅' if success else '❌', '短信状态更新'))

        lines = [
            f"{emoji} <b>{title}</b>",
            "",
            f"设备 <code>{self.escape_html(device)}</code>",
        ]
        if phone_number:
            lines.append(f"接收方 <code>{self.escape_html(phone_number)}</code>")
        if sms_id:
            lines.append(f"SMS ID <code>{self.escape_html(sms_id)}</code>")
        if ref_id:
            lines.append(f"参考ID <code>{self.escape_html(ref_id)}</code>")
        if status_code:
            lines.append(f"状态码 <code>{self.escape_html(status_code)}</code>")

        if message_type == 'status_report':
            lines.append(f"状态 <code>{'成功' if success else '失败'}</code>")
        elif message_type == 'send_success':
            lines.append("状态 <code>模块已确认发出</code>")
        elif message_type in ('send_error', 'ack_error'):
            lines.append("状态 <code>失败</code>")

        if send_info and send_info.get('content'):
            lines.extend([
                "",
                f"内容 <code>{self.escape_html(self.preview_text(send_info['content']))}</code>"
            ])

        lines.append(f"时间 <code>{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</code>")
        return "\n".join(lines)
    
    async def handle_sms_send_result(self, sms_result: dict):
        """处理SMS发送结果（支持多种消息类型）"""
        try:
            device = sms_result['device']
            sms_id = sms_result['sms_id']
            ref_id = sms_result['ref_id']
            phone_number = sms_result.get('phone_number')
            status_code = sms_result.get('status_code')
            message_type = sms_result['message_type']
            success = sms_result['success']
            
            # 根据消息类型构建不同的消息
            message = ""
            emoji = "📱"
            
            if message_type == "send_success":
                emoji = "✅"
                message = f"{emoji} <b>SMS发送成功</b>\n"
                message += f"📱 设备: <code>{device}</code>\n"
                message += f"🆔 SMS ID: <code>{sms_id}</code>\n"
                if ref_id:
                    message += f"🔗 参考ID: <code>{ref_id}</code>\n"
                if phone_number:
                    message += f"📞 接收方: <code>{phone_number}</code>\n"
                message += f"⏰ 时间: <code>{datetime.now().strftime('%H:%M:%S')}</code>"
                
            elif message_type == "sending_progress":
                emoji = "📤"
                message = f"{emoji} <b>SMS发送进行中</b>\n"
                message += f"📱 设备: <code>{device}</code>\n"
                message += f"🆔 SMS ID: <code>{sms_id}</code>\n"
                message += f"⏰ 时间: <code>{datetime.now().strftime('%H:%M:%S')}</code>"
                
            elif message_type == "send_error":
                emoji = "❌"
                message = f"{emoji} <b>SMS发送失败</b>\n"
                message += f"📱 设备: <code>{device}</code>\n"
                message += f"🆔 SMS ID: <code>{sms_id}</code>\n"
                if phone_number:
                    message += f"📞 接收方: <code>{phone_number}</code>\n"
                message += f"⏰ 时间: <code>{datetime.now().strftime('%H:%M:%S')}</code>"
                
            elif message_type == "ack_error":
                emoji = "⚠️"
                message = f"{emoji} <b>SMS确认失败</b>\n"
                message += f"📱 设备: <code>{device}</code>\n"
                message += f"🆔 SMS ID: <code>{sms_id}</code>\n"
                message += f"⏰ 时间: <code>{datetime.now().strftime('%H:%M:%S')}</code>"
                
            elif message_type == "status_report":
                emoji = "📊" if success else "⚠️"
                status_text = "成功" if success else "失败"
                message = f"{emoji} <b>SMS状态报告</b>\n"
                message += f"📱 设备: <code>{device}</code>\n"
                message += f"🆔 SMS ID: <code>{sms_id}</code>\n"
                if phone_number:
                    message += f"📞 发送方: <code>{phone_number}</code>\n"
                if status_code:
                    message += f"📊 状态码: <code>{status_code}</code>\n"
                message += f"📋 状态: <code>{status_text}</code>\n"
                message += f"⏰ 时间: <code>{datetime.now().strftime('%H:%M:%S')}</code>"
                
            elif message_type == "report_success":
                emoji = "✅"
                message = f"{emoji} <b>SMS报告成功</b>\n"
                message += f"📱 设备: <code>{device}</code>\n"
                message += f"🆔 SMS ID: <code>{sms_id}</code>\n"
                message += f"⏰ 时间: <code>{datetime.now().strftime('%H:%M:%S')}</code>"
            
            else:
                # 未知类型，使用通用格式
                emoji = "✅" if success else "❌"
                status_text = "成功" if success else "失败"
                message = f"{emoji} <b>SMS状态更新</b>\n"
                message += f"📱 设备: <code>{device}</code>\n"
                if sms_id:
                    message += f"🆔 SMS ID: <code>{sms_id}</code>\n"
                if ref_id:
                    message += f"🔗 参考ID: <code>{ref_id}</code>\n"
                message += f"📋 状态: <code>{status_text}</code>\n"
                message += f"⏰ 时间: <code>{datetime.now().strftime('%H:%M:%S')}</code>"
            
            # 记录日志
            log_msg = f"📱 SMS{message_type}: {device}"
            if sms_id:
                log_msg += f" SMS:{sms_id}"
            if ref_id:
                log_msg += f" REF:{ref_id}"
            if phone_number:
                log_msg += f" PHONE:{phone_number}"
            if status_code:
                log_msg += f" STATUS:{status_code}"
            print(log_msg)
            
            # 根据通知设置决定是否发送通知
            if self.sms_notifications:
                important_types = ["send_success", "send_error", "ack_error", "status_report", "report_success"]
                if message_type in important_types:
                    matched_send = self.find_pending_sms_send(device, phone_number)
                    send_info = self.pending_sms_sends.get(matched_send) if matched_send else None
                    target_chats = []

                    if send_info:
                        message = self.format_sms_send_result_message(sms_result, send_info)
                        target_chats = [send_info['chat_id']]
                    else:
                        message = self.format_sms_send_result_message(sms_result)
                        target_chats = list(Config.AUTHORIZED_USERS)

                    notification_sent = False
                    for target_chat in dict.fromkeys(target_chats):
                        try:
                            sent = await self.send_message(target_chat, message)
                            if sent is not None:
                                notification_sent = True
                                print(f"📱 SMS通知已发送给 {target_chat}")
                            else:
                                print("⚠️ SMS状态通知投递未确认")
                        except Exception as e:
                            print(f"❌ 发送SMS通知给 {target_chat} 失败: {str(e)}")

                    if matched_send:
                        if message_type == "send_success":
                            self.pending_sms_sends[matched_send]['status'] = 'sent'
                        else:
                            self.pending_sms_sends[matched_send]['status'] = 'completed'
                        self.pending_sms_sends[matched_send]['last_result_at'] = time.time()
                        print(f"📱 更新发送请求状态: {matched_send} -> {self.pending_sms_sends[matched_send]['status']}")

                    if not notification_sent:
                        print("⚠️ SMS通知发送失败，没有成功发送给任何用户")
                elif message_type == "sending_progress":
                    pass
            else:
                print("🔇 SMS通知已关闭，跳过发送")
                
        except Exception as e:
            print(f"❌ 处理SMS发送结果时出错: {str(e)}")
    
    async def send_sms(self, chat_id: int, device_id: str, phone_number: str, content: str, silent: bool = True):
        """使用指定设备发送短信"""
        try:
            devices = await self.get_device_list()
            device_found = False
            device_phone = None
            
            for device in devices:
                if device['id'] == device_id:
                    device_found = True
                    device_phone = device['number']
                    break
            
            if not device_found:
                if not silent:
                    await self.send_message(chat_id, f"❌ 设备 <code>{self.escape_html(device_id)}</code> 不存在")
                return False
            
            # 只在非静默模式下发送设备信息
            if not silent:
                await self.send_message(
                    chat_id,
                    f"📱 <b>准备发送短信</b>\n\n"
                    f"设备 <code>{self.escape_html(device_id)}</code>\n"
                    f"本机号码 <code>{self.escape_html(device_phone or '未知')}</code>\n"
                    f"接收方 <code>{self.escape_html(phone_number)}</code>"
                )
            
            result = await self.execute_asterisk_cli(f"quectel sms send {device_id} {phone_number} {content}")
            
            # 使用智能检测方法判断是否成功
            is_success = self.is_sms_send_successful(result, device_id)
            
            if is_success:
                # 区分队列成功和实际发送成功
                if "sms queued for send" in result.lower():
                    # 记录待确认的SMS发送请求
                    send_id = f"{device_id}_{int(time.time())}_{secrets.token_hex(2)}"
                    self.pending_sms_sends[send_id] = {
                        'device_id': device_id,
                        'phone_number': phone_number,
                        'content': content,
                        'chat_id': chat_id,
                        'timestamp': time.time(),
                        'status': 'queued'
                    }
                    
                    # 只在非静默模式下发送队列消息
                    if not silent:
                        await self.send_message(
                            chat_id,
                            f"📤 <b>短信已提交</b>\n\n"
                            f"设备 <code>{self.escape_html(device_id)}</code>\n"
                            f"接收方 <code>{self.escape_html(phone_number)}</code>\n"
                            f"状态 <code>等待模块回执</code>\n\n"
                            f"内容 <code>{self.escape_html(self.preview_text(content))}</code>"
                        )
                    print(f"📱 SMS队列成功: {device_id} -> {phone_number} | 发送ID: {send_id}")
                    return True
                else:
                    if not silent:
                        await self.send_message(chat_id, f"✅ 短信发送成功到 <code>{self.escape_html(phone_number)}</code>")
                    print(f"📱 SMS发送成功: {device_id} -> {phone_number} | 响应: {result}")
                    return True
            else:
                if not silent:
                    await self.send_message(chat_id, f"❌ 短信发送失败：<code>{self.escape_html(result)}</code>")
                print(f"📱 SMS发送失败: {device_id} -> {phone_number} | 响应: {result}")
                return False
                
        except Exception as e:
            if not silent:
                await self.send_message(chat_id, f"❌ 发送短信时出错：<code>{self.escape_html(str(e))}</code>")
            return False
    
    async def query_devices(self, chat_id: int):
        """查询设备状态"""
        try:
            print("📋 开始查询设备状态...")
            devices = await self.get_device_list()
            
            if not devices:
                await self.send_message(chat_id, "❌ 没有找到设备")
                return
            
            device_info = "📱 Asterisk设备状态：\n\n"
            
            for device in devices:
                device_info += f"· 设备ID: {device['id']}\n"
                device_info += f"   状态: {device['state']}\n"
                device_info += f"   RSSI: {device['rssi']}\n"
                device_info += f"   模式: {device['mode']}\n"
                device_info += f"   提供商: {device['provider']}\n"
                device_info += f"   型号: {device['model']}\n"
                device_info += f"   固件: {device['firmware']}\n"
                device_info += f"   手机号: {device['number']}\n\n"
            
            if len(device_info) > 4000:
                chunks = [device_info[i:i+4000] for i in range(0, len(device_info), 4000)]
                for chunk in chunks:
                    await self.send_message(chat_id, chunk)
            else:
                await self.send_message(chat_id, device_info)
                
        except Exception as e:
            print(f"❌ 查询设备时出错: {str(e)}")
            await self.send_message(chat_id, f"❌ 查询设备时出错：{str(e)}")
    
    async def stop_command_process(self, process, communication):
        """Terminate the process group, drain pipes, and reap before releasing its slot."""
        if process.returncode is None:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(asyncio.shield(communication), self.PROCESS_STOP_SECONDS)
            except asyncio.TimeoutError:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        # Descendants may still own stdout after the leader exits.
        if not communication.done():
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        await communication
        await process.wait()

    async def run_bounded_command(self, command, shell=False):
        if not hasattr(self, 'command_slots'):
            self.command_slots = asyncio.Semaphore(self.COMMAND_CONCURRENCY)
        if not hasattr(self, 'processes'):
            self.processes = []
        try:
            await asyncio.wait_for(self.command_slots.acquire(), self.COMMAND_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            return "命令等待超时：未启动进程"
        process = communication = None
        try:
            kwargs = dict(stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                          start_new_session=True)
            spawn = asyncio.create_task(
                asyncio.create_subprocess_shell(command, **kwargs) if shell
                else asyncio.create_subprocess_exec(*command, **kwargs))
            try:
                process = await asyncio.shield(spawn)
            except asyncio.CancelledError:
                # Spawn may already have created a child; take ownership before
                # propagating cancellation so finally can reap it.
                process = await spawn
                self.processes.append(process)
                communication = asyncio.create_task(process.communicate())
                raise
            self.processes.append(process)
            communication = asyncio.create_task(process.communicate())
            try:
                stdout, stderr = await asyncio.wait_for(
                    asyncio.shield(communication), self.COMMAND_TIMEOUT_SECONDS)
            except asyncio.TimeoutError:
                return "命令执行超时：结果未确认，未自动重试"
            output = stdout.decode('utf-8', errors='replace')
            error = stderr.decode('utf-8', errors='replace')
            return output if process.returncode == 0 else error or output
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            return f"命令执行失败：{type(exc).__name__}"
        finally:
            try:
                if process is not None and communication is not None:
                    cleanup = asyncio.create_task(self.stop_command_process(process, communication))
                    try:
                        await asyncio.shield(cleanup)
                    except asyncio.CancelledError:
                        await cleanup
                        raise
            finally:
                if process in self.processes:
                    self.processes.remove(process)
                self.command_slots.release()

    async def execute_command(self, command: str) -> str:
        return await self.run_bounded_command(command, shell=True)

    async def execute_asterisk_cli(self, cli_command: str) -> str:
        try:
            prefix_args = shlex.split(Config.ASTERISK_COMMAND_PREFIX)
        except ValueError:
            return "命令执行失败：ASTERISK_COMMAND_PREFIX 格式错误"
        if not prefix_args:
            return "命令执行失败：ASTERISK_COMMAND_PREFIX 未配置"
        return await self.run_bounded_command([*prefix_args, cli_command])

    @staticmethod
    def parse_uac_health(state_output: str) -> bool:
        fields = {}
        for line in state_output.splitlines():
            if ':' not in line:
                continue
            key, value = line.split(':', 1)
            key, value = key.strip().lower(), value.strip().lower()
            if key in fields:
                return False
            fields[key] = value
        registration = fields.get('gsm registration status', fields.get('gsm registration', ''))
        return (fields.get('current device state') == 'start'
                and fields.get('desired device state') == 'start'
                and fields.get('voice') == 'yes'
                and fields.get('calls/channels') == '0'
                and registration in ('registered', 'registered, home network', 'registered, roaming'))

    async def recover_uac(self, device_id: str, source: str, hard_reset: bool = False) -> dict:
        """Perform exactly one explicit recovery; no path here escalates from soft to hard."""
        if not self.is_valid_quectel_device(device_id):
            return {'healthy': False, 'detail': '拒绝无效设备名', 'command': ''}
        if source == 'auto' and device_id not in await self.get_live_uac_devices():
            return {'healthy': False, 'detail': '设备不在实时列表中，未执行恢复', 'command': ''}
        command = f"quectel {'uac apply' if hard_reset else 'restart when convenient'} {device_id}"
        async with self.uac_device_locks[device_id]:
            if Config.UAC_RECOVERY_DRY_RUN:
                return {'healthy': False, 'dry_run': True, 'detail': 'UAC_RECOVERY_DRY_RUN=true，未执行 CLI', 'command': command}
            output = await self.execute_asterisk_cli(command)
            deadline = time.monotonic() + 120
            last_state = output.strip()
            while time.monotonic() < deadline:
                state_output = await self.execute_asterisk_cli(f"quectel show device state {device_id}")
                last_state = state_output.strip() or last_state
                if self.parse_uac_health(state_output):
                    return {'healthy': True, 'detail': '状态、语音、注册和空闲通话均已就绪', 'command': command}
                await asyncio.sleep(5)
            return {'healthy': False, 'detail': self.preview_text(last_state, 220), 'command': command}

    @staticmethod
    def classify_uac_log_line(log_line: str) -> Optional[dict]:
        """Classify only actionable UAC faults; normal idle XRUNs remain below the threshold."""
        device_match = re.search(r'\b(quectel[0-9]+)\b', log_line, re.IGNORECASE)
        if not device_match:
            return None
        device_id = device_match.group(1).lower()
        text = log_line.lower()
        direction = 'capture' if 'capture' in text or 'read error' in text else ('playback' if 'playback' in text else 'unknown')
        if ('prepare failed' in text or 'start failed' in text
                or re.search(r'(connection|device).*(lost|disconnected)', text)
                or re.search(r'(current|desired) device state:\s*(stop|error|failed)', text)):
            return {'device': device_id, 'severity': 'severe', 'direction': direction, 'reason': '严重 UAC 初始化/连接错误'}
        if ('error - try again later' in text and ('alsa' in text or 'playback' in text or 'capture' in text)) or 'not enough samples: 0/160' in text or 'read error' in text:
            return {'device': device_id, 'severity': 'burst', 'direction': direction, 'reason': 'UAC PCM 错误突发'}
        return None

    def uac_auto_recovery_allowed(self, device_id: str, now: float) -> tuple:
        record = self.uac_state_for(device_id)
        today = datetime.now().date().isoformat()
        if record.get('day') != today:
            record['day'] = today
            record['automatic_count'] = 0
        if now < float(record.get('cooldown_until', 0)):
            return False, '仍在冷却期'
        if int(record.get('automatic_count', 0)) >= Config.UAC_RECOVERY_MAX_PER_DAY:
            return False, '已达到每日自动恢复上限'
        return True, ''

    async def handle_uac_log_line(self, log_line: str, now: float = None) -> bool:
        event = self.classify_uac_log_line(log_line)
        if not event or not Config.UAC_AUTO_RECOVERY_ENABLED or event['device'] not in Config.UAC_AUTO_RECOVERY_DEVICES:
            return False
        now = time.time() if now is None else now
        device_id = event['device']
        events = self.uac_error_events[device_id]
        events[:] = [timestamp for timestamp in events if now - timestamp <= Config.UAC_ERROR_WINDOW_SECONDS]
        if event['severity'] == 'burst':
            events.append(now)
            if len(events) < Config.UAC_ERROR_THRESHOLD:
                return False
        allowed, reason = self.uac_auto_recovery_allowed(device_id, now)
        if not allowed:
            print(f"ℹ️ UAC 自动恢复跳过 {device_id}: {reason}")
            return False
        # Clear the burst before awaiting so a dense log flood cannot queue duplicates.
        events.clear()
        record = self.uac_state_for(device_id)
        record['automatic_count'] = int(record.get('automatic_count', 0)) + 1
        record['cooldown_until'] = now + Config.UAC_RECOVERY_COOLDOWN_SECONDS
        record['last_reason'] = f"{event['reason']} ({event['direction']})"
        record['last_started_at'] = datetime.now().isoformat(timespec='seconds')
        self.save_uac_recovery_state()
        self.create_background_task(self.run_auto_uac_recovery(device_id, event, record))
        return True

    async def run_auto_uac_recovery(self, device_id: str, event: dict, record: dict) -> None:
        """Run the already rate-limited recovery without blocking SMS/log monitoring."""
        await self.notify_authorized_users(
            f"⚠️ <b>UAC 自动软恢复已排队</b>\n\n设备 <code>{device_id}</code>\n"
            f"方向 <code>{event['direction']}</code>，窗口内计数 <code>{Config.UAC_ERROR_THRESHOLD if event['severity'] == 'burst' else 1}</code>\n"
            f"命令 <code>quectel restart when convenient {device_id}</code>\n"
            f"冷却至 <code>{datetime.fromtimestamp(record['cooldown_until']).isoformat(timespec='seconds')}</code>"
        )
        result = await self.recover_uac(device_id, source='auto', hard_reset=False)
        record['last_result'] = 'healthy' if result.get('healthy') else ('dry-run' if result.get('dry_run') else 'timeout')
        record['last_completed_at'] = datetime.now().isoformat(timespec='seconds')
        self.save_uac_recovery_state()
        await self.notify_authorized_users(
            f"{'✅' if result.get('healthy') else '⚠️'} <b>UAC 自动软恢复完成</b>\n\n设备 <code>{device_id}</code>\n"
            f"结果 <code>{self.escape_html(record['last_result'])}</code>\n"
            f"详情 <code>{self.escape_html(result.get('detail', ''))}</code>"
        )
    
    async def get_device_list(self, allow_recovery: bool = True) -> List[Dict]:
        """获取设备列表"""
        try:
            print("📋 开始获取设备列表...")
            result = await self.execute_asterisk_cli("quectel show devices")
            
            devices = []
            lines = result.strip().split('\n')
            devices_without_phone = []
            
            # 跳过标题行
            for line in lines[1:]:
                if line.strip():
                    phone_match = re.search(r'(\+\d+)$', line.strip())
                    phone_number = phone_match.group(1) if phone_match else ''
                    
                    line_without_phone = re.sub(r'\s+\+\d+$', '', line.strip())
                    parts = line_without_phone.split()
                    
                    if len(parts) >= 7:
                        state_parts = []
                        i = 2
                        while i < len(parts) and not parts[i].isdigit():
                            state_parts.append(parts[i])
                            i += 1
                        
                        state = ' '.join(state_parts) if state_parts else parts[2]
                        
                        device = {
                            'id': parts[0],
                            'group': parts[1],
                            'state': state,
                            'rssi': parts[i] if i < len(parts) else '',
                            'mode': parts[i+1] if i+1 < len(parts) else '',
                            'provider': parts[i+2] if i+2 < len(parts) else '',
                            'model': parts[i+3] if i+3 < len(parts) else '',
                            'firmware': parts[i+4] if i+4 < len(parts) else '',
                            'number': phone_number
                        }
                        
                        if device['provider'] != 'NONE' and not phone_number:
                            devices_without_phone.append(device['id'])
                        
                        devices.append(device)
            
            # 如果有设备没有手机号，执行AT命令
            if devices_without_phone and allow_recovery:
                print(f"📱 发现 {len(devices_without_phone)} 个设备无手机号，执行AT命令...")
                await self.execute_at_commands_for_devices(devices_without_phone)
                
                # 重新获取设备列表
                result = await self.execute_asterisk_cli("quectel show devices")
                lines = result.strip().split('\n')
                
                devices = []
                for line in lines[1:]:
                    if line.strip():
                        phone_match = re.search(r'(\+\d+)$', line.strip())
                        phone_number = phone_match.group(1) if phone_match else ''
                        
                        line_without_phone = re.sub(r'\s+\+\d+$', '', line.strip())
                        parts = line_without_phone.split()
                        
                        if len(parts) >= 7:
                            state_parts = []
                            i = 2
                            while i < len(parts) and not parts[i].isdigit():
                                state_parts.append(parts[i])
                                i += 1
                            
                            state = ' '.join(state_parts) if state_parts else parts[2]
                            
                            device = {
                                'id': parts[0],
                                'group': parts[1],
                                'state': state,
                                'rssi': parts[i] if i < len(parts) else '',
                                'mode': parts[i+1] if i+1 < len(parts) else '',
                                'provider': parts[i+2] if i+2 < len(parts) else '',
                                'model': parts[i+3] if i+3 < len(parts) else '',
                                'firmware': parts[i+4] if i+4 < len(parts) else '',
                                'number': phone_number
                            }
                            
                            devices.append(device)
            
            print(f"📋 获取到 {len(devices)} 个设备")
            return devices
            
        except Exception as e:
            print(f"❌ 获取设备列表时出错：{str(e)}")
            return []
    
    async def execute_at_commands_for_devices(self, device_ids: List[str]):
        """对指定设备执行AT命令"""
        try:
            print(f"📱 开始对 {len(device_ids)} 个设备执行AT命令...")
            
            for device_id in device_ids:
                print(f"📱 对设备 {device_id} 执行AT命令...")
                
                # 执行电话簿偏好命令。这里必须避开shell，否则$QCPBMPREF会被当成环境变量展开。
                pref_command = Config.PHONEBOOK_PREF_COMMAND
                result1 = await self.execute_asterisk_cli(f"quectel cmd {device_id} {pref_command}")
                print(f"📱 设备 {device_id} {pref_command} 结果: {result1}")
                
                await asyncio.sleep(1)
                
                # 执行 AT+CNUM 命令
                result2 = await self.execute_asterisk_cli(f"quectel cmd {device_id} AT+CNUM")
                print(f"📱 设备 {device_id} AT+CNUM 结果: {result2}")
                
                await asyncio.sleep(1)
            
            print("📱 AT命令执行完成")
            
        except Exception as e:
            print(f"❌ 执行AT命令时出错: {str(e)}")

    async def notify_authorized_users(self, message: str):
        """向所有授权用户推送通知"""
        for user_id in Config.AUTHORIZED_USERS:
            try:
                await self.send_message(user_id, message)
            except Exception as e:
                print(f"❌ 推送通知给用户 {user_id} 失败: {str(e)}")

    async def recover_missing_phone_numbers(self, notify: bool = False) -> List[str]:
        """检测并修复已注册但缺少本机号码的Quectel设备。"""
        try:
            devices = await self.get_device_list(allow_recovery=False)
            missing_devices = [
                device
                for device in devices
                if device.get('provider') and device.get('provider') != 'NONE' and not device.get('number')
            ]

            if not missing_devices:
                if self.phone_recovery_failures:
                    print("📱 所有设备手机号已恢复，清理恢复失败计数")
                self.phone_recovery_failures.clear()
                return []

            missing_ids = [device['id'] for device in missing_devices]
            print(f"📱 检测到设备缺少本机号码: {', '.join(missing_ids)}")

            await self.execute_at_commands_for_devices(missing_ids)
            await asyncio.sleep(2)

            refreshed_devices = await self.get_device_list(allow_recovery=False)
            refreshed_by_id = {device['id']: device for device in refreshed_devices}
            unresolved = []
            recovered = []

            for device_id in missing_ids:
                refreshed = refreshed_by_id.get(device_id)
                if refreshed and refreshed.get('number'):
                    recovered.append((device_id, refreshed['number']))
                    self.phone_recovery_failures.pop(device_id, None)
                    self.last_phone_recovery_notice.pop(device_id, None)
                else:
                    unresolved.append(device_id)
                    self.phone_recovery_failures[device_id] += 1

            if recovered:
                recovered_text = "\n".join(
                    f"· <code>{device_id}</code>: <code>{number}</code>"
                    for device_id, number in recovered
                )
                print(f"📱 已恢复手机号: {recovered}")
                if notify:
                    await self.notify_authorized_users(
                        f"✅ <b>Quectel本机号码已恢复</b>\n\n{recovered_text}"
                    )

            if unresolved:
                now = time.time()
                should_notify = False
                for device_id in unresolved:
                    last_notice = self.last_phone_recovery_notice.get(device_id, 0)
                    if now - last_notice >= Config.PHONE_RECOVERY_NOTICE_INTERVAL_SECONDS:
                        self.last_phone_recovery_notice[device_id] = now
                        should_notify = True

                expected_numbers = ", ".join(Config.PHONE_RECOVERY_EXPECTED_NUMBERS) or "未配置"
                unresolved_text = "\n".join(
                    f"· <code>{device_id}</code>，已尝试 <code>{self.phone_recovery_failures[device_id]}</code> 次"
                    for device_id in unresolved
                )
                print(f"⚠️ 手机号仍未恢复: {unresolved}")
                if notify and should_notify:
                    await self.notify_authorized_users(
                        "⚠️ <b>Quectel设备缺少本机号码</b>\n\n"
                        f"{unresolved_text}\n\n"
                        f"已执行 <code>{Config.PHONEBOOK_PREF_COMMAND}</code> 和 <code>AT+CNUM</code>。\n"
                        f"已知问题号码: <code>{expected_numbers}</code>"
                    )

            return unresolved

        except Exception as e:
            print(f"❌ 恢复手机号时出错: {str(e)}")
            if notify:
                await self.notify_authorized_users(f"❌ <b>Quectel手机号恢复任务出错</b>\n\n<code>{self.escape_html(str(e))}</code>")
            return []

    async def phone_recovery_watchdog(self):
        """后台周期性恢复缺失的Quectel本机号码。"""
        interval = max(60, Config.PHONE_RECOVERY_INTERVAL_SECONDS)
        print(f"📱 Quectel手机号恢复监控已启动，间隔 {interval} 秒")

        while self.running:
            try:
                await self.recover_missing_phone_numbers(notify=True)
                for _ in range(interval * 10):
                    if not self.running:
                        break
                    await asyncio.sleep(0.1)
            except Exception as e:
                print(f"❌ Quectel手机号恢复监控出错: {str(e)}")
                await asyncio.sleep(60)

    async def query_device_settings(self, chat_id: int, device_id: str):
        """查询设备设置"""
        try:
            print(f"📋 开始查询设备 {device_id} 的设置...")
            
            # 先检查设备状态
            result_check = await self.execute_asterisk_cli("quectel show devices")
            
            for line in result_check.strip().split('\n'):
                if line.startswith(device_id):
                    parts = line.split()
                    if len(parts) >= 7:
                        provider = parts[5] if len(parts) > 5 else 'NONE'
                        if provider == 'NONE':
                            await self.send_message(chat_id, f"❌ 设备 {device_id} 的provider为NONE，无法查询设置")
                            return
                    break
            
            # 获取设备设置
            result = await self.execute_asterisk_cli(f"quectel show device settings {device_id}")
            
            if "Settings" in result:
                settings_info = self.parse_device_settings(result, device_id)
                await self.send_message(chat_id, settings_info)
            else:
                await self.send_message(chat_id, f"❌ 无法获取设备 {device_id} 的设置信息")
                
        except Exception as e:
            print(f"❌ 查询设备设置时出错: {str(e)}")
            await self.send_message(chat_id, f"❌ 查询设备设置时出错：{str(e)}")
    
    async def query_device_state(self, chat_id: int, device_id: str):
        """查询设备状态"""
        try:
            print(f"📋 开始查询设备 {device_id} 的状态...")
            
            # 先检查设备状态
            result_check = await self.execute_asterisk_cli("quectel show devices")
            
            for line in result_check.strip().split('\n'):
                if line.startswith(device_id):
                    parts = line.split()
                    if len(parts) >= 7:
                        provider = parts[5] if len(parts) > 5 else 'NONE'
                        if provider == 'NONE':
                            await self.send_message(chat_id, f"❌ 设备 {device_id} 的provider为NONE，无法查询状态")
                            return
                    break
            
            # 获取设备状态
            result = await self.execute_asterisk_cli(f"quectel show device state {device_id}")
            
            if "Status" in result:
                state_info = self.parse_device_state(result, device_id)
                await self.send_message(chat_id, state_info)
            else:
                await self.send_message(chat_id, f"❌ 无法获取设备 {device_id} 的状态信息")
                
        except Exception as e:
            print(f"❌ 查询设备状态时出错: {str(e)}")
            await self.send_message(chat_id, f"❌ 查询设备状态时出错：{str(e)}")
    
    async def query_device_statistics(self, chat_id: int, device_id: str):
        """查询设备统计"""
        try:
            print(f"📋 开始查询设备 {device_id} 的统计...")
            
            # 先检查设备状态
            result_check = await self.execute_asterisk_cli("quectel show devices")
            
            for line in result_check.strip().split('\n'):
                if line.startswith(device_id):
                    parts = line.split()
                    if len(parts) >= 7:
                        provider = parts[5] if len(parts) > 5 else 'NONE'
                        if provider == 'NONE':
                            await self.send_message(chat_id, f"❌ 设备 {device_id} 的provider为NONE，无法查询统计")
                            return
                    break
            
            # 获取设备统计
            result = await self.execute_asterisk_cli(f"quectel show device statistics {device_id}")
            
            if "Statistics" in result:
                statistics_info = self.parse_device_statistics(result, device_id)
                await self.send_message(chat_id, statistics_info)
            else:
                await self.send_message(chat_id, f"❌ 无法获取设备 {device_id} 的统计信息")
                
        except Exception as e:
            print(f"❌ 查询设备统计时出错: {str(e)}")
            await self.send_message(chat_id, f"❌ 查询设备统计时出错：{str(e)}")
    
    def parse_device_settings(self, result: str, device_id: str = None) -> str:
        """解析设备设置信息"""
        try:
            lines = result.strip().split('\n')
            title = f"⚙️ <b>设备设置信息</b>"
            if device_id:
                title += f"\n· <b>设备</b>: {device_id}"
            title += "\n\n"
            settings_info = title
            
            for line in lines:
                line = line.strip()
                if ':' in line and not line.startswith('-'):
                    parts = line.split(':', 1)
                    if len(parts) == 2:
                        key = parts[0].strip()
                        value = parts[1].strip()
                        
                        key_translation = {
                            'Device': '设备', 'Audio UAC': '音频UAC', 'Audio format': '音频格式',
                            'Data': '数据端口', 'Channel Language': '通道语言', 'Context': '上下文',
                            'Exten': '分机', 'Group': '组', 'Used Notifications': '使用的通知',
                            '16kHz audio': '16kHz音频', 'RX gain': '接收增益', 'TX gain': '发送增益',
                            'Use CallingPres': '使用呼叫显示', 'Default CallingPres': '默认呼叫显示',
                            'Message Service': '消息服务', 'Message Storage': '消息存储',
                            'Direct Message': '直接消息', 'Auto Delete SMS': '自动删除SMS',
                            'Reset Modem': '重置调制解调器', 'Call Waiting': '呼叫等待',
                            'Multiparty Calls': '多方通话', 'DTMF Detection': 'DTMF检测',
                            'DTMF Duration': 'DTMF持续时间', 'Hold/Unhold Action': '保持/取消保持操作',
                            'Query Time': '查询时间', 'Initial Device State': '初始设备状态',
                            'Use QHUP Command': '使用QHUP命令'
                        }
                        
                        translated_key = key_translation.get(key, key)
                        # 处理空值
                        if not value or value == '':
                            value = '未设置'
                        settings_info += f"· <b>{translated_key}</b>: {value}\n"
            
            return settings_info
            
        except Exception as e:
            print(f"解析设备设置时出错: {str(e)}")
            return f"❌ 解析设备设置时出错: {str(e)}"
    
    def parse_device_state(self, result: str, device_id: str = None) -> str:
        """解析设备状态信息"""
        try:
            lines = result.strip().split('\n')
            title = f"📊 <b>设备状态信息</b>"
            if device_id:
                title += f"\n· <b>设备</b>: {device_id}"
            title += "\n\n"
            state_info = title
            
            for line in lines:
                line = line.strip()
                if ':' in line and not line.startswith('-'):
                    parts = line.split(':', 1)
                    if len(parts) == 2:
                        key = parts[0].strip()
                        value = parts[1].strip()
                        
                        key_translation = {
                            'Device': '设备', 'State': '状态', 'Audio UAC': '音频UAC',
                            'Data': '数据端口', 'Voice': '语音', 'SMS': '短信',
                            'Manufacturer': '制造商', 'Model': '型号', 'Firmware': '固件',
                            'IMEI': 'IMEI', 'IMSI': 'IMSI', 'ICCID': 'ICCID',
                            'GSM Registration Status': 'GSM注册状态', 'RSSI': '信号强度',
                            'Access technology': '接入技术', 'Network Name': '网络名称',
                            'Short Network Name': '网络简称', 'Registered PLMN': '注册PLMN',
                            'Provider Name': '提供商名称', 'Band': '频段',
                            'Location area code': '位置区域码', 'Cell ID': '小区ID',
                            'Subscriber Number': '用户号码', 'SMS Service Center': '短信服务中心',
                            'Tasks in queue': '队列中的任务', 'Commands in queue': '队列中的命令',
                            'Call Waiting': '呼叫等待', 'Current device state': '当前设备状态',
                            'Desired device state': '期望设备状态', 'When change state': '状态变更时间',
                            'Calls/Channels': '通话/通道', 'Active': '活跃', 'Held': '保持',
                            'Dialing': '拨号中', 'Alerting': '振铃中', 'Incoming': '来电',
                            'Waiting': '等待', 'Releasing': '释放中', 'Initializing': '初始化中'
                        }
                        
                        translated_key = key_translation.get(key, key)
                        # 处理空值
                        if not value or value == '':
                            value = '未设置'
                        state_info += f"· <b>{translated_key}</b>: {value}\n"
            
            return state_info
            
        except Exception as e:
            print(f"解析设备状态时出错: {str(e)}")
            return f"❌ 解析设备状态时出错: {str(e)}"
    
    def parse_device_statistics(self, result: str, device_id: str = None) -> str:
        """解析设备统计信息"""
        try:
            lines = result.strip().split('\n')
            title = f"📈 <b>设备统计信息</b>"
            if device_id:
                title += f"\n· <b>设备</b>: {device_id}"
            title += "\n\n"
            statistics_info = title
            
            for line in lines:
                line = line.strip()
                if ':' in line and not line.startswith('-'):
                    parts = line.split(':', 1)
                    if len(parts) == 2:
                        key = parts[0].strip()
                        value = parts[1].strip()
                        
                        key_translation = {
                            'Device': '设备', 'Queue tasks': '队列任务', 'Queue commands': '队列命令',
                            'Responses': '响应数', 'Bytes of read responses': '读取响应字节数',
                            'Bytes of written commands': '写入命令字节数', 'Bytes of read audio': '读取音频字节数',
                            'Bytes of written audio': '写入音频字节数', 'Readed frames': '读取帧数',
                            'Readed short frames': '读取短帧数', 'Wrote frames': '写入帧数',
                            'Wrote short frames': '写入短帧数', 'Wrote silence frames': '写入静音帧数',
                            'Write buffer overflow bytes': '写入缓冲区溢出字节数',
                            'Write buffer overflow count': '写入缓冲区溢出次数',
                            'Incoming calls': '来电数', 'Waiting calls': '等待通话数',
                            'Handled input calls': '已处理输入通话数', 'Fails to PBX run': 'PBX运行失败数',
                            'Attempts to outgoing calls': '呼出尝试数', 'Answered outgoing calls': '已接听呼出数',
                            'Answered incoming calls': '已接听来电数', 'Seconds of outgoing calls': '呼出通话秒数',
                            'Seconds of incoming calls': '来电通话秒数', 'ACD for incoming calls': '来电平均通话时长',
                            'ACD for outgoing calls': '呼出平均通话时长', 'ASR for incoming calls': '来电应答率',
                            'ASR for outgoing calls': '呼出应答率'
                        }
                        
                        translated_key = key_translation.get(key, key)
                        # 处理空值
                        if not value or value == '':
                            value = '未设置'
                        statistics_info += f"· <b>{translated_key}</b>: {value}\n"
            
            return statistics_info
            
        except Exception as e:
            print(f"解析设备统计时出错: {str(e)}")
            return f"❌ 解析设备统计时出错: {str(e)}"
    
    def is_silent_sms(self, content: str, device_name: str = None, sender: str = None, context: dict = None) -> tuple[bool, str]:
        """
        智能检测是否为silent SMS
        返回 (是否为silent, 检测原因)
        """
        try:
            if not content:
                return True, "空内容"
            
            content_stripped = content.strip()
            if not content_stripped:
                return True, "只有空白字符"

            if context:
                tp_pid = context.get('tp_pid')
                if not tp_pid and context.get('silent_sms_notice'):
                    notice = context.get('silent_sms_notice')
                    if isinstance(notice, dict):
                        tp_pid = notice.get('tp_pid')
                parsed_pid = self.parse_tp_pid(tp_pid)
                if parsed_pid is not None:
                    if self.is_silent_tp_pid(parsed_pid):
                        return True, f"TP-PID Type 0 (0x{parsed_pid:02x})"
                    return False, f"非Type0 TP-PID (0x{parsed_pid:02x})"
            
            # 1. 检查是否包含中文字符 - 包含中文的短信通常不是silent SMS
            if any('\u4e00' <= char <= '\u9fff' for char in content):
                return False, "包含中文字符"
            
            # 2. 检查是否包含常见短信标识
            common_sms_patterns = [
                (r'【.*?】', "包含中文括号"),
                (r'\[.*?\]', "包含方括号"),
                (r'http[s]?://', "包含网址"),
                (r'www\.', "包含网址"),
                (r'\.(com|cn|net|org|gov)', "包含域名"),
                (r'验证码|验证|code', "验证码关键字"),
                (r'您的.*码|您的.*验证', "验证码模式"),
                (r'尊敬的|亲爱的|先生|女士', "常见称呼"),
                (r'优惠|促销|活动|打折', "营销关键字"),
                (r'银行|信用卡|支付宝|微信', "金融关键字"),
                (r'通知|提醒|账单|还款', "通知关键字"),
            ]
            
            for pattern, reason in common_sms_patterns:
                if re.search(pattern, content, re.IGNORECASE):
                    return False, reason
            
            # 3. 长度检查 - 正常短信通常至少有10个字符
            if len(content_stripped) >= 10:
                return False, f"长度正常({len(content_stripped)}字符)"
            
            # 4. 检查是否只包含空白字符
            if content.isspace():
                return True, "只有空白字符"
            
            # 5. 验证码检查 - 4-8位数字
            if re.match(r'^\d{4,8}$', content_stripped):
                return False, "验证码格式"
            
            # 6. 控制字符检查
            if any(ord(char) < 32 and char not in '\r\n\t' for char in content):
                return True, "包含控制字符"
            
            # 没有Type 0 TP-PID时，不再用短数字、关键词或发送频率推断隐藏定位短信，避免误报。
            return False, "正常短信"
            
        except Exception as e:
            print(f"检测silent SMS时出错: {str(e)}")
            return False, f"检测错误: {str(e)}"
    
    def check_sender_frequency(self, sender: str, device_name: str) -> bool:
        """检查发送者频率是否异常（短时间内多次发送）"""
        try:
            current_time = datetime.now()
            time_window = 60  # 60秒窗口
            recent_count = 0
            
            # 检查最近1分钟内的发送次数
            for record in self.log_cache:
                if (record.get('sender') == sender and 
                    record.get('device') == device_name and
                    (current_time - record.get('timestamp', current_time)).total_seconds() < time_window):
                    recent_count += 1
            
            # 如果1分钟内发送超过3次，可能是异常
            return recent_count > 3
            
        except Exception as e:
            print(f"检查发送频率时出错: {str(e)}")
            return False
    
    def add_to_log_cache(self, log_entry: dict):
        """添加日志条目到缓存"""
        try:
            current_time = datetime.now()
            log_entry['timestamp'] = current_time
            
            # 添加到缓存
            self.log_cache.append(log_entry)
            
            # 清理过期缓存
            self.clean_log_cache()
            
            # 限制缓存大小
            if len(self.log_cache) > self.log_cache_max_size:
                self.log_cache = self.log_cache[-self.log_cache_max_size:]
                            
        except Exception as e:
            print(f"添加日志缓存时出错: {str(e)}")
    
    def clean_log_cache(self):
        """清理过期的日志缓存"""
        try:
            current_time = datetime.now()
            self.log_cache = [
                entry for entry in self.log_cache
                if (current_time - entry.get('timestamp', current_time)).total_seconds() < self.log_cache_max_age
            ]
        except Exception as e:
            print(f"清理日志缓存时出错: {str(e)}")
    
    async def analyze_log_context(self, device_name: str, sender: str, timestamp: str, content: str) -> dict:
        """分析日志上下文，查找3分钟内的相关日志"""
        try:
            target_time = datetime.strptime(timestamp, '%Y-%m-%d %H:%M:%S')
            time_window = 180  # 3分钟
            
            context = {
                'tp_pid_found': False,
                'silent_sms_notice': None,
                'related_sms_count': 0,
                'sender_history': [],
                'device_activity': [],
                'analysis_time': target_time
            }
            
            # 在缓存中查找相关日志
            for entry in self.log_cache:
                entry_time = entry.get('timestamp')
                if not entry_time:
                    continue
                
                time_diff = abs((target_time - entry_time).total_seconds())
                if time_diff <= time_window:
                    
                    # 检查是否有Silent SMS NOTICE
                    if entry.get('type') == 'silent_notice' and entry.get('device') == device_name:
                        context['tp_pid_found'] = True
                        context['silent_sms_notice'] = entry
                    
                    # 统计相关SMS数量
                    if entry.get('type') == 'sms_received' and entry.get('device') == device_name:
                        context['related_sms_count'] += 1
                    
                    # 记录发送者历史
                    if entry.get('sender') == sender:
                        context['sender_history'].append(entry)
                    
                    # 记录设备活动
                    if entry.get('device') == device_name:
                        context['device_activity'].append(entry)
            
            # 如果缓存中没有足够信息，尝试从日志文件读取
            if not context['tp_pid_found'] and len(context['sender_history']) < 2:
                await self.analyze_log_file_context(target_time, device_name, sender, context)
            
            return context
            
        except Exception as e:
            print(f"分析日志上下文时出错: {str(e)}")
            return {'error': str(e)}
    
    async def analyze_log_file_context(self, target_time: datetime, device_name: str, sender: str, context: dict):
        """从日志文件分析上下文（仅在缓存信息不足时使用）"""
        try:
            # 计算时间范围
            start_time = target_time - timedelta(minutes=3)
            end_time = target_time + timedelta(minutes=1)
            
            # 使用grep搜索相关日志（更高效）
            grep_pattern = f"\\[{device_name}\\]"
            cmd = f"grep '{grep_pattern}' '{self.log_file}' | head -100"
            
            result = await self.execute_command(cmd)
            if not result:
                return
            
            lines = result.strip().split('\n')
            for line in lines:
                try:
                    # 解析日志时间戳
                    time_match = re.search(r'\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\]', line)
                    if not time_match:
                        continue
                    
                    log_time = datetime.strptime(time_match.group(1), '%Y-%m-%d %H:%M:%S')
                    
                    # 检查是否在时间范围内
                    if start_time <= log_time <= end_time:
                        
                        # 检查Silent SMS NOTICE
                        if 'TP-PID' in line and 'NOTICE' in line:
                            tp_pid_match = re.search(r'TP-PID value (0x[0-9a-fA-F]+)', line)
                            if tp_pid_match and self.is_silent_tp_pid(tp_pid_match.group(1)):
                                context['tp_pid_found'] = True
                                context['silent_sms_notice'] = {
                                    'timestamp': log_time,
                                    'tp_pid': tp_pid_match.group(1),
                                    'line': line
                                }
                        
                        # 统计SMS数量
                        if 'Got message from' in line:
                            context['related_sms_count'] += 1
                        
                except Exception as e:
                    continue
                    
        except Exception as e:
            print(f"分析日志文件上下文时出错: {str(e)}")
    
    async def verify_silent_sms(self, device_name: str, sender: str, content: str, timestamp: str, context: dict) -> tuple[bool, str, dict]:
        """验证是否为Silent SMS，基于上下文分析"""
        try:
            # 深度分析
            analysis = await self.analyze_log_context(device_name, sender, timestamp, content)
            
            # 检查TP-PID标识（最高优先级）
            if analysis.get('tp_pid_found'):
                tp_pid_info = analysis.get('silent_sms_notice', {})
                tp_pid_value = tp_pid_info.get('tp_pid', '未知')

                if self.is_silent_tp_pid(tp_pid_value):
                    final_reason = f"确认Type 0 Silent SMS - TP-PID: {tp_pid_value}"
                    return True, final_reason, analysis

                final_reason = f"检测到非Type0 TP-PID {tp_pid_value}，按普通短信处理"
                return False, final_reason, analysis
            
            # 如果没有TP-PID，进行基础内容检测
            is_silent, reason = self.is_silent_sms(content, device_name, sender, context)
            
            # 如果基础检测不是Silent SMS，直接返回
            if not is_silent:
                return False, reason, context
            
            # 没有TP-PID但有可疑内容，进行综合评分
            confidence_score = 0
            evidence = []
            
            # 1. 发送频率证据
            if len(analysis.get('sender_history', [])) > 2:
                confidence_score += 20
                evidence.append("发送频率异常")
            
            # 2. 设备活动证据
            if len(analysis.get('device_activity', [])) > 5:
                confidence_score += 15
                evidence.append("设备活动频繁")
            
            # 3. 内容特征证据（高权重）
            if reason in ["空内容", "只有空白字符", "包含控制字符"]:
                confidence_score += 40
                evidence.append(f"内容特征: {reason}")
            
            # 4. 关键词证据（高权重）
            if "关键词" in reason:
                confidence_score += 35
                evidence.append(f"关键词匹配: {reason}")
            
            # 5. 上下文辅助证据
            if analysis.get('related_sms_count', 0) > 3:
                confidence_score += 10
                evidence.append("相关SMS数量异常")
            
            # 判断结果（调整阈值，因为没有了TP-PID的最高权重）
            if confidence_score >= 50:  # 降低阈值，因为没有TP-PID
                final_reason = f"高置信度Silent SMS (分数: {confidence_score}) - {', '.join(evidence)}"
                return True, final_reason, analysis
            elif confidence_score >= 25:  # 降低疑似阈值
                final_reason = f"疑似Silent SMS (分数: {confidence_score}) - {', '.join(evidence)}"
                return True, final_reason, analysis
            else:
                final_reason = f"误判，重新分类为正常短信 (分数: {confidence_score})"
                return False, final_reason, analysis
                
        except Exception as e:
            print(f"验证Silent SMS时出错: {str(e)}")
            return False, f"验证错误: {str(e)}", {}
    
    def queue_sms_item(self, item):
        try:
            self.sms_outbox.put_nowait(item)
        except asyncio.QueueFull:
            self.sms_queue_dropped += 1

    def queue_guard_notice(self, device, transition):
        kind, dropped = transition
        self.queue_sms_item({'notice': kind, 'device_id': device, 'dropped': dropped})

    async def handle_sms_guard_command(self, chat_id, text, user_id):
        if not Config.is_authorized(user_id):
            return
        parts = text.split()
        if len(parts) > 3 or (len(parts) == 3 and not DEVICE.fullmatch(parts[2])):
            await self.send_message(chat_id, '/sms_guard auto|manual|off [quectel0]')
            return
        action = parts[1] if len(parts) >= 2 else 'status'
        if action != 'status':
            try:
                self.sms_guard.set_mode(action, parts[2] if len(parts) == 3 else None)
            except (OSError, ValueError):
                await self.send_message(chat_id, '设置失败。/sms_guard auto|manual|off [quectel0]')
                return
        labels = {'auto': '自动', 'manual': '手动拦截', 'off': '关闭'}
        lines = ['短信防护', '默认：' + labels[self.sms_guard.state['default_mode']],
                 '自动阈值：一分钟15条或五分钟30条验证码',
                 '/sms_guard auto [quectel0]', '/sms_guard manual [quectel0]',
                 '/sms_guard off [quectel0]']
        for record in self.sms_guard.state['devices'].values():
            mode = labels[record['mode']]
            if record['active']:
                mode += '（拦截中）'
            lines.append(f"{record['device']}：{mode}，丢弃 {record['dropped']} 条")
        if self.sms_guard.storage_failed:
            lines.append('状态保存异常，验证码暂时丢弃')
        lines.append(f'发送队列满时丢弃：{self.sms_queue_dropped} 条')
        await self.send_message(chat_id, '\n'.join(lines), parse_mode=None)

    async def process_sms_data(self, data: str):
        try:
            info = json.loads(data)
            if not isinstance(info, dict):
                raise ValueError('SMS envelope type')
            device = info['device_id']
            sender = info.get('sender_number', '')
            content = info.get('content', '')
            if info.get('content_base64'):
                content = base64.b64decode(info['content_base64'], validate=True).decode('utf-8')
            timestamp = info.get('timestamp', '')
            event_id = info.get('event_id')
            sim_id = info.get('sim_id', '')
            if not all(isinstance(value, str) for value in (device, sender, content, timestamp, sim_id)):
                raise ValueError('SMS field type')
            if len(content.encode('utf-8')) > 131072 or not DEVICE.fullmatch(device):
                raise ValueError('SMS field bounds')
            if not isinstance(event_id, str) or not re.fullmatch(r'[A-Za-z0-9_.-]{1,100}', event_id):
                raise ValueError('SMS event identity missing')
            outcome, transition = self.sms_guard.admit(device, content, device + ':' + event_id, sim_id)
            if transition:
                self.queue_guard_notice(device, transition)
            if outcome != 'allow':
                return
            self.queue_sms_item({'device_id': device, 'sender_number': sender,
                                 'content': content, 'timestamp': timestamp})
        except (ValueError, TypeError, KeyError, UnicodeError) as exc:
            print('短信输入已丢弃：' + type(exc).__name__)

    async def deliver_sms_item(self, info):
        if 'notice' in info:
            device = info['device_id']
            message = (f"{device} 短信防护已开启，后续验证码正文丢弃" if info['notice'] == 'start'
                       else f"{device} 短信防护已解除，丢弃 {info['dropped']} 条验证码")
            for user in Config.AUTHORIZED_USERS:
                await self.send_message(user, message, parse_mode=None)
            return
        device, sender = info['device_id'], info['sender_number']
        content, timestamp = info['content'], info['timestamp']
        self.add_to_log_cache({'type': 'sms_received', 'device': device,
                               'sender': sender, 'timestamp_str': timestamp})
        is_silent, reason, analysis = await self.verify_silent_sms(
            device, sender, content, timestamp, {'timestamp': timestamp})
        await self.push_sms_to_users(device, sender, content, timestamp, is_silent, reason, analysis)

    async def sms_delivery_worker(self):
        while self.running:
            item = await self.sms_outbox.get()
            try:
                await asyncio.wait_for(self.deliver_sms_item(item), timeout=30)
            except Exception as exc:
                print('短信投递未确认：' + type(exc).__name__ + '；未自动重发')
            finally:
                self.sms_outbox.task_done()
                del item

    async def sms_guard_watchdog(self):
        while self.running:
            await asyncio.sleep(5)
            try:
                for device, dropped in self.sms_guard.recover():
                    self.queue_guard_notice(device, ('end', dropped))
            except OSError:
                self.sms_guard.storage_failed = True

    async def push_sms_to_users(self, device_id: str, sender_number: str, content: str, timestamp: str, is_silent: bool = False, reason: str = "", analysis: dict = None):
        """向授权用户推送短信"""
        try:
            # 获取设备对应的手机号
            devices = await self.get_device_list()
            device_phone = ""
            for device in devices:
                if device['id'] == device_id:
                    device_phone = device['number']
                    break
            
            device_display = f"{device_id} {device_phone}" if device_phone else device_id
            
            # 生成唯一的SMS ID用于回复
            sms_id = 'sms_' + secrets.token_hex(12)

            sms_info = {
                'device': device_id,
                'device_display': device_display,
                'sender': sender_number,
                'content': content,
                'timestamp': timestamp,
                'created_at': datetime.now(),
                'delivery_status': {}
            }
            
            if is_silent:
                # 使用增强的格式化方法构建Silent SMS报告
                message = self.format_silent_sms_message(
                    device_display, sender_number, timestamp, content, reason, analysis
                )
                
                # Silent SMS 不需要回复按钮
                buttons = None
            else:
                message = self.format_original_sms_message(sms_info)
                
                # 为普通短信添加回复按钮
                buttons = [
                    [Button.inline("💬 回复", f"reply_sms:{sms_id}")]
                ]
            
            # 保存SMS信息用于回复
            if len(self.pending_replies) >= 256:
                del self.pending_replies[next(iter(self.pending_replies))]
            self.pending_replies[sms_id] = sms_info
            
            # 向所有授权用户发送消息
            for user_id in Config.AUTHORIZED_USERS:
                try:
                    sent = await self.send_message(user_id, message, buttons=buttons, link_preview=is_silent)
                    sms_info['delivery_status'][user_id] = 'confirmed' if sent is not None else 'unconfirmed'
                    if sent is None:
                        print("❌ 短信Telegram投递未确认；保留待确认记录，未自动重发")
                        continue
                    if is_silent:
                        print(f"🔇 Silent SMS已推送给用户: {user_id}")
                    else:
                        print(f"📤 短信已推送给用户: {user_id}")
                except Exception as e:
                    sms_info['delivery_status'][user_id] = 'unconfirmed'
                    print(f"❌ 短信Telegram投递未确认: {type(e).__name__}")
                    
        except Exception as e:
            print(f"推送短信时出错: {str(e)}")
    
    async def listen_sms_pipe(self):
        """监听SMS管道"""
        print("📡 开始监听SMS管道...")
        while self.running:
            try:
                if not os.path.exists(self.sms_pipe_path):
                    print(f"⚠️ SMS管道不存在: {self.sms_pipe_path}")
                    await asyncio.sleep(5)
                    continue
                
                fd = None
                try:
                    fd = os.open(self.sms_pipe_path, os.O_RDONLY | os.O_NONBLOCK)
                    buffer = b''

                    while self.running:
                        try:
                            chunk = os.read(fd, 4096)
                            if not chunk:
                                await asyncio.sleep(0.2)
                                continue

                            buffer += chunk
                            if len(buffer) > 524288 and b'\n' not in buffer:
                                buffer = b''
                            while b'\n' in buffer:
                                raw_line, buffer = buffer.split(b'\n', 1)
                                if len(raw_line) > 262144:
                                    continue
                                line = raw_line.decode('utf-8', errors='replace').strip()
                                if line:
                                    await self.process_sms_data(line)
                        except BlockingIOError:
                            await asyncio.sleep(0.2)
                        except Exception as e:
                            print(f"⚠️ 读取管道文件时出错: {str(e)}")
                            await asyncio.sleep(1)
                        
                except Exception as e:
                    print(f"⚠️ 读取SMS管道时出错: {str(e)}")
                    await asyncio.sleep(1)
                finally:
                    if fd is not None:
                        os.close(fd)
                    
            except Exception as e:
                print(f"❌ SMS管道监听出错: {str(e)}")
                await asyncio.sleep(5)
    
    async def monitor_asterisk_logs(self):
        """监控Asterisk日志"""
        try:
            # 检查日志文件是否存在
            if not os.path.exists(self.log_file):
                print(f"⚠️ 日志文件不存在: {self.log_file}")
                # 尝试重新检测日志文件
                self.log_file = self.detect_asterisk_log_file()
                if not os.path.exists(self.log_file):
                    print(f"❌ 无法找到有效的Asterisk日志文件")
                return
            
            print(f"📋 开始监控Asterisk日志: {self.log_file}")
            
            # 创建tail进程
            process = await asyncio.create_subprocess_exec(
                'tail', '-F', self.log_file,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            self.processes.append(process)
            
            # 预编译正则表达式以提高性能
            silent_sms_patterns = [
                # 标准 Silent SMS NOTICE 模式
                re.compile(r'\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\].*?\[NOTICE\].*?\[(\w+)\].*?Treating TP-PID value (0x[0-9a-fA-F]+) as regular SMS'),
                # 变体模式1：不同的日志格式
                re.compile(r'\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\].*?\[NOTICE\].*?\[(\w+)\].*?TP-PID (0x[0-9a-fA-F]+).*?as regular SMS'),
                # 变体模式2：可能的其他格式
                re.compile(r'\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\].*?\[(\w+)\].*?Silent SMS.*?TP-PID (0x[0-9a-fA-F]+)')
            ]
            
            line_count = 0
            last_log_check = time.time()
            
            while self.running:
                try:
                    # 使用更短的超时时间，以便更快响应停止信号
                    line = await asyncio.wait_for(process.stdout.readline(), timeout=0.5)
                    if not line:
                        # 检查进程是否还在运行
                        if process.returncode is not None:
                            print("⚠️ tail进程已退出，重新启动...")
                            await asyncio.sleep(5)
                            break
                        continue
                    
                    line_str = line.decode('utf-8', errors='ignore').strip()
                    line_count += 1

                    # UAC recovery intentionally shares this listener; no periodic restart is used.
                    await self.handle_uac_log_line(line_str)
                    
                    # 每1000行输出一次状态
                    if line_count % 1000 == 0:
                        print(f"📋 已处理 {line_count} 行日志")
                    
                    # 检测 SMS 发送结果
                    sms_result = self.parse_sms_send_result(line_str)
                    if sms_result:
                        print(f"📱 检测到SMS发送结果: {sms_result['message_type']} - {line_str}")
                        await self.handle_sms_send_result(sms_result)
                    
                    # 检测 Silent SMS NOTICE
                    for pattern in silent_sms_patterns:
                        silent_match = pattern.search(line_str)
                        if silent_match:
                            try:
                                log_timestamp = silent_match.group(1)
                                device_name = silent_match.group(2)
                                tp_pid = silent_match.group(3)
                                
                                await self.handle_silent_sms_notice(device_name, tp_pid, log_timestamp, line_str)
                                break  # 找到匹配后跳出循环
                            except (IndexError, AttributeError) as e:
                                print(f"⚠️ Silent SMS模式匹配错误: {str(e)}")
                                continue
                    
                    # 定期检查日志文件是否仍然存在
                    current_time = time.time()
                    if current_time - last_log_check > 60:  # 每分钟检查一次
                        last_log_check = current_time
                        if not os.path.exists(self.log_file):
                            print(f"⚠️ 日志文件已删除: {self.log_file}")
                            break
                            
                except asyncio.TimeoutError:
                    # 超时是正常的，继续循环
                    continue
                except Exception as e:
                    print(f"⚠️ 处理日志行时出错: {str(e)}")
                    continue
                    
        except Exception as e:
            print(f"❌ 监控Asterisk日志时出错: {str(e)}")
        finally:
            if 'process' in locals():
                if process in self.processes:
                    self.processes.remove(process)
                if process.returncode is None:
                    process.terminate()
                    try:
                        await asyncio.wait_for(process.wait(), timeout=5)
                    except asyncio.TimeoutError:
                        process.kill()
    
    async def restart_log_monitor(self):
        """重启日志监控"""
        print("🔄 重启日志监控...")
        # 分段睡眠，以便更快响应停止信号
        for _ in range(20):  # 2秒 = 20 * 0.1秒
            if not self.running:
                break
            await asyncio.sleep(0.1)
        if self.running:
            asyncio.create_task(self.monitor_asterisk_logs())
    
    async def monitor_asterisk_logs_with_restart(self):
        """带自动重启的日志监控"""
        restart_count = 0
        max_restarts = 10
        
        while self.running and restart_count < max_restarts:
            try:
                print(f"📋 启动日志监控 (尝试 {restart_count + 1}/{max_restarts})")
                await self.monitor_asterisk_logs()
                
                # 如果正常退出，重置重启计数
                restart_count = 0
                print("📋 日志监控正常退出")
                
            except Exception as e:
                restart_count += 1
                print(f"❌ 日志监控出错 (第 {restart_count} 次): {str(e)}")
                
                if restart_count < max_restarts and self.running:
                    wait_time = min(30, 5 * restart_count)  # 递增等待时间，最多30秒
                    print(f"⏳ 等待 {wait_time} 秒后重启...")
                    # 分段睡眠，以便更快响应停止信号
                    for _ in range(wait_time * 10):  # 每0.1秒检查一次
                        if not self.running:
                            break
                        await asyncio.sleep(0.1)
                else:
                    if not self.running:
                        print("🛑 收到停止信号，退出日志监控")
                    else:
                        print(f"❌ 日志监控重启次数已达上限 ({max_restarts})，停止自动重启")
                    break
    
    async def handle_silent_sms_notice(self, device_name, tp_pid, log_timestamp, log_line):
        """处理 Silent SMS NOTICE"""
        if not self.is_silent_tp_pid(tp_pid):
            print(f"ℹ️ 忽略非Type0 TP-PID notice: Device={device_name}, TP-PID={tp_pid}")
            return

        print(f"🔇 [SILENT SMS NOTICE] Device: {device_name}, TP-PID: {tp_pid}, Time: {log_timestamp}")
        
        silent_record = {
            'device_name': device_name,
            'tp_pid': tp_pid,
            'log_timestamp': log_timestamp,
            'log_line': log_line,
            'detected_at': datetime.now()
        }
        
        # 添加到Silent SMS队列
        self.silent_sms_queue[device_name].append(silent_record)
        
        # 添加到日志缓存
        self.add_to_log_cache({
            'type': 'silent_notice',
            'device': device_name,
            'tp_pid': tp_pid,
            'timestamp_str': log_timestamp,
            'line': log_line
        })
        
        # 发送通知
        await self.send_silent_sms_notification(device_name, tp_pid, log_timestamp, log_line)
    
    async def handle_sms_received(self, device_name, sms_id, sender, message, sms_timestamp):
        # Receive logs cannot carry the producer's stable event identity.
        return

    def find_matching_silent_sms(self, device_name, sms_timestamp):
        """查找匹配的 Silent SMS 记录"""
        if device_name not in self.silent_sms_queue:
            return None
        
        try:
            sms_time = datetime.strptime(sms_timestamp, '%Y-%m-%d %H:%M:%S')
            
            for record in self.silent_sms_queue[device_name]:
                log_time = datetime.strptime(record['log_timestamp'], '%Y-%m-%d %H:%M:%S')
                
                if abs((sms_time - log_time).total_seconds()) <= 10:
                    return record
        except ValueError:
            pass
        
        return None
    
    async def send_silent_sms_notification(self, device_name, tp_pid, log_timestamp, log_line):
        """发送 Silent SMS 通知"""
        try:
            message = f"""
🔇 <b>SILENT SMS 检测 (NOTICE)</b>

📱 设备: <code>{device_name}</code>
🔍 TP-PID: <code>{tp_pid}</code>
⏰ 时间: <code>{log_timestamp}</code>
📋 日志: <code>{log_line}</code>

⚠️ <b>注意</b>: 检测到Silent SMS，正在等待内容...
            """
            
            for user_id in Config.AUTHORIZED_USERS:
                try:
                    await self.send_message(user_id, message)
                    print(f"🔇 Silent SMS通知已推送给用户: {user_id}")
                except Exception as e:
                    print(f"推送Silent SMS通知给用户 {user_id} 失败: {str(e)}")
                    
        except Exception as e:
            print(f"发送Silent SMS通知时出错: {str(e)}")
    
    async def send_silent_sms_content_notification(self, device_name, sender, message, tp_pid, is_silent=True, reason="", analysis=None):
        """发送 Silent SMS 内容通知"""
        try:
            if is_silent:
                notification_message = f"""
🔇 <b>SILENT SMS 内容确认</b>

📱 设备: <code>{device_name}</code>
📞 发送方: <code>{sender}</code>
🔍 TP-PID: <code>{tp_pid if tp_pid else '未知'}</code>
📝 原始内容: <code>{repr(message)}</code>
📊 内容长度: <code>{len(message)} 字符</code>
🎯 检测原因: <code>{reason}</code>
⏰ 时间: <code>{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</code>"""

                # 添加分析详情
                if analysis:
                    notification_message += f"""
📈 <b>上下文分析</b>:
"""
                    if analysis.get('tp_pid_found'):
                        notification_message += f"· TP-PID标识: 已检测到\n"
                    if analysis.get('related_sms_count', 0) > 0:
                        notification_message += f"· 相关SMS数量: {analysis['related_sms_count']}\n"
                    if analysis.get('sender_history'):
                        notification_message += f"· 发送者历史: {len(analysis['sender_history'])}条记录\n"
                    if analysis.get('device_activity'):
                        notification_message += f"· 设备活动: {len(analysis['device_activity'])}条记录\n"

                notification_message += f"""
⚠️ <b>确认</b>: 这是Silent/Stealth SMS
🚨 <b>安全警告</b>: 可能用于监控、定位或状态检查
                """
            else:
                notification_message = self.format_original_sms_message({
                    'device': device_name,
                    'sender': sender,
                    'content': message,
                    'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                }) + "\n\nℹ️ <i>检测到TP-PID但内容正常</i>"
            
            for user_id in Config.AUTHORIZED_USERS:
                try:
                    sent = await self.send_message(user_id, notification_message, link_preview=is_silent)
                    if sent is not None:
                        print(f"📱 SMS内容通知已推送给用户: {user_id}")
                    else:
                        print("❌ SMS内容通知投递未确认；未自动重发")
                except Exception as e:
                    print(f"推送SMS内容通知给用户 {user_id} 失败: {str(e)}")
                    
        except Exception as e:
            print(f"发送SMS内容通知时出错: {str(e)}")
    
    async def cleanup_old_records(self):
        """清理旧记录"""
        while self.running:
            try:
                await asyncio.sleep(300)  # 每5分钟清理一次
                
                current_time = datetime.now()
                
                # 清理 Silent SMS 队列中的旧记录（超过30分钟）
                for device_name in list(self.silent_sms_queue.keys()):
                    self.silent_sms_queue[device_name] = [
                        record for record in self.silent_sms_queue[device_name]
                        if (current_time - record['detected_at']).total_seconds() < 1800
                    ]
                    
                    if not self.silent_sms_queue[device_name]:
                        del self.silent_sms_queue[device_name]
                
                # 清理疑似 SMS 队列中的旧记录（超过30分钟）
                for device_name in list(self.suspected_sms_queue.keys()):
                    self.suspected_sms_queue[device_name] = [
                        record for record in self.suspected_sms_queue[device_name]
                        if (current_time - record['detected_at']).total_seconds() < 1800
                    ]
                    
                    if not self.suspected_sms_queue[device_name]:
                        del self.suspected_sms_queue[device_name]
                
                # 清理日志缓存
                self.clean_log_cache()
                
                # 清理过期的待回复SMS记录（超过1小时）
                expired_sms_ids = []
                for sms_id, sms_info in self.pending_replies.items():
                    if (current_time - sms_info['created_at']).total_seconds() > 3600:
                        expired_sms_ids.append(sms_id)
                
                for sms_id in expired_sms_ids:
                    del self.pending_replies[sms_id]
                
                if expired_sms_ids:
                    print(f"🧹 清理了 {len(expired_sms_ids)} 个过期的SMS记录")
                        
            except Exception as e:
                print(f"清理旧记录时出错: {str(e)}")
    
    async def cleanup_pending_sms_sends(self):
        """清理过期的待确认SMS发送请求"""
        while self.running:
            try:
                current_time = time.time()
                expired_sends = []
                
                for send_id, send_info in self.pending_sms_sends.items():
                    status = send_info.get('status')
                    age = current_time - send_info.get('timestamp', current_time)
                    completed_age = current_time - send_info.get('last_result_at', send_info.get('timestamp', current_time))
                    if status in ('queued', 'sent') and age > 600:
                        await self.send_message(
                            send_info['chat_id'],
                            f"⚠️ <b>短信发送未收到最终回执</b>\n\n"
                            f"设备 <code>{self.escape_html(send_info['device_id'])}</code>\n"
                            f"接收方 <code>{self.escape_html(send_info['phone_number'])}</code>\n"
                            f"等待 <code>{int(age)}秒</code>\n\n"
                            f"内容 <code>{self.escape_html(self.preview_text(send_info.get('content', '')))}</code>"
                        )
                        expired_sends.append(send_id)
                    elif status == 'completed' and completed_age > 60:
                        expired_sends.append(send_id)
                
                for send_id in expired_sends:
                    send_info = self.pending_sms_sends.pop(send_id)
                    print(f"🧹 清理过期的SMS发送请求: {send_id} (设备: {send_info['device_id']})")
                
                if expired_sends:
                    print(f"📊 清理了 {len(expired_sends)} 个过期的SMS发送请求")
                
                # 每5分钟清理一次
                await asyncio.sleep(300)
                
            except Exception as e:
                print(f"清理待确认SMS发送请求时出错: {str(e)}")
                await asyncio.sleep(60)

    def create_background_task(self, coro):
        """创建并跟踪后台任务，便于退出时统一取消。"""
        task = asyncio.create_task(coro)
        self.background_tasks.append(task)
        return task

    async def connect_telegram_with_retry(self):
        """连接Telegram，失败时按指数退避重试。"""
        retry_delay = max(1, Config.TELEGRAM_CONNECT_RETRY_MIN_SECONDS)
        max_delay = max(retry_delay, Config.TELEGRAM_CONNECT_RETRY_MAX_SECONDS)

        while self.running:
            try:
                if self.client.is_connected():
                    return True

                print("🔌 正在连接Telegram客户端...")
                await self.client.start(bot_token=self.bot_token)
                print("✅ Telethon客户端已连接")
                try:
                    await self.sync_bot_commands()
                except Exception as menu_error:
                    print(f"⚠️ 命令菜单同步失败: {type(menu_error).__name__}；下次连接重试")
                return True

            except Exception as e:
                print(f"❌ Telegram连接失败: {str(e)}")
                print(f"⏳ {retry_delay} 秒后重试Telegram连接...")

                for _ in range(retry_delay * 10):
                    if not self.running:
                        return False
                    await asyncio.sleep(0.1)

                retry_delay = min(max_delay, retry_delay * 2)

        return False

    async def run_telegram_until_stopped(self):
        """保持Telegram客户端运行；断开后自动重连。"""
        reconnect_delay = max(1, Config.TELEGRAM_CONNECT_RETRY_MIN_SECONDS)

        while self.running:
            connected = await self.connect_telegram_with_retry()
            if not connected:
                break

            disconnected = self.client.disconnected
            try:
                while self.running:
                    try:
                        await asyncio.wait_for(
                            asyncio.shield(disconnected),
                            timeout=1.0
                        )
                        break
                    except asyncio.TimeoutError:
                        continue

                if self.running:
                    print("⚠️ Telegram客户端已断开，将自动重连")

            except asyncio.CancelledError:
                raise
            except Exception as e:
                print(f"❌ Telegram客户端运行时出错: {str(e)}")

            finally:
                if not disconnected.done():
                    disconnected.cancel()
                try:
                    await disconnected
                except (asyncio.CancelledError, Exception):
                    pass

            if self.running:
                try:
                    await self.client.disconnect()
                except Exception as e:
                    print(f"⚠️ 断开旧Telegram连接时出错: {str(e)}")

                for _ in range(reconnect_delay * 10):
                    if not self.running:
                        break
                    await asyncio.sleep(0.1)
    
    async def start(self):
        """启动机器人"""
        print("🚀 开始启动 Asterisk Telegram Bot (Telethon版本)...")
        exit_code = 0
        
        try:
            self.running = True

            # 1. 连接Telethon客户端，网络不可用时持续重试
            if not await self.connect_telegram_with_retry():
                exit_code = 1
                self.running = False
                return exit_code
            
            # 2. 设置事件处理器
            if not self.event_handlers_ready:
                await self.setup_event_handlers()
                self.event_handlers_ready = True
                print("✅ 事件处理器已设置")

            # 3. 等待Asterisk/Quectel通道完成初始化
            if Config.ASTERISK_STARTUP_DELAY_SECONDS > 0:
                print(f"⏳ 等待Asterisk/Quectel初始化 {Config.ASTERISK_STARTUP_DELAY_SECONDS} 秒...")
                await asyncio.sleep(Config.ASTERISK_STARTUP_DELAY_SECONDS)
            
            # 4. 启动SMS管道监听任务
            self.create_background_task(self.sms_delivery_worker())
            self.create_background_task(self.sms_guard_watchdog())
            self.create_background_task(self.listen_sms_pipe())
            print("✅ SMS管道监听已启动")
            
            # 5. 启动Asterisk日志监控（带自动重启）
            self.create_background_task(self.monitor_asterisk_logs_with_restart())
            print("✅ Asterisk日志监控已启动")
            
            # 6. 启动Quectel手机号恢复任务
            self.create_background_task(self.phone_recovery_watchdog())
            print("✅ Quectel手机号恢复监控已启动")
            
            # 7. 启动清理任务
            self.create_background_task(self.cleanup_old_records())
            self.create_background_task(self.cleanup_pending_sms_sends())
            print("✅ 清理任务已启动")
            
            print("🎉 所有服务已启动完成，等待消息...")
            
            # 测试SMS日志解析功能
            print("🧪 测试SMS日志解析功能...")
            self.test_sms_log_parsing()
            
            await self.run_telegram_until_stopped()
            
        except KeyboardInterrupt:
            print("🛑 收到停止信号，正在关闭...")
            self.running = False
        except Exception as e:
            print(f"❌ 启动时出错: {str(e)}")
            self.running = False
            exit_code = 1
        finally:
            print("🧹 开始清理资源...")

            for task in self.background_tasks:
                if not task.done():
                    task.cancel()

            for task in self.background_tasks:
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                except Exception as e:
                    print(f"⚠️ 后台任务退出时出错: {str(e)}")

            # 清理所有子进程
            await self.cleanup_processes()
            
            # 断开Telegram客户端
            if self.client:
                try:
                    await self.client.disconnect()
                    print("✅ Telegram客户端已断开")
                except Exception as e:
                    print(f"⚠️ 断开Telegram客户端时出错: {str(e)}")
            
            print("✅ Bot已完全关闭")
            return exit_code


def main():
    """主函数"""
    try:
        bot = AsteriskBot()
        exit_code = asyncio.run(bot.start())
        sys.exit(exit_code)
    except KeyboardInterrupt:
        print("\n🛑 程序被用户中断")
        sys.exit(0)
    except Exception as e:
        print(f"❌ 程序运行出错: {str(e)}")
        sys.exit(1)


if __name__ == "__main__":
    main()
