#!/bin/bash

# Asterisk Telegram Bot 配置检查脚本
echo "=== Asterisk Telegram Bot 配置检查 ==="

PROJECT_DIR="$(dirname "$0")"
cd "$PROJECT_DIR"

echo "项目目录: $PROJECT_DIR"

# 检查Python环境
echo
echo "1. 检查Python环境..."
if [ -f "venv/bin/python" ]; then
    echo "✅ Python虚拟环境存在"
    PYTHON_CMD="./venv/bin/python"
else
    echo "⚠️  Python虚拟环境不存在，使用系统Python"
    PYTHON_CMD="python3"
fi

# 检查依赖
echo
echo "2. 检查Python依赖..."
$PYTHON_CMD -c "
import sys
missing_deps = []
try:
    import telethon
    print('✅ telethon 已安装')
except ImportError:
    missing_deps.append('telethon')

try:
    import dotenv
    print('✅ python-dotenv 已安装')
except ImportError:
    missing_deps.append('python-dotenv')

try:
    import aiofiles
    print('✅ aiofiles 已安装')
except ImportError:
    missing_deps.append('aiofiles')

try:
    import socks
    print('✅ PySocks 已安装')
except ImportError:
    missing_deps.append('PySocks')

if missing_deps:
    print('❌ 缺少依赖:', ', '.join(missing_deps))
    print('请运行: pip install', ' '.join(missing_deps))
    sys.exit(1)
else:
    print('✅ 所有依赖已安装')
"

# 检查配置文件
echo
echo "3. 检查配置文件..."
if [ ! -f ".env" ]; then
    echo "❌ .env 文件不存在"
    echo "请复制 config.example 到 .env 并配置"
    exit 1
fi

echo "✅ .env 文件存在"

# 测试配置加载
echo
echo "4. 测试配置加载..."
$PYTHON_CMD -c "
import sys
try:
    from config import Config
    
    print('检查 BOT_TOKEN...')
    if not Config.BOT_TOKEN or Config.BOT_TOKEN == '':
        print('❌ BOT_TOKEN 未配置')
        sys.exit(1)
    else:
        print('✅ BOT_TOKEN 已配置')
    
    print('检查 API_ID...')
    if not Config.API_ID or Config.API_ID == 0:
        print('❌ API_ID 未配置')
        sys.exit(1)
    else:
        print('✅ API_ID 已配置')
    
    print('检查 API_HASH...')
    if not Config.API_HASH or Config.API_HASH == '':
        print('❌ API_HASH 未配置')
        sys.exit(1)
    else:
        print('✅ API_HASH 已配置')
    
    print('检查 AUTHORIZED_USERS...')
    if not Config.AUTHORIZED_USERS:
        print('⚠️  AUTHORIZED_USERS 未配置（警告）')
    else:
        print('✅ AUTHORIZED_USERS 已配置:', len(Config.AUTHORIZED_USERS), '个用户')

    if Config.PROXY_TYPE:
        print('✅ 代理配置:', f'{Config.PROXY_TYPE}://{Config.PROXY_HOST}:{Config.PROXY_PORT}', 'rdns=' + str(Config.PROXY_RDNS))
    else:
        print('⚠️  未配置代理，将直连Telegram')

    print('✅ Quectel号码恢复命令:', Config.PHONEBOOK_PREF_COMMAND)
    print('✅ Quectel号码恢复间隔:', Config.PHONE_RECOVERY_INTERVAL_SECONDS, '秒')
    print('✅ Telegram重连退避:', Config.TELEGRAM_CONNECT_RETRY_MIN_SECONDS, '-', Config.TELEGRAM_CONNECT_RETRY_MAX_SECONDS, '秒')
    print('✅ SMS管道路径:', Config.SMS_PIPE_PATH)
    if Config.PHONE_RECOVERY_EXPECTED_NUMBERS:
        print('✅ 已知问题号码:', ', '.join(Config.PHONE_RECOVERY_EXPECTED_NUMBERS))
    
    print('✅ 配置检查通过')
    
except Exception as e:
    print('❌ 配置加载失败:', e)
    sys.exit(1)
"

# 检查Asterisk环境
echo
echo "5. 检查Asterisk环境..."
if [ -d "/var/log/asterisk" ]; then
    echo "✅ Asterisk日志目录存在"
    
    if [ -f "/var/log/asterisk/messages.log" ]; then
        echo "✅ Asterisk messages.log 存在"
        if [ -r "/var/log/asterisk/messages.log" ]; then
            echo "✅ messages.log 可读"
        else
            echo "⚠️  messages.log 不可读，可能需要调整权限"
        fi
    else
        echo "⚠️  Asterisk messages.log 不存在"
    fi
else
    echo "⚠️  Asterisk日志目录不存在"
fi

# 检查SMS管道
echo
echo "6. 检查SMS管道..."
SMS_PIPE="$($PYTHON_CMD -c 'from config import Config; print(Config.SMS_PIPE_PATH)' 2>/dev/null)"
if [ -z "$SMS_PIPE" ]; then
    SMS_PIPE="/tmp/asterisk_sms_pipe"
fi
echo "SMS管道路径: $SMS_PIPE"
if [ -p "$SMS_PIPE" ]; then
    echo "✅ SMS管道已存在"
elif [ -e "$SMS_PIPE" ]; then
    echo "⚠️  SMS管道存在但不是命名管道"
else
    echo "ℹ️  SMS管道不存在（将在运行时创建）"
fi

# 检查systemd服务
echo
echo "7. 检查systemd服务..."
if systemctl list-unit-files | grep -q "asterisk-telegram-bot@.service"; then
    echo "✅ systemd服务已安装"
    
    # 检查当前用户的服务状态
    CURRENT_USER=${SUDO_USER:-$USER}
    if [ -n "$CURRENT_USER" ]; then
        SERVICE_NAME="asterisk-telegram-bot@$CURRENT_USER.service"
        if systemctl is-enabled --quiet "$SERVICE_NAME" 2>/dev/null; then
            echo "✅ 服务已启用: $SERVICE_NAME"
            if systemctl is-active --quiet "$SERVICE_NAME" 2>/dev/null; then
                echo "✅ 服务正在运行"
            else
                echo "⚠️  服务已启用但未运行"
            fi
        else
            echo "ℹ️  服务未启用: $SERVICE_NAME"
        fi
    fi
else
    echo "ℹ️  systemd服务未安装"
fi

echo
echo "=== 配置检查完成 ==="
echo "如果所有检查都通过，可以使用以下命令启动服务:"
echo "  sudo ./install_service_linux.sh  # 完整安装"
echo "  sudo ./quick_install.sh          # 快速安装"
