#!/bin/bash

# Asterisk Telegram Bot 快速安装脚本
set -e

echo "=== Asterisk Telegram Bot 快速安装脚本 ==="

# 检查是否以root权限运行
if [ "$EUID" -ne 0 ]; then
    echo "❌ 请以root权限运行此脚本 (sudo $0)"
    exit 1
fi

# 获取实际用户
if [ -n "$SUDO_USER" ]; then
    REAL_USER=$SUDO_USER
else
    echo "请输入要运行服务的用户名:"
    read REAL_USER
fi

echo "为用户 $REAL_USER 快速安装服务"

USER_HOME=$(eval echo ~$REAL_USER)
PROJECT_DIR="$USER_HOME/Asterisk-Telegram-Bot"

# 基本检查
if [ ! -d "$PROJECT_DIR" ]; then
    echo "❌ 项目目录不存在: $PROJECT_DIR"
    exit 1
fi

if [ ! -f "$PROJECT_DIR/asterisk_bot.py" ]; then
    echo "❌ 找不到 asterisk_bot.py"
    exit 1
fi

if [ ! -f "$PROJECT_DIR/.env" ]; then
    echo "❌ 找不到 .env 文件，请先配置"
    exit 1
fi

# 检查虚拟环境，如果不存在则创建
if [ ! -e "$PROJECT_DIR/venv/bin/python" ]; then
    echo "⚠️  Python虚拟环境不存在，正在创建..."
    sudo -u "$REAL_USER" bash -c "cd $PROJECT_DIR && python3 -m venv venv"
    if [ $? -eq 0 ]; then
        echo "✅ Python虚拟环境创建成功"
    else
        echo "❌ Python虚拟环境创建失败"
        exit 1
    fi
fi

# 检查并安装依赖
echo "检查Python依赖..."
DEPENDENCY_CHECK_RESULT=$(sudo -u "$REAL_USER" bash -c "cd $PROJECT_DIR && ./venv/bin/python -c \"
import sys
try:
    import telethon, dotenv, aiofiles
    print('SUCCESS')
except ImportError as e:
    print('MISSING:', str(e))
\"" 2>&1)

if echo "$DEPENDENCY_CHECK_RESULT" | grep -q "SUCCESS"; then
    echo "✅ 主要依赖已安装"
else
    echo "⚠️  缺少依赖，正在安装..."
    echo "详细信息: $DEPENDENCY_CHECK_RESULT"
    sudo -u "$REAL_USER" bash -c "cd $PROJECT_DIR && ./venv/bin/pip install -r requirements.txt"
    if [ $? -eq 0 ]; then
        echo "✅ 依赖安装成功"
    else
        echo "❌ 依赖安装失败"
        exit 1
    fi
fi

# 停止现有服务
echo "停止现有服务..."
systemctl stop "asterisk-telegram-bot@$REAL_USER.service" 2>/dev/null || true

# 安装服务文件
echo "安装服务..."
cp "$PROJECT_DIR/systemd/asterisk-telegram-bot-simple.service" "/etc/systemd/system/asterisk-telegram-bot@.service"
chown root:root "/etc/systemd/system/asterisk-telegram-bot@.service"
chmod 644 "/etc/systemd/system/asterisk-telegram-bot@.service"

# 重新加载并启动
echo "启动服务..."
systemctl daemon-reload
systemctl enable "asterisk-telegram-bot@$REAL_USER.service"
systemctl start "asterisk-telegram-bot@$REAL_USER.service"

# 检查状态
sleep 3
if systemctl is-active --quiet "asterisk-telegram-bot@$REAL_USER.service"; then
    echo "✅ 服务安装并启动成功！"
    echo
    echo "服务名称: asterisk-telegram-bot@$REAL_USER.service"
    echo "查看状态: sudo systemctl status asterisk-telegram-bot@$REAL_USER.service"
    echo "查看日志: sudo journalctl -u asterisk-telegram-bot@$REAL_USER.service -f"
else
    echo "❌ 服务启动失败，请检查日志:"
    echo "sudo journalctl -u asterisk-telegram-bot@$REAL_USER.service -n 20"
    exit 1
fi
