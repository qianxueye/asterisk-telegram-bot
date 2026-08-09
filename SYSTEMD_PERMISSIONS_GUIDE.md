# Systemd 服务权限配置完整指南

## 问题分析

### 为什么 systemd 服务容器不允许 sudo？

systemd 的安全特性中，`NoNewPrivileges=true` 会阻止进程获取新的特权，包括：
- 无法使用 sudo（即使配置了 NOPASSWD）
- 无法使用 setuid 程序
- 无法提升权限

这是一个重要的安全特性，用于防止服务被攻破后获取更高权限。

### 错误表现

```
sudo is running in a container, you may need to adjust the container configuration to disable the flag.
```

或者设备状态显示为：
```
📱 sudo:
   📞 手机号: 
   📶 状态: sudo is running in a container...
```

## 解决方案：Asterisk Socket 权限配置

### 方案原理

Asterisk 通过 Unix socket（`/var/run/asterisk/asterisk.ctl`）接受命令。我们可以配置这个 socket 的权限，让所有用户都能访问，这样就不需要 sudo 了。

### 优势

✅ **安全兼容**: 保持 systemd 的安全特性
✅ **简单有效**: 只需配置一次
✅ **持久化**: 重启后仍然有效
✅ **标准做法**: 这是 Asterisk 的标准权限配置方式

## 快速配置

### 一步到位

```bash
cd ~/Asterisk-Telegram-Bot
sudo chmod +x fix_asterisk_socket_permissions.sh
sudo ./fix_asterisk_socket_permissions.sh
```

脚本会自动：
1. 查找 Asterisk socket 文件
2. 设置 socket 为所有用户可访问
3. 配置 `/etc/asterisk/asterisk.conf` 使权限持久化
4. 更新 `.env` 配置为不使用 sudo
5. 重启 Asterisk 服务

### 验证配置

```bash
# 1. 重启 Asterisk
sudo systemctl restart asterisk

# 2. 检查 socket 权限
ls -la /var/run/asterisk/asterisk.ctl
# 应该显示: srwxrwxrwx （所有用户可读写）

# 3. 测试命令（不需要 sudo）
asterisk -rx 'core show version'
asterisk -rx 'quectel show devices'

# 4. 重启 Bot 服务
sudo systemctl restart asterisk-telegram-bot-你的用户名.service

# 5. 查看状态
sudo systemctl status asterisk-telegram-bot-你的用户名.service
```

## 手动配置步骤

如果自动脚本无法运行，可以手动配置：

### 1. 编辑 Asterisk 配置

```bash
sudo nano /etc/asterisk/asterisk.conf
```

在 `[options]` 段添加或修改：
```ini
[options]
astctlpermissions = 0777
```

保存并退出（Ctrl+X, Y, Enter）

### 2. 重启 Asterisk

```bash
sudo systemctl restart asterisk
```

### 3. 验证 socket 权限

```bash
ls -la /var/run/asterisk/asterisk.ctl
```

应该显示：
```
srwxrwxrwx 1 asterisk asterisk 0 date asterisk.ctl
```

### 4. 更新 .env 配置

```bash
nano ~/Asterisk-Telegram-Bot/.env
```

修改为：
```
ASTERISK_COMMAND_PREFIX=asterisk -rx
```

**重要**: 去掉 `sudo`

### 5. 测试命令

```bash
# 以普通用户身份测试（不需要 sudo）
asterisk -rx 'core show version'
asterisk -rx 'quectel show devices'
```

应该能正常执行并显示结果。

### 6. 重启 Bot 服务

```bash
sudo systemctl restart asterisk-telegram-bot-你的用户名.service
```

### 7. 查看日志确认

```bash
sudo journalctl -u asterisk-telegram-bot-你的用户名.service -f
```

应该不再出现权限相关错误。

## 技术细节

### astctlpermissions 参数

这个参数控制 Asterisk 创建的 socket 文件权限：

- `0660`: 只有 owner 和 group 可访问（默认）
- `0666`: 所有用户可读写（推荐用于 socket）
- `0777`: 所有用户可读写执行（最宽松）

### Socket 文件位置

常见位置：
- `/var/run/asterisk/asterisk.ctl`
- `/run/asterisk/asterisk.ctl`（符号链接）
- `/tmp/asterisk/asterisk.ctl`（少见）

### 权限说明

```bash
srwxrwxrwx
s         # socket 文件类型
 rwx      # owner 权限（读、写、执行）
    rwx   # group 权限（读、写、执行）
       rwx # other 权限（读、写、执行）
```

## 安全性考虑

### 这样做安全吗？

✅ **相对安全**，因为：

1. **本地访问**: socket 只能在本地系统访问，不暴露到网络
2. **Asterisk 授权**: Asterisk 本身有权限控制机制
3. **审计日志**: 所有操作都会记录在 Asterisk 日志中
4. **systemd 限制**: systemd 的其他安全特性仍然有效

### 如何进一步提升安全性

如果担心安全性，可以：

1. **创建专用组**:
   ```bash
   sudo groupadd asterisk-users
   sudo usermod -aG asterisk-users 你的用户名
   ```
   
   然后设置权限为 `0770`，并将 socket 的 group 设为 `asterisk-users`

2. **使用 ACL**:
   ```bash
   sudo setfacl -m u:你的用户名:rw /var/run/asterisk/asterisk.ctl
   ```

3. **限制 Asterisk 功能**: 在 Asterisk 中配置更细粒度的权限控制

## 故障排除

### 问题 1: Socket 文件不存在

**原因**: Asterisk 未运行

**解决**:
```bash
sudo systemctl status asterisk
sudo systemctl start asterisk
```

### 问题 2: 配置后权限仍然不对

**原因**: Asterisk 未重启或配置未生效

**解决**:
```bash
# 检查配置
sudo grep astctlpermissions /etc/asterisk/asterisk.conf

# 重启 Asterisk
sudo systemctl restart asterisk

# 再次检查权限
ls -la /var/run/asterisk/asterisk.ctl
```

### 问题 3: 仍然提示权限错误

**原因**: .env 配置仍然使用 sudo

**解决**:
```bash
# 检查配置
cat ~/Asterisk-Telegram-Bot/.env | grep ASTERISK_COMMAND_PREFIX

# 应该是: asterisk -rx
# 如果是: sudo asterisk -rx，需要去掉 sudo

# 修改
nano ~/Asterisk-Telegram-Bot/.env
```

### 问题 4: Bot 服务仍然报错

**完整排查步骤**:

```bash
# 1. 确认 Asterisk 运行正常
sudo systemctl status asterisk

# 2. 确认 socket 存在且权限正确
ls -la /var/run/asterisk/asterisk.ctl

# 3. 测试命令
asterisk -rx 'core show version'

# 4. 查看 Bot 配置
cat ~/Asterisk-Telegram-Bot/.env | grep ASTERISK_COMMAND_PREFIX

# 5. 重启 Bot 服务
sudo systemctl restart asterisk-telegram-bot-你的用户名.service

# 6. 查看详细日志
sudo journalctl -u asterisk-telegram-bot-你的用户名.service -n 100 --no-pager
```

## 其他方案对比

### ❌ 方案 A: 使用 sudo + sudoers

```bash
ASTERISK_COMMAND_PREFIX=sudo asterisk -rx
```

**问题**: systemd 容器不允许 sudo
**结果**: 无法工作

### ❌ 方案 B: 放宽 systemd 限制

```ini
NoNewPrivileges=false
```

**问题**: 降低安全性
**结果**: 不推荐

### ✅ 方案 C: Socket 权限配置（当前方案）

```bash
ASTERISK_COMMAND_PREFIX=asterisk -rx
astctlpermissions = 0777
```

**优点**: 
- 兼容 systemd 安全特性
- 配置简单
- 安全可控

## 总结

1. **问题根源**: systemd 服务容器的安全限制不允许使用 sudo
2. **正确方案**: 配置 Asterisk socket 权限，允许直接访问
3. **配置步骤**: 运行 `fix_asterisk_socket_permissions.sh` 脚本
4. **关键配置**: `astctlpermissions = 0777` 和 `.env` 中不使用 sudo
5. **验证方法**: 直接执行 `asterisk -rx` 命令应该成功

这是解决 systemd 服务权限问题的**标准且推荐的方案**！

