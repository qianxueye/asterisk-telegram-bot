#!/bin/bash

# Asterisk Telegram Bot Linux服务器部署脚本
set -e

echo "=== Asterisk Telegram Bot Linux服务器部署脚本 ==="

# 检查是否以root权限运行
if [ "$EUID" -ne 0 ]; then
    echo "❌ 请以root权限运行此脚本 (sudo $0)"
    exit 1
fi

# 获取实际用户（不是root）
if [ -n "$SUDO_USER" ]; then
    REAL_USER=$SUDO_USER
else
    echo "请输入要运行服务的用户名:"
    read REAL_USER
fi

echo "将为用户 $REAL_USER 安装服务"

# 获取当前脚本所在的目录作为项目目录（绝对路径）
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
PROJECT_DIR="$SCRIPT_DIR"

echo "项目目录: $PROJECT_DIR"

# 配置 sudoers 以允许无密码执行 asterisk 命令
echo "配置 sudo 权限..."
if command -v asterisk &> /dev/null; then
    SUDOERS_FILE="/etc/sudoers.d/asterisk-bot-${REAL_USER}"
    
    if [ ! -f "$SUDOERS_FILE" ]; then
        echo "创建 sudoers 配置..."
        
        # 查找 asterisk 路径
        ASTERISK_PATH=$(which asterisk 2>/dev/null || echo "/usr/sbin/asterisk")
        
        cat > "$SUDOERS_FILE" <<EOF
# Asterisk Telegram Bot - 允许用户无密码执行 asterisk 命令
$REAL_USER ALL=(ALL) NOPASSWD: $ASTERISK_PATH
$REAL_USER ALL=(ALL) NOPASSWD: /usr/sbin/asterisk
$REAL_USER ALL=(ALL) NOPASSWD: /usr/bin/asterisk
$REAL_USER ALL=(ALL) NOPASSWD: /usr/local/sbin/asterisk
EOF
        
        chmod 0440 "$SUDOERS_FILE"
        
        if visudo -c -f "$SUDOERS_FILE" > /dev/null 2>&1; then
            echo "✅ sudoers 配置已创建"
        else
            echo "❌ sudoers 配置失败"
            rm -f "$SUDOERS_FILE"
        fi
    else
        echo "✅ sudoers 配置已存在"
    fi
else
    echo "⚠️  未检测到asterisk命令，请确保Asterisk已安装"
fi

# 检查项目目录是否存在
if [ ! -d "$PROJECT_DIR" ]; then
    echo "❌ 项目目录不存在: $PROJECT_DIR"
    exit 1
fi

# 检查必要文件
echo "检查必要文件..."
if [ ! -e "$PROJECT_DIR/asterisk_bot.py" ]; then
    echo "❌ 缺少必要文件: asterisk_bot.py"
    exit 1
fi

if [ ! -e "$PROJECT_DIR/.env" ]; then
    echo "❌ 缺少必要文件: .env"
    echo "请创建 .env 文件，参考 config.example"
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
else
    echo "✅ Python虚拟环境已存在"
fi

echo "✅ 所有必要文件都存在"

# 检查并安装Python依赖
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
    echo "⚠️  依赖检查失败，正在安装依赖..."
    echo "详细信息: $DEPENDENCY_CHECK_RESULT"
    
    # 安装依赖
    sudo -u "$REAL_USER" bash -c "cd $PROJECT_DIR && ./venv/bin/pip install -r requirements.txt"
    if [ $? -eq 0 ]; then
        echo "✅ 依赖安装成功"
        
        # 重新检查依赖
        DEPENDENCY_CHECK_RESULT=$(sudo -u "$REAL_USER" bash -c "cd $PROJECT_DIR && ./venv/bin/python -c \"
import sys
try:
    import telethon, dotenv, aiofiles
    print('SUCCESS')
except ImportError as e:
    print('MISSING:', str(e))
\"" 2>&1)
        
        if echo "$DEPENDENCY_CHECK_RESULT" | grep -q "SUCCESS"; then
            echo "✅ 依赖安装后检查通过"
        else
            echo "❌ 依赖安装后仍然失败: $DEPENDENCY_CHECK_RESULT"
            exit 1
        fi
    else
        echo "❌ 依赖安装失败"
        exit 1
    fi
fi

# 测试配置文件
echo "测试配置文件..."
sudo -u "$REAL_USER" bash -c "cd $PROJECT_DIR && ./venv/bin/python -c \"
import sys
try:
    from config import Config
    if not Config.BOT_TOKEN or Config.BOT_TOKEN == '':
        print('❌ BOT_TOKEN未正确配置')
        sys.exit(1)
    if not Config.API_ID or Config.API_ID == 0:
        print('❌ API_ID未正确配置')
        sys.exit(1)
    if not Config.API_HASH or Config.API_HASH == '':
        print('❌ API_HASH未正确配置')
        sys.exit(1)
    if not Config.AUTHORIZED_USERS:
        print('⚠️  警告: 未配置任何授权用户')
    print('✅ 配置文件检查通过')
except Exception as e:
    print('❌ 配置文件错误:', e)
    sys.exit(1)
\""

# 检查Asterisk相关
echo "检查Asterisk环境..."
if [ ! -d "/var/log/asterisk" ]; then
    echo "⚠️  警告: Asterisk日志目录不存在，请确保Asterisk已安装"
fi

# 检查SMS管道权限
echo "检查SMS管道权限..."
sudo -u "$REAL_USER" bash -c "cd $PROJECT_DIR && ./venv/bin/python -c \"
import os
sms_pipe = '/tmp/asterisk_sms_pipe'
if os.path.exists(sms_pipe):
    print('✅ SMS管道已存在')
else:
    print('ℹ️  SMS管道不存在，将在运行时创建')
\""

# 停止现有服务（如果存在）
SERVICE_NAME="asterisk-telegram-bot-${REAL_USER}.service"
echo "停止现有服务..."
systemctl stop "$SERVICE_NAME" 2>/dev/null || true
systemctl disable "$SERVICE_NAME" 2>/dev/null || true

# 动态生成服务文件（使用实际的项目路径）
echo "生成systemd服务文件..."
cat > "/etc/systemd/system/$SERVICE_NAME" <<EOF
[Unit]
Description=Asterisk Telegram Bot with Telethon Integration
After=network.target network-online.target
Wants=network-online.target
After=systemd-resolved.service
Wants=systemd-resolved.service

[Service]
Type=simple
User=$REAL_USER
Group=$REAL_USER
WorkingDirectory=$PROJECT_DIR

# 环境变量设置
Environment=PATH=$PROJECT_DIR/venv/bin:/usr/local/bin:/usr/bin:/bin:/sbin:/usr/sbin
Environment=PYTHONPATH=$PROJECT_DIR
Environment=PYTHONUNBUFFERED=1

# 启动前检查
ExecStartPre=/bin/bash -c 'cd $PROJECT_DIR && test -f .env || (echo "错误: .env文件不存在" && exit 1)'
ExecStartPre=/bin/bash -c 'cd $PROJECT_DIR && test -x venv/bin/python || (echo "错误: Python虚拟环境不可执行" && exit 1)'
ExecStartPre=/bin/bash -c 'cd $PROJECT_DIR && venv/bin/python -c "import asterisk_bot, config" || (echo "错误: 项目模块导入失败" && exit 1)'

# 启动命令
ExecStart=$PROJECT_DIR/venv/bin/python $PROJECT_DIR/asterisk_bot.py

# 重启配置
Restart=on-failure
RestartSec=60
StartLimitIntervalSec=1800
StartLimitBurst=5

# 超时配置
TimeoutStartSec=180
TimeoutStopSec=60

# 日志配置
StandardOutput=journal
StandardError=journal
SyslogIdentifier=asterisk-telegram-bot

# 环境变量文件
EnvironmentFile=-$PROJECT_DIR/.env

# 安全配置
# NoNewPrivileges 设为 false 以允许 sudo 命令
# 注意: 这会降低一些安全性，但对于访问 Asterisk 是必需的
NoNewPrivileges=false
PrivateTmp=false
ProtectSystem=false
ProtectHome=false

# 允许访问Asterisk日志和SMS管道
ReadWritePaths=/var/log/asterisk
ReadWritePaths=/tmp

# 资源限制
MemoryMax=512M
TasksMax=50

[Install]
WantedBy=multi-user.target
EOF

# 设置正确权限
chown root:root "/etc/systemd/system/$SERVICE_NAME"
chmod 644 "/etc/systemd/system/$SERVICE_NAME"

# 重新加载systemd
echo "重新加载systemd配置..."
systemctl daemon-reload

# 启用服务
echo "启用服务..."
systemctl enable "$SERVICE_NAME"

# 测试服务启动
echo "测试服务启动..."
if systemctl start "$SERVICE_NAME"; then
    echo "✅ 服务启动成功"
    
    # 等待几秒钟检查状态
    sleep 5
    
    if systemctl is-active --quiet "$SERVICE_NAME"; then
        echo "✅ 服务运行正常"
        echo
        echo "=== 服务安装完成 ==="
        echo "服务名称: $SERVICE_NAME"
        echo "项目目录: $PROJECT_DIR"
        echo "运行用户: $REAL_USER"
        echo
        echo "常用命令:"
        echo "  查看状态: sudo systemctl status $SERVICE_NAME"
        echo "  查看日志: sudo journalctl -u $SERVICE_NAME -f"
        echo "  重启服务: sudo systemctl restart $SERVICE_NAME"
        echo "  停止服务: sudo systemctl stop $SERVICE_NAME"
        echo "  禁用服务: sudo systemctl disable $SERVICE_NAME"
        echo
        echo "配置说明:"
        echo "  1. 确保 .env 文件包含正确的 BOT_TOKEN, API_ID, API_HASH"
        echo "  2. 确保 AUTHORIZED_USERS 包含授权用户ID"
        echo "  3. 确保 Asterisk 服务正在运行"
        echo "  4. 确保 /var/log/asterisk/messages.log 文件存在且可读"
        echo
        echo "⚠️  重要提示:"
        echo "  - 服务已配置为使用 sudo 执行 asterisk 命令"
        echo "  - NoNewPrivileges 已设置为 false 以允许 sudo"
        echo "  - sudoers 已配置为允许无密码执行"
        echo ""
        echo "  如果仍遇到权限问题，请运行:"
        echo "  sudo ./fix_systemd_sudo.sh"
        echo ""
        echo "  诊断权限问题:"
        echo "  sudo ./diagnose_asterisk.sh"
    else
        echo "⚠️  服务已启动但可能有问题，请检查日志:"
        echo "sudo journalctl -u $SERVICE_NAME -n 20"
    fi
else
    echo "❌ 服务启动失败，请检查日志:"
    echo "sudo journalctl -u $SERVICE_NAME -n 20"
    exit 1
fi
