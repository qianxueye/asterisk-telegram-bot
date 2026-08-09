#!/bin/bash

# Asterisk SMS 实时推送脚本
# 用于从Asterisk接收SMS并推送给Telegram Bot

SMS_PIPE="/tmp/asterisk_sms_pipe"
BOT_SCRIPT="/path/to/asterisk_bot.py"

# 创建命名管道
if [ ! -p "$SMS_PIPE" ]; then
    mkfifo "$SMS_PIPE"
    echo "创建SMS管道: $SMS_PIPE"
fi

# 启动Telegram Bot
echo "启动Telegram Bot..."
python3 "$BOT_SCRIPT" &

# 等待Bot启动
sleep 5

# 监听SMS管道
echo "开始监听SMS管道..."
while true; do
    if [ -p "$SMS_PIPE" ]; then
        # 读取管道数据
        while read -r line; do
            if [ -n "$line" ]; then
                echo "收到SMS数据: $line"
                # 数据已经通过管道传递给Bot处理
            fi
        done < "$SMS_PIPE"
    else
        echo "SMS管道不存在，重新创建..."
        mkfifo "$SMS_PIPE"
        sleep 1
    fi
done
