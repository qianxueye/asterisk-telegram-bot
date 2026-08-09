#!/bin/bash

# Asterisk Socket 权限配置脚本
# 配置 Asterisk socket 权限，允许普通用户直接访问
set -e

echo "=== Asterisk Socket 权限配置脚本 ==="

# 检查是否以root权限运行
if [ "$EUID" -ne 0 ]; then
    echo "❌ 请以root权限运行此脚本 (sudo $0)"
    exit 1
fi

# 获取实际用户
if [ -n "$SUDO_USER" ]; then
    REAL_USER=$SUDO_USER
else
    echo "请输入要配置权限的用户名:"
    read REAL_USER
fi

echo "将为用户 $REAL_USER 配置 Asterisk socket 权限"

# 检查Asterisk是否安装
if ! command -v asterisk &> /dev/null; then
    echo "❌ 未检测到asterisk命令，请先安装Asterisk"
    exit 1
fi

echo "✅ 检测到Asterisk"

# 1. 查找 Asterisk socket 文件
echo ""
echo "查找 Asterisk socket 文件..."
SOCKET_PATHS=(
    "/var/run/asterisk/asterisk.ctl"
    "/run/asterisk/asterisk.ctl"
    "/tmp/asterisk/asterisk.ctl"
)

SOCKET_FILE=""
for socket in "${SOCKET_PATHS[@]}"; do
    if [ -S "$socket" ]; then
        SOCKET_FILE="$socket"
        echo "✅ 找到 socket: $socket"
        break
    fi
done

if [ -z "$SOCKET_FILE" ]; then
    echo "⚠️  未找到 Asterisk socket 文件"
    echo "可能的原因："
    echo "  1. Asterisk 未运行"
    echo "  2. Socket 在非标准位置"
    
    # 尝试从进程信息获取socket位置
    if pgrep asterisk > /dev/null; then
        echo ""
        echo "Asterisk 正在运行，尝试从配置获取socket位置..."
        if [ -f "/etc/asterisk/asterisk.conf" ]; then
            ASTRUNDIR=$(grep -E "^astrundir\s*=>" /etc/asterisk/asterisk.conf | awk '{print $3}' | tr -d ' ')
            if [ -n "$ASTRUNDIR" ]; then
                SOCKET_FILE="${ASTRUNDIR}/asterisk.ctl"
                echo "从配置获取: $SOCKET_FILE"
            fi
        fi
    else
        echo ""
        echo "Asterisk 未运行，请先启动："
        echo "  sudo systemctl start asterisk"
        exit 1
    fi
fi

# 2. 配置 socket 权限
if [ -n "$SOCKET_FILE" ] && [ -S "$SOCKET_FILE" ]; then
    echo ""
    echo "配置 socket 权限..."
    
    # 方案：设置 socket 为 o+rw（所有用户可读写）
    chmod o+rw "$SOCKET_FILE"
    
    # 同时配置目录权限
    SOCKET_DIR=$(dirname "$SOCKET_FILE")
    chmod o+rx "$SOCKET_DIR"
    
    echo "✅ Socket 权限已配置"
    echo "   文件: $SOCKET_FILE"
    echo "   权限: $(ls -l $SOCKET_FILE)"
    echo "   目录: $SOCKET_DIR"
    echo "   权限: $(ls -ld $SOCKET_DIR)"
else
    echo "❌ 无法配置 socket 权限"
fi

# 3. 配置 Asterisk 使socket权限持久化
echo ""
echo "配置 Asterisk 使权限持久化..."

ASTERISK_CONF="/etc/asterisk/asterisk.conf"
if [ -f "$ASTERISK_CONF" ]; then
    # 备份配置文件
    cp "$ASTERISK_CONF" "${ASTERISK_CONF}.backup.$(date +%Y%m%d%H%M%S)"
    
    # 检查是否已有配置
    if grep -q "^astctlpermissions\s*=" "$ASTERISK_CONF"; then
        echo "⚠️  astctlpermissions 已配置"
        CURRENT_PERM=$(grep "^astctlpermissions\s*=" "$ASTERISK_CONF" | awk '{print $3}')
        echo "   当前值: $CURRENT_PERM"
        
        if [ "$CURRENT_PERM" != "0777" ]; then
            echo "   正在更新为 0777..."
            sed -i.bak 's/^astctlpermissions\s*=.*/astctlpermissions = 0777/' "$ASTERISK_CONF"
            echo "✅ 已更新配置"
        fi
    else
        # 在 [options] 段添加配置
        if grep -q "^\[options\]" "$ASTERISK_CONF"; then
            sed -i.bak '/^\[options\]/a astctlpermissions = 0777' "$ASTERISK_CONF"
            echo "✅ 已添加 astctlpermissions = 0777"
        else
            # 如果没有 [options] 段，在文件开头添加
            sed -i.bak '1i[options]\nastctlpermissions = 0777\n' "$ASTERISK_CONF"
            echo "✅ 已创建 [options] 段并添加配置"
        fi
    fi
    
    echo "✅ Asterisk 配置已更新"
    echo "📝 原配置已备份到: ${ASTERISK_CONF}.backup.*"
else
    echo "⚠️  未找到 Asterisk 配置文件: $ASTERISK_CONF"
fi

# 4. 更新 .env 配置
echo ""
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
ENV_FILE="$SCRIPT_DIR/.env"

if [ -f "$ENV_FILE" ]; then
    echo "更新 .env 配置..."
    
    # 备份原文件
    cp "$ENV_FILE" "$ENV_FILE.backup.$(date +%Y%m%d%H%M%S)"
    
    # 更新配置为不使用sudo
    if grep -q "^ASTERISK_COMMAND_PREFIX=" "$ENV_FILE"; then
        sed -i.bak 's|^ASTERISK_COMMAND_PREFIX=.*|ASTERISK_COMMAND_PREFIX=asterisk -rx|' "$ENV_FILE"
    else
        echo "ASTERISK_COMMAND_PREFIX=asterisk -rx" >> "$ENV_FILE"
    fi
    
    echo "✅ .env 已配置为: ASTERISK_COMMAND_PREFIX=asterisk -rx"
    echo "📝 原配置已备份"
else
    echo "⚠️  未找到 .env 文件"
    echo "请确保 .env 文件包含: ASTERISK_COMMAND_PREFIX=asterisk -rx"
fi

# 5. 提示重启 Asterisk
echo ""
echo "=== 配置完成 ==="
echo "✅ Socket 权限已配置"
echo "✅ Asterisk 配置已更新"
echo "✅ .env 配置已更新"
echo ""
echo "⚠️  重要: 需要重启 Asterisk 使配置生效"
echo "   sudo systemctl restart asterisk"
echo ""
echo "🔍 验证配置："
echo "   1. 重启 Asterisk 后，检查 socket 权限："
echo "      ls -la $SOCKET_FILE"
echo "      # 应该显示所有用户可读写"
echo ""
echo "   2. 测试命令（不需要 sudo）："
echo "      asterisk -rx 'core show version'"
echo "      asterisk -rx 'quectel show devices'"
echo ""
echo "   3. 如果测试成功，重启 Bot 服务："
echo "      sudo systemctl restart asterisk-telegram-bot-${REAL_USER}.service"
echo ""
echo "   4. 查看 Bot 服务状态："
echo "      sudo systemctl status asterisk-telegram-bot-${REAL_USER}.service"
echo "      sudo journalctl -u asterisk-telegram-bot-${REAL_USER}.service -f"
echo ""
echo "📖 技术说明："
echo "   通过设置 astctlpermissions = 0777，Asterisk 会创建"
echo "   所有用户可访问的 socket 文件，这样普通用户就可以"
echo "   直接执行 asterisk -rx 命令，无需 sudo 或特殊权限。"

