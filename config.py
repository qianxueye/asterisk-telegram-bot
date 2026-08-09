import os
from dotenv import load_dotenv

# 加载环境变量
load_dotenv()

class Config:
    # Telegram Bot配置
    BOT_TOKEN = os.getenv('BOT_TOKEN', '')
    
    # Telethon API配置
    API_ID = int(os.getenv('API_ID', '0'))
    API_HASH = os.getenv('API_HASH', '')
    
    # 授权用户列表
    AUTHORIZED_USERS = set()
    auth_users_str = os.getenv('AUTHORIZED_USERS', '')
    if auth_users_str:
        AUTHORIZED_USERS = set(int(user_id.strip()) for user_id in auth_users_str.split(',') if user_id.strip())
    
    # Asterisk配置
    ASTERISK_COMMAND_PREFIX = os.getenv('ASTERISK_COMMAND_PREFIX', 'sudo asterisk -rx')
    ASTERISK_STARTUP_DELAY_SECONDS = int(os.getenv('ASTERISK_STARTUP_DELAY_SECONDS', '5'))
    ASTERISK_LOG_FILE = os.getenv('ASTERISK_LOG_FILE', '')
    SMS_PIPE_PATH = os.getenv('SMS_PIPE_PATH', '/tmp/asterisk_sms_pipe')

    # Telegram网络恢复配置
    TELEGRAM_CONNECT_RETRY_MIN_SECONDS = int(os.getenv('TELEGRAM_CONNECT_RETRY_MIN_SECONDS', '5'))
    TELEGRAM_CONNECT_RETRY_MAX_SECONDS = int(os.getenv('TELEGRAM_CONNECT_RETRY_MAX_SECONDS', '300'))
    
    # 代理配置
    PROXY_TYPE = os.getenv('PROXY_TYPE', '')  # http, socks4, socks5
    PROXY_HOST = os.getenv('PROXY_HOST', '')
    PROXY_PORT = int(os.getenv('PROXY_PORT', '0'))
    PROXY_USERNAME = os.getenv('PROXY_USERNAME', '')
    PROXY_PASSWORD = os.getenv('PROXY_PASSWORD', '')
    PROXY_RDNS = os.getenv('PROXY_RDNS', 'true').lower() in ('1', 'true', 'yes', 'on')

    # Quectel号码恢复配置
    PHONEBOOK_PREF_COMMAND = os.getenv('PHONEBOOK_PREF_COMMAND', 'AT$QCPBMPREF=1')
    PHONE_RECOVERY_INTERVAL_SECONDS = int(os.getenv('PHONE_RECOVERY_INTERVAL_SECONDS', '300'))
    PHONE_RECOVERY_NOTICE_INTERVAL_SECONDS = int(os.getenv('PHONE_RECOVERY_NOTICE_INTERVAL_SECONDS', '1800'))
    PHONE_RECOVERY_EXPECTED_NUMBERS = [
        number.strip()
        for number in os.getenv('PHONE_RECOVERY_EXPECTED_NUMBERS', '').split(',')
        if number.strip()
    ]

    # Quectel UAC 故障触发软恢复。默认仅保护 quectel1，绝不自动重置 modem。
    UAC_AUTO_RECOVERY_ENABLED = os.getenv('UAC_AUTO_RECOVERY_ENABLED', 'true').lower() in ('1', 'true', 'yes', 'on')
    UAC_AUTO_RECOVERY_DEVICES = {
        device.strip() for device in os.getenv('UAC_AUTO_RECOVERY_DEVICES', 'quectel1').split(',')
        if device.strip()
    }
    UAC_ERROR_THRESHOLD = int(os.getenv('UAC_ERROR_THRESHOLD', '25'))
    UAC_ERROR_WINDOW_SECONDS = int(os.getenv('UAC_ERROR_WINDOW_SECONDS', '5'))
    UAC_RECOVERY_COOLDOWN_SECONDS = int(os.getenv('UAC_RECOVERY_COOLDOWN_SECONDS', '3600'))
    UAC_RECOVERY_MAX_PER_DAY = int(os.getenv('UAC_RECOVERY_MAX_PER_DAY', '2'))
    UAC_RECOVERY_STATE_FILE = os.getenv(
        'UAC_RECOVERY_STATE_FILE',
        os.path.join(os.path.dirname(os.path.abspath(__file__)), '.uac_recovery_state.json')
    )
    UAC_RECOVERY_DRY_RUN = os.getenv('UAC_RECOVERY_DRY_RUN', 'false').lower() in ('1', 'true', 'yes', 'on')

    
    @classmethod
    def is_authorized(cls, user_id):
        """检查用户是否已授权"""
        return user_id in cls.AUTHORIZED_USERS
    
    @classmethod
    def add_user(cls, user_id):
        """添加授权用户"""
        cls.AUTHORIZED_USERS.add(user_id)
    
    @classmethod
    def remove_user(cls, user_id):
        """移除授权用户"""
        cls.AUTHORIZED_USERS.discard(user_id)
