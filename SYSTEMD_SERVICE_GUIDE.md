# Systemd 服务运行指南

## 问题说明

当以 systemd 服务运行时，如果出现以下错误：

```
sudo is running in a container, you may need to adjust the container configuration to disable the flag.
```

这是因为 systemd 服务的安全容器（`NoNewPrivileges=true`）**不允许使用 sudo 命令**，即使配置了 sudoers NOPASSWD 也无法工作。

## 解决方案：配置 Asterisk Socket 权限（推荐）

通过配置 Asterisk 的 socket 文件权限，让普通用户可以直接访问，无需 sudo。

### 快速配置（一步到位）

```bash
cd ~/Asterisk-Telegram-Bot
sudo chmod +x fix_asterisk_socket_permissions.sh
sudo ./fix_asterisk_socket_permissions.sh
```

脚本会自动：
1. 查找并配置 Asterisk socket 权限
2. 修改 `/etc/asterisk/asterisk.conf` 设置 `astctlpermissions = 0777`
3. 更新 `.env` 配置为不使用 sudo
4. 重启 Asterisk 使配置生效

### 验证配置

```bash
# 1. 重启 Asterisk（脚本会提示）
sudo systemctl restart asterisk

# 2. 检查 socket 权限
ls -la /var/run/asterisk/asterisk.ctl
# 应该显示: srwxrwxrwx（所有用户可访问）

# 3. 测试命令（不需要 sudo）
asterisk -rx 'core show version'
asterisk -rx 'quectel show devices'

# 4. 重启 Bot 服务
sudo systemctl restart asterisk-telegram-bot-你的用户名.service

# 5. 查看状态
sudo systemctl status asterisk-telegram-bot-你的用户名.service
```

### 手动配置（如果脚本失败）

```bash
# 1. 编辑 Asterisk 配置
sudo nano /etc/asterisk/asterisk.conf

# 在 [options] 段添加或修改:
[options]
astctlpermissions = 0777

# 2. 重启 Asterisk
sudo systemctl restart asterisk

# 3. 修改 .env 文件
nano ~/Asterisk-Telegram-Bot/.env

# 将:
ASTERISK_COMMAND_PREFIX=sudo asterisk -rx
# 改为:
ASTERISK_COMMAND_PREFIX=asterisk -rx

# 4. 测试命令
asterisk -rx 'core show version'

# 5. 重启 Bot 服务
sudo systemctl restart asterisk-telegram-bot-你的用户名.service
```

### ⚠️ 为什么不能使用 sudo？

**重要**: systemd 服务的安全容器明确禁止 sudo，原因：

- `NoNewPrivileges=true`: 防止进程获取新特权
- 即使配置 sudoers NOPASSWD 也无法工作
- 这是 systemd 的安全特性，不应绕过

**不推荐的做法**:
- ❌ 设置 `NoNewPrivileges=false`（降低安全性）
- ❌ 以 root 运行服务（非常不安全）
- ✅ 配置 Asterisk socket 权限（正确做法）

## 全新安装

如果是首次安装服务，使用安装脚本会自动检测并提示配置权限：

```bash
cd ~/Asterisk-Telegram-Bot
sudo ./install_service_linux.sh
```

安装完成后，如果出现权限问题，运行：

```bash
sudo ./fix_asterisk_socket_permissions.sh
sudo systemctl restart asterisk
sudo systemctl restart asterisk-telegram-bot-你的用户名.service
```

## 验证配置

### 检查 Socket 权限

```bash
ls -la /var/run/asterisk/asterisk.ctl
# 应该显示: srwxrwxrwx（所有用户可读写）
```

### 检查 Asterisk 配置

```bash
sudo grep astctlpermissions /etc/asterisk/asterisk.conf
# 应该显示: astctlpermissions = 0777
```

### 测试 Asterisk 命令（不使用 sudo）

```bash
asterisk -rx 'core show version'
asterisk -rx 'quectel show devices'
# 应该能正常执行并显示结果
```

### 检查服务状态

```bash
sudo systemctl status asterisk-telegram-bot-你的用户名.service
# 服务应该正常运行，不再出现权限错误
```

### 查看服务日志

```bash
sudo journalctl -u asterisk-telegram-bot-你的用户名.service -f
# 应该能看到正常的运行日志，没有权限相关错误
```

## 常见问题

### Q: Socket 文件不存在怎么办？

A: 确保 Asterisk 正在运行：

```bash
sudo systemctl status asterisk
sudo systemctl start asterisk
```

### Q: 配置后权限仍然不对？

A: 检查 Asterisk 配置并重启：

```bash
# 检查配置
sudo grep astctlpermissions /etc/asterisk/asterisk.conf

# 重启 Asterisk
sudo systemctl restart asterisk

# 再次检查权限
ls -la /var/run/asterisk/asterisk.ctl
```

### Q: .env 配置应该是什么？

A: 必须**不使用 sudo**：

```bash
# 正确
ASTERISK_COMMAND_PREFIX=asterisk -rx

# 错误（会失败）
ASTERISK_COMMAND_PREFIX=sudo asterisk -rx
```

### Q: 为什么不能使用 sudoers NOPASSWD？

A: systemd 服务容器明确禁止 sudo，即使配置 NOPASSWD 也无法工作。这是安全特性，正确做法是配置 socket 权限。

### Q: 服务仍然无法启动？

A: 完整排查步骤：

```bash
# 1. 确认 Asterisk 运行正常
sudo systemctl status asterisk

# 2. 确认 socket 存在且权限正确
ls -la /var/run/asterisk/asterisk.ctl

# 3. 测试命令
asterisk -rx 'core show version'

# 4. 查看 .env 配置
cat ~/Asterisk-Telegram-Bot/.env | grep ASTERISK_COMMAND_PREFIX

# 5. 重启服务
sudo systemctl restart asterisk-telegram-bot-你的用户名.service

# 6. 查看详细日志
sudo journalctl -u asterisk-telegram-bot-你的用户名.service -n 100 --no-pager
```

## 卸载服务

如果需要卸载服务：

```bash
cd ~/Asterisk-Telegram-Bot
sudo ./uninstall_service_linux.sh
```

## 技术说明

### Asterisk Socket 权限机制

1. **工作原理**: Asterisk 通过 Unix socket (`/var/run/asterisk/asterisk.ctl`) 接受命令
2. **权限配置**: 通过 `astctlpermissions` 参数控制 socket 文件权限
3. **访问方式**: 配置为 `0777` 后，所有用户都可以读写 socket

### astctlpermissions 参数说明

```ini
[options]
astctlpermissions = 0777
```

- `0777`: 所有用户可读写执行（推荐用于单用户或信任环境）
- `0660`: 只有 owner 和 group 可访问（默认）
- `0666`: 所有用户可读写（也可以）

### 为什么不能使用 sudo？

**systemd 服务容器的限制**:

- `NoNewPrivileges=true`: 明确禁止进程获取新特权
- sudo 需要提权，会被 systemd 阻止
- 即使配置 sudoers NOPASSWD 也无法绕过
- 这是重要的安全特性，不应该禁用

### Systemd 安全特性

当前服务配置保持了以下安全特性：

- `NoNewPrivileges=true`: 防止进程提权 ✅
- `PrivateTmp=false`: 独立的临时目录
- `ProtectSystem=false`: 允许访问系统文件
- `ProtectHome=false`: 允许访问用户目录
- `ReadWritePaths`: 限制可写路径

通过 socket 权限配置，我们在不降低安全性的情况下解决了权限问题。


## Asterisk 本身的原生监督（现有 Raspberry Pi 部署）

`systemd/asterisk.service` 用于替代本项目现场的 SysV 生成单元。它运行
`/usr/sbin/asterisk -f`，因此 systemd 跟踪真正的 PBX 主进程；异常退出后
5秒重试，5分钟内最多3次启动，避免崩溃重启风暴。`systemctl stop` 不会自动拉起。

此模板保留已核验现场的root运行身份、默认Asterisk配置和524288文件描述符上限，
不替换Asterisk二进制或模块。不应直接用于原本有自定义 `AST_USER`、`ASTARGS`、
`ALTCONF` 等启动参数的主机；先将实际参数迁移到本机override。

部署前备份现有单元、脚本、启用状态和运行参数，验证没有第二个PBX进程。
在确认没有活跃通话后，先执行 `core stop gracefully` 等待原进程退出，
再安装原生单元、`daemon-reload`、启动和启用。不要只依赖旧SysV的stop返回码，
旧单元的MainPID=0可能无法证明daemon已停止。首次启动需确认MainPID与实际
Asterisk PID一致、双卡注册、SIP注册、短信Bot及其FIFO仍正常。

回滚时停止新单元并确认进程消失，恢复原单元/启用状态和watchdog脚本，
再启动旧服务并验证。不要通过杀死生产Asterisk来测试自动拉起；可以在隔离的
systemd测试单元验证重启策略，生产回读验证MainPID与Restart属性。

网络watchdog现在先刷新PJSIP；成功时不重启PBX。失败时再次核对网络、注册
和精确的通话计数，未知/错误结果不视为零。必要时请求 `core restart gracefully`，
让Asterisk停止接收新呼叫并等待已有呼叫结束；请求日志不等于已完成重启。
CLI调用有10秒期限（`WATCHDOG_CLI_TIMEOUT_SECONDS`）和2秒终止宽限，
失败不自动升级为强制重启。该策略不能检测所有daemon挂死，也不等于端到端通话检测。
