# 网络接口绑定配置指南

## 方法1：使用systemd服务绑定网络接口

### 1. 修改systemd服务文件

使用新的网络绑定服务文件：
```bash
sudo cp systemd/asterisk-telegram-bot-network.service /etc/systemd/system/asterisk-telegram-bot@.service
```

### 2. 配置环境变量

在您的 `.env` 文件中添加：
```bash
# 指定网络接口名称
NETWORK_INTERFACE=eth0

# 或者指定绑定地址
BIND_ADDRESS=192.168.1.100
```

### 3. 重新加载并启动服务
```bash
sudo systemctl daemon-reload
sudo systemctl restart asterisk-telegram-bot@qianxueye.service
```

## 方法2：直接使用网络绑定脚本

### 1. 设置环境变量
```bash
export NETWORK_INTERFACE=eth0
```

### 2. 使用绑定脚本启动
```bash
./start_with_interface.sh .
```

## 方法3：手动设置路由

### 1. 查看网络接口
```bash
ip addr show
```

### 2. 设置Telegram API路由
```bash
# 假设您的网络接口是eth0，网关是192.168.1.1
sudo ip route add 149.154.175.0/24 via 192.168.1.1 dev eth0
sudo ip route add 91.108.4.0/22 via 192.168.1.1 dev eth0
sudo ip route add 91.108.8.0/22 via 192.168.1.1 dev eth0
sudo ip route add 91.108.12.0/22 via 192.168.1.1 dev eth0
sudo ip route add 91.108.16.0/22 via 192.168.1.1 dev eth0
sudo ip route add 91.108.20.0/22 via 192.168.1.1 dev eth0
sudo ip route add 91.108.56.0/22 via 192.168.1.1 dev eth0
```

### 3. 启动机器人
```bash
./venv/bin/python asterisk_bot.py
```

## 方法4：使用iptables进行源地址绑定

### 1. 设置iptables规则
```bash
# 让Telegram API流量从指定接口发出
sudo iptables -t mangle -A OUTPUT -d 149.154.175.0/24 -j MARK --set-mark 1
sudo iptables -t mangle -A OUTPUT -d 91.108.4.0/22 -j MARK --set-mark 1
sudo iptables -t mangle -A OUTPUT -d 91.108.8.0/22 -j MARK --set-mark 1
sudo iptables -t mangle -A OUTPUT -d 91.108.12.0/22 -j MARK --set-mark 1
sudo iptables -t mangle -A OUTPUT -d 91.108.16.0/22 -j MARK --set-mark 1
sudo iptables -t mangle -A OUTPUT -d 91.108.20.0/22 -j MARK --set-mark 1
sudo iptables -t mangle -A OUTPUT -d 91.108.56.0/22 -j MARK --set-mark 1

# 创建路由表
echo "100 telegram_api" >> /etc/iproute2/rt_tables

# 添加路由规则
sudo ip route add default dev eth0 table telegram_api
sudo ip rule add fwmark 1 table telegram_api
```

## 测试连接

### 1. 测试网络连通性
```bash
# 测试Telegram API服务器连通性
ping 149.154.175.50
curl -I https://api.telegram.org
```

### 2. 检查路由
```bash
# 查看路由表
ip route show
ip route show table 100

# 查看网络接口
ip addr show
```

### 3. 查看服务日志
```bash
sudo journalctl -u asterisk-telegram-bot@qianxueye.service -f
```

## 常见问题

### 1. 权限问题
如果遇到权限问题，确保：
- 脚本有执行权限：`chmod +x *.sh`
- 以正确用户身份运行服务

### 2. 网络接口不存在
检查网络接口名称：
```bash
ip addr show
ls /sys/class/net/
```

### 3. 路由不生效
确保：
- 网络接口已启用
- 网关地址正确
- 有足够权限修改路由表

### 4. 服务启动失败
检查：
- 网络接口配置
- 环境变量设置
- 服务文件语法
