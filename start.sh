#!/bin/bash

# Asterisk Telegram Bot 启动脚本

echo "🤖 启动 Asterisk Telegram Bot..."

# 检查Python环境
if ! command -v python3 &> /dev/null; then
    echo "❌ Python3 未安装"
    exit 1
fi

# 检查依赖
if [ ! -f "requirements.txt" ]; then
    echo "❌ requirements.txt 文件不存在"
    exit 1
fi

# 安装依赖
echo "📦 安装依赖包..."
pip3 install -r requirements.txt

# 检查配置文件
if [ ! -f ".env" ]; then
    echo "⚠️  .env 文件不存在，请复制 .env.example 并配置"
    echo "cp .env.example .env"
    echo "然后编辑 .env 文件填入正确的配置信息"
    exit 1
fi

# 启动机器人
echo "🚀 启动机器人..."
python3 asterisk_bot.py
