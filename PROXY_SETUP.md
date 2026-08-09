# 代理配置指南

## 支持的代理类型

### SOCKS5 配置示例
```bash
PROXY_TYPE=socks5
PROXY_HOST=proxy.example.internal
PROXY_PORT=1080
PROXY_RDNS=true
PROXY_USERNAME=
PROXY_PASSWORD=
```

`PROXY_RDNS=true` 会让域名解析也走SOCKS5代理，适合Telegram在本地网络无法直接解析或访问的环境。

### 1. HTTP代理
```bash
PROXY_TYPE=http
PROXY_HOST=127.0.0.1
PROXY_PORT=8080
PROXY_USERNAME=用户名（可选）
PROXY_PASSWORD=密码（可选）
```

### 2. SOCKS4代理
```bash
PROXY_TYPE=socks4
PROXY_HOST=127.0.0.1
PROXY_PORT=1080
PROXY_USERNAME=用户名（可选）
PROXY_PASSWORD=密码（可选）
```

### 3. SOCKS5代理
```bash
PROXY_TYPE=socks5
PROXY_HOST=127.0.0.1
PROXY_PORT=1080
PROXY_USERNAME=用户名（可选）
PROXY_PASSWORD=密码（可选）
```

### 4. MTProto代理（Telegram专用）
```bash
PROXY_TYPE=mtproto
PROXY_HOST=代理服务器地址
PROXY_PORT=代理端口
PROXY_PASSWORD=MTProto密钥
```

## 配置步骤

### 1. 编辑配置文件
```bash
nano .env
```

### 2. 添加代理配置
在 `.env` 文件中添加相应的代理配置，例如：
```bash
# HTTP代理示例
PROXY_TYPE=http
PROXY_HOST=192.168.1.100
PROXY_PORT=8080
```

### 3. 安装依赖
```bash
pip install PySocks==1.7.1
```

### 4. 测试配置
```bash
python asterisk_bot.py
```

## 常见代理服务器

### 1. Shadowsocks
```bash
PROXY_TYPE=socks5
PROXY_HOST=127.0.0.1
PROXY_PORT=1080
```

### 2. V2Ray
```bash
PROXY_TYPE=http
PROXY_HOST=127.0.0.1
PROXY_PORT=8080
```

### 3. Clash
```bash
PROXY_TYPE=http
PROXY_HOST=127.0.0.1
PROXY_PORT=7890
```

### 4. Telegram MTProto代理
```bash
PROXY_TYPE=mtproto
PROXY_HOST=代理服务器IP
PROXY_PORT=代理端口
PROXY_PASSWORD=代理密钥
```

## 故障排除

### 1. 代理连接失败
- 检查代理服务器是否运行
- 验证代理地址和端口
- 确认代理用户名和密码

### 2. 认证失败
- 检查用户名和密码是否正确
- 确认代理服务器支持认证

### 3. 网络超时
- 检查代理服务器网络连接
- 尝试不同的代理类型
- 增加超时时间

### 4. 测试代理连接
```bash
# 测试HTTP代理
curl --proxy http://用户名:密码@代理地址:端口 https://api.telegram.org

# 测试SOCKS5代理
curl --socks5 用户名:密码@代理地址:端口 https://api.telegram.org
```

## 系统服务配置

### 1. 使用systemd服务
```bash
# 复制服务文件
sudo cp systemd/asterisk-telegram-bot.service /etc/systemd/system/asterisk-telegram-bot@.service

# 重新加载配置
sudo systemctl daemon-reload

# 启动服务
sudo systemctl start asterisk-telegram-bot@用户名.service
```

### 2. 查看服务状态
```bash
sudo systemctl status asterisk-telegram-bot@用户名.service
```

### 3. 查看日志
```bash
sudo journalctl -u asterisk-telegram-bot@用户名.service -f
```

## 安全注意事项

1. **不要在生产环境中使用明文密码**
2. **定期更换代理密码**
3. **使用可信的代理服务器**
4. **监控代理连接状态**

## 性能优化

1. **选择地理位置较近的代理服务器**
2. **使用专用代理而非公共代理**
3. **定期测试代理性能**
4. **配置代理池以提高可用性**
