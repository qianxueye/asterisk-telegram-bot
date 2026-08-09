#!/bin/bash

# Asterisk 权限诊断脚本
echo "=== Asterisk 权限诊断脚本 ==="
echo ""

# 1. 检查 Asterisk 是否运行
echo "1. 检查 Asterisk 运行状态"
if pgrep -x asterisk > /dev/null; then
    ASTERISK_USER=$(ps -eo user,comm | grep asterisk | grep -v grep | awk '{print $1}' | head -1)
    echo "✅ Asterisk 正在运行"
    echo "   运行用户: $ASTERISK_USER"
else
    echo "❌ Asterisk 未运行"
    exit 1
fi

echo ""

# 2. 查找 socket 文件
echo "2. 查找 Asterisk socket 文件"
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
        ls -la "$socket"
        break
    fi
done

if [ -z "$SOCKET_FILE" ]; then
    echo "❌ 未找到 socket 文件"
fi

echo ""

# 3. 测试当前用户访问
echo "3. 测试当前用户访问"
echo "   当前用户: $(whoami)"
echo "   用户组: $(groups)"
echo ""

echo "   测试命令: asterisk -rx 'core show version'"
if asterisk -rx 'core show version' 2>&1 | grep -q "Unable to connect"; then
    echo "❌ 连接失败 - 权限不足"
    ERROR_MSG=$(asterisk -rx 'core show version' 2>&1)
    echo "   错误信息: $ERROR_MSG"
elif asterisk -rx 'core show version' 2>&1 | grep -q "Asterisk"; then
    echo "✅ 连接成功"
else
    echo "⚠️  命令执行异常"
    asterisk -rx 'core show version' 2>&1
fi

echo ""

# 4. 检查 Asterisk 配置
echo "4. 检查 Asterisk 配置文件"
if [ -f "/etc/asterisk/asterisk.conf" ]; then
    echo "✅ 找到配置文件: /etc/asterisk/asterisk.conf"
    
    if grep -q "^astctlpermissions" /etc/asterisk/asterisk.conf; then
        PERM=$(grep "^astctlpermissions" /etc/asterisk/asterisk.conf | awk '{print $3}')
        echo "   astctlpermissions = $PERM"
    else
        echo "   ⚠️  未配置 astctlpermissions"
    fi
    
    if grep -q "^astctlowner" /etc/asterisk/asterisk.conf; then
        OWNER=$(grep "^astctlowner" /etc/asterisk/asterisk.conf | awk '{print $3}')
        echo "   astctlowner = $OWNER"
    fi
    
    if grep -q "^astctlgroup" /etc/asterisk/asterisk.conf; then
        GROUP=$(grep "^astctlgroup" /etc/asterisk/asterisk.conf | awk '{print $3}')
        echo "   astctlgroup = $GROUP"
    fi
else
    echo "❌ 未找到配置文件"
fi

echo ""

# 5. 测试 sudo 访问
echo "5. 测试 sudo 访问"
if sudo -n asterisk -rx 'core show version' 2>&1 | grep -q "Asterisk"; then
    echo "✅ sudo 访问成功（无需密码）"
elif sudo asterisk -rx 'core show version' 2>&1 | grep -q "Asterisk"; then
    echo "✅ sudo 访问成功（需要密码）"
else
    echo "❌ sudo 访问失败"
fi

echo ""

# 6. 建议
echo "=== 诊断结果和建议 ==="
echo ""

if [ -n "$SOCKET_FILE" ]; then
    SOCKET_PERM=$(ls -l "$SOCKET_FILE" | awk '{print $1}')
    echo "Socket 文件: $SOCKET_FILE"
    echo "Socket 权限: $SOCKET_PERM"
    echo ""
    
    if [[ "$SOCKET_PERM" == *"rwxrwxrwx"* ]] || [[ "$SOCKET_PERM" == *"rw-rw-rw-"* ]]; then
        echo "✅ Socket 权限看起来正确（所有用户可访问）"
    else
        echo "⚠️  Socket 权限可能不够"
        echo "建议: 运行 sudo chmod 777 $SOCKET_FILE"
    fi
fi

echo ""
echo "推荐方案："
echo "1. 如果必须使用 systemd 服务，配置使用 sudo"
echo "   - 在 .env 中: ASTERISK_COMMAND_PREFIX=sudo asterisk -rx"
echo "   - 修改 systemd 服务文件，设置 NoNewPrivileges=false"
echo ""
echo "2. 或者使用普通方式运行（不作为 systemd 服务）"
echo "   - 直接运行: python asterisk_bot.py"
echo "   - 或使用 screen/tmux 保持运行"

