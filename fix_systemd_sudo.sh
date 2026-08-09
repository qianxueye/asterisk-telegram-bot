#!/bin/bash

# 修复 systemd 服务以支持 sudo
set -e

echo "=== 修复 systemd 服务以支持 sudo ==="
echo ""

# 检查是否以root权限运行
if [ "$EUID" -ne 0 ]; then
    echo "❌ 请以root权限运行此脚本 (sudo $0)"
    exit 1
fi

# 获取实际用户
if [ -n "$SUDO_USER" ]; then
    REAL_USER=$SUDO_USER
else
    echo "请输入服务运行的用户名:"
    read REAL_USER
fi

echo "将为用户 $REAL_USER 配置 sudo 支持"
echo ""

# 1. 配置 sudoers 允许无密码执行 asterisk
echo "1. 配置 sudoers..."
SUDOERS_FILE="/etc/sudoers.d/asterisk-bot-${REAL_USER}"

# 查找 asterisk 命令路径
ASTERISK_PATHS=$(which -a asterisk 2>/dev/null || echo "/usr/sbin/asterisk /usr/bin/asterisk")

cat > "$SUDOERS_FILE" <<EOF
# Asterisk Telegram Bot - 允许用户无密码执行 asterisk 命令
# 创建时间: $(date)
# 用户: $REAL_USER

# 允许执行 asterisk 命令
EOF

for path in $ASTERISK_PATHS; do
    if [ -f "$path" ]; then
        echo "$REAL_USER ALL=(ALL) NOPASSWD: $path" >> "$SUDOERS_FILE"
    fi
done

# 添加常见路径
cat >> "$SUDOERS_FILE" <<EOF
$REAL_USER ALL=(ALL) NOPASSWD: /usr/sbin/asterisk
$REAL_USER ALL=(ALL) NOPASSWD: /usr/bin/asterisk
$REAL_USER ALL=(ALL) NOPASSWD: /usr/local/sbin/asterisk
EOF

chmod 0440 "$SUDOERS_FILE"

# 验证语法
if visudo -c -f "$SUDOERS_FILE" > /dev/null 2>&1; then
    echo "✅ sudoers 配置已创建: $SUDOERS_FILE"
else
    echo "❌ sudoers 配置失败"
    rm -f "$SUDOERS_FILE"
    exit 1
fi

echo ""

# 2. 修改 systemd 服务文件
echo "2. 修改 systemd 服务配置..."
SERVICE_NAME="asterisk-telegram-bot-${REAL_USER}.service"
SERVICE_FILE="/etc/systemd/system/$SERVICE_NAME"

if [ -f "$SERVICE_FILE" ]; then
    # 备份原文件
    cp "$SERVICE_FILE" "${SERVICE_FILE}.backup.$(date +%Y%m%d%H%M%S)"
    
    # 修改 NoNewPrivileges
    sed -i 's/^NoNewPrivileges=true/NoNewPrivileges=false/' "$SERVICE_FILE"
    
    echo "✅ 服务文件已修改"
    echo "   NoNewPrivileges: true -> false"
else
    echo "⚠️  服务文件不存在: $SERVICE_FILE"
    echo "   请先运行: sudo ./install_service_linux.sh"
fi

echo ""

# 3. 更新 .env 配置
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
ENV_FILE="$SCRIPT_DIR/.env"

if [ -f "$ENV_FILE" ]; then
    echo "3. 更新 .env 配置..."
    
    # 备份
    cp "$ENV_FILE" "$ENV_FILE.backup.$(date +%Y%m%d%H%M%S)"
    
    # 确保使用 sudo
    if grep -q "^ASTERISK_COMMAND_PREFIX=" "$ENV_FILE"; then
        sed -i.bak 's|^ASTERISK_COMMAND_PREFIX=.*|ASTERISK_COMMAND_PREFIX=sudo asterisk -rx|' "$ENV_FILE"
    else
        echo "ASTERISK_COMMAND_PREFIX=sudo asterisk -rx" >> "$ENV_FILE"
    fi
    
    echo "✅ .env 已配置为使用 sudo"
else
    echo "⚠️  未找到 .env 文件"
fi

echo ""

# 4. 重新加载 systemd
echo "4. 重新加载 systemd..."
systemctl daemon-reload
echo "✅ systemd 已重新加载"

echo ""

# 5. 测试配置
echo "5. 测试 sudo 配置..."
if su - "$REAL_USER" -c "sudo -n asterisk -rx 'core show version'" 2>&1 | grep -q "Asterisk"; then
    echo "✅ sudo 命令测试成功（无需密码）"
else
    echo "⚠️  sudo 命令测试失败"
    echo "   请手动测试: sudo asterisk -rx 'core show version'"
fi

echo ""
echo "=== 配置完成 ==="
echo "✅ sudoers 已配置"
echo "✅ systemd 服务已修改"
echo "✅ .env 已更新"
echo ""
echo "现在需要重启服务："
echo "  sudo systemctl restart $SERVICE_NAME"
echo ""
echo "查看服务状态："
echo "  sudo systemctl status $SERVICE_NAME"
echo ""
echo "查看服务日志："
echo "  sudo journalctl -u $SERVICE_NAME -f"
echo ""
echo "⚠️  注意事项："
echo "  - NoNewPrivileges 已设置为 false，这会降低一些安全性"
echo "  - 但这是在 systemd 服务中使用 sudo 的必要配置"
echo "  - 服务仍然受到其他 systemd 安全限制的保护"

