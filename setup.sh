#!/bin/bash

# Asterisk Telegram Bot 快速设置脚本
set -e

echo "=== Asterisk Telegram Bot 快速设置 ==="

# 检查是否在项目目录中
if [ ! -f "asterisk_bot.py" ]; then
    echo "❌ 请在项目根目录运行此脚本"
    exit 1
fi

# 创建虚拟环境
echo "1. 创建Python虚拟环境..."
if [ ! -d "venv" ]; then
    python3 -m venv venv
    echo "✅ 虚拟环境创建成功"
else
    echo "✅ 虚拟环境已存在"
fi

# 激活虚拟环境并安装依赖
echo "2. 安装Python依赖..."
source venv/bin/activate
pip install -r requirements.txt
echo "✅ 依赖安装完成"

# 创建配置文件
echo "3. 检查配置文件..."
if [ ! -f ".env" ]; then
    echo "⚠️  .env 文件不存在，正在创建..."
    cp config.example .env
    echo "✅ .env 文件已创建"
    echo ""
    echo "📝 请编辑 .env 文件，填入正确的配置信息："
    echo "   - BOT_TOKEN (从 @BotFather 获取)"
    echo "   - API_ID 和 API_HASH (从 https://my.telegram.org 获取)"
    echo "   - AUTHORIZED_USERS (授权用户ID列表)"
    echo ""
    echo "编辑命令: nano .env"
    echo ""
    echo "配置完成后，运行以下命令检查配置："
    echo "  ./check_config.sh"
    echo ""
    echo "然后可以使用以下命令安装服务："
    echo "  sudo ./install_service_linux.sh  # 完整安装"
    echo "  sudo ./quick_install.sh          # 快速安装"
else
    echo "✅ .env 文件已存在"
fi

echo ""
echo "=== 设置完成 ==="
echo "下一步："
echo "1. 编辑 .env 文件配置参数"
echo "2. 运行 ./check_config.sh 检查配置"
echo "3. 运行 sudo ./install_service_linux.sh 安装服务"
