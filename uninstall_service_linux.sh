#!/bin/bash

# Asterisk Telegram Bot Linux服务卸载脚本
set -e

echo "=== Asterisk Telegram Bot Linux服务卸载脚本 ==="

# 检查是否以root权限运行
if [ "$EUID" -ne 0 ]; then
    echo "❌ 请以root权限运行此脚本 (sudo $0)"
    exit 1
fi

# 获取实际用户（不是root）
if [ -n "$SUDO_USER" ]; then
    REAL_USER=$SUDO_USER
else
    echo "请输入服务运行的用户名:"
    read REAL_USER
fi

SERVICE_NAME="asterisk-telegram-bot-${REAL_USER}.service"

echo "将卸载服务: $SERVICE_NAME"
echo "按 Ctrl+C 取消，或按任意键继续..."
read -n 1 -s

# 1. 停止服务
echo "停止服务..."
if systemctl is-active --quiet "$SERVICE_NAME"; then
    systemctl stop "$SERVICE_NAME"
    echo "✅ 服务已停止"
else
    echo "ℹ️  服务未运行"
fi

# 2. 禁用服务
echo "禁用服务..."
if systemctl is-enabled --quiet "$SERVICE_NAME" 2>/dev/null; then
    systemctl disable "$SERVICE_NAME"
    echo "✅ 服务已禁用"
else
    echo "ℹ️  服务未启用"
fi

# 3. 删除服务文件
echo "删除服务文件..."
SERVICE_FILE="/etc/systemd/system/$SERVICE_NAME"
if [ -f "$SERVICE_FILE" ]; then
    rm "$SERVICE_FILE"
    echo "✅ 服务文件已删除: $SERVICE_FILE"
else
    echo "⚠️  服务文件不存在: $SERVICE_FILE"
fi

# 4. 重新加载 systemd
echo "重新加载systemd配置..."
systemctl daemon-reload

# 5. 重置失败状态
echo "重置失败状态..."
systemctl reset-failed 2>/dev/null || true

echo
echo "=== 服务卸载完成 ==="
echo "服务 $SERVICE_NAME 已完全卸载"
echo
echo "注意: 项目文件和配置未删除，如需删除请手动处理"

