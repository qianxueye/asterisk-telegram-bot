#!/bin/bash
# The Bot must be the FIFO's only reader.
printf '%s\n' '请使用 systemd 启动 Bot；此脚本不再读取短信管道。' >&2
exit 1
