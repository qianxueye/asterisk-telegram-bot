# Telegram Bot copy ledger

## Initial source baseline

Candidate: the Bot source on `4e7bfdd` plus the new `/inbound_call` command. Client: Telegram Bot, including the default/private command menu, messages, inline buttons, callback answers, generated notifications, and dynamic device/SMS text. This review used `plain-language-iso-24495` and traced each display sink to its command, callback, worker, or formatter condition. It is a source review; rendered Telegram layout and installed-service behavior have not been examined.

| Flow and source | Display condition and reviewed material |
| --- | --- |
| Authorization, `/start`, `/help`, command menu (`asterisk_bot.py:34-49, 390-420, 470-553, 1110-1140`) | An authorized or unauthorized numeric user ID enters a command or callback. Reviewed menu labels, help text, unauthorized replies, and unknown-command handling. The new menu label uses the same authorized command path. |
| SMS compose, preview, confirm, reply, cancel (`asterisk_bot.py:555-1028, 1140-1170`) | A user selects a device, enters body/number, receives a possibly split preview, confirms, edits, cancels, or uses a stale callback. Reviewed the content blocks, button labels, saved-draft states, uncertain delivery language, and error replies; user-supplied body/number are data. |
| Device selection and detail (`asterisk_bot.py:1170-1360, 2105-2140, 2560-2795`) | Device list is empty/present, a callback is stale/invalid, provider is `NONE`, or Asterisk returns settings/state/statistics. Reviewed selection text, buttons, translated field names, raw values, and error replies. |
| User, notification, diagnostics commands (`asterisk_bot.py:1365-1600`) | An authorized user adds/removes a user, changes notification mode, tests a log line, checks recent logs, or asks for pending SMS. Reviewed command syntax, success/failure text, dynamic log presentation, and empty/pending states. |
| SMS submission and receipts (`asterisk_bot.py:1837-2140, 3680-3710`) | CLI submission is rejected, queued, confirmed, fails, or has no module result after ten minutes. Reviewed status formatter, follow-up recipient/content details, and unknown-result notice. |
| UAC recovery and number recovery (`asterisk_bot.py:1033-1110, 1370-1410, 2310-2340, 2458-2550`) | Authorized manual callback, automatic UAC threshold, or periodic number recovery. Reviewed soft/hard reset consequences, expired callbacks, dry-run/health results, automatic notices, and missing-number summaries. |
| Incoming SMS and flood guard (`asterisk_bot.py:3148-3310`) | `/sms_guard` changes mode, FIFO event is dropped/admitted, or a guard transition occurs. Reviewed mode/status labels, drop counts, incoming SMS formatting, reply button, and delivery uncertainty. |
| Silent-SMS alerts (`asterisk_bot.py:349-382, 3510-3635`) | Classifier marks an admitted SMS as silent, or a matching Type 0 TP-PID log notice appears. Reviewed generated fields, classification explanation, and log notice. `send_silent_sms_content_notification` has no caller in this source and is excluded from live display; its text remains present. |
| New per-SIM call settings (`asterisk_bot.py`, `read_inbound_lines` and `handle_inbound_call_command`) | Authorized status, live line list, number selection, saved mode/secret/volume/test update, invalid or ambiguous line, storage failure, or post-replace durability uncertainty. Reviewed every new string below. |

The sink inventory included `self.send_message`, `self.client.send_message`, `event.answer`, `event.edit`, and `Button.inline`, then followed message variables through their builders and the `notify_authorized_users` producers. The `COMMANDS` tuple and formatter return strings were reviewed separately. Asterisk/Telegram library messages, caller-provided SMS bodies, and internal stdout/stderr logs are outside app-owned client copy. No native/mobile/browser client exists in this repository.

## New copy decisions

| Screen/key and condition | Before | After | Reason and activation |
| --- | --- | --- | --- |
| Command menu, authorized or default menu | No `/inbound_call` entry | `按本机号码设置来电处理` | Names the primary selection action; active only after Bot deployment and command sync. |
| Status, no policy file | No command | `来电处理未配置；来电照常转接 3CX。` | States the actual fallback; active only after Bot deployment. |
| Status, unreadable/invalid policy | No command | `来电设置读取失败；来电照常转接 3CX。请联系管理员检查。` | States the read failure, fallback, and next action. |
| Status, valid policy | No command | `上次选择 +号码` when available, SIM's last four identity digits, mode, `口令回放音量 N%`, and explicit `测试：关闭` or `测试：开启（剩余 N 秒）` | Lets the authorized user recognize the selected line while the stable SIM identity remains the routing key. The number is the last selected label, not a live network read. |
| `/inbound_call lines`, no devices or complete line | No command | `未发现设备。请检查 /devices。` or `+号码：可设置。`; the next `mode` command appears only when a selectable line exists | Shows current selectable numbers and the next action. The Bot reads device states twice. |
| `/inbound_call lines`, missing or ambiguous metadata | No command | Device/number with `本机号码未读取`, `SIM 身份未读取`, `号码或 SIM 身份重复`, `设备未连接或未就绪`, `设备状态未就绪`, `其他设备状态不完整`, or a stored setting conflict and a relevant next step | Shows only what the CLI returned and why the number cannot be selected. An inactive slot without identity is unavailable, not asserted to have no SIM. |
| `/inbound_call lines`, invalid stored policy | No command | `来电设置读取失败，暂无法按号码设置。请联系管理员检查。` | Does not suggest a line can be saved when the policy file cannot be read. |
| Number selection, unreadable or changed state | No command | `设备状态读取失败，设置未保存。请检查 /devices 和 /device_state 后重试。` | The selection was not persisted; the device state was incomplete or changed during the two scans. |
| Number selection, unknown/duplicate/incomplete line | No command | `本机号码或 SIM 身份未读取，或存在重复；设置未保存。请检查 /inbound_call lines。` | Keeps the setting unbound and directs the user to current line evidence. |
| Number selection, stored identity or number conflict | No command | `该 SIM 有重复的身份设置，设置未保存。请联系管理员处理。` or `该号码已关联另一张 SIM，设置未保存。请联系管理员核对旧设置。` | Prevents a newly selected number from silently moving a policy to another SIM. |
| Valid write | No command | `设置已保存。` | Sent only after atomic replacement and directory sync. |
| Write rejected before replacement | No command | `来电设置未写入。请联系管理员检查保存目录。` | Names the storage failure separately from command syntax. |
| Write result uncertain after replacement | No command | `保存过程没有返回结果。请先查询 /inbound_call status；口令可重新设置。` | Does not claim success or failure after directory-sync failure. |
| Invalid command/setting | No command | `设置未保存。先发 /inbound_call lines …` with number-based forms and the full ICCID/IMSI option | Gives the accepted actions and primary phone-number route without echoing a secret. |
| Protected mode requested before a secret | No command | `请先设置该 SIM 的口令，再启用口令应答。` | Names the missing prerequisite and next action without disclosing the secret. |

The secret is never included in a Bot reply. The incoming secret command is deleted on a best-effort basis; deletion failure does not claim removal. The command menu and status are source-reviewed only; Telegram rendering and real-device acceptance remain pending.

Arc MCP guidance was consulted for the three exclusive mode choices (radio-group), the timed test on/off state (switch), and the copy/checklist review. This is a Telegram command interface, so no React component or CSS was installed. The applicable checklist items pass in source: named choices, explicit empty/success/error/disabled states, honest result text, and no decorative styling or em dash in new copy. The authorized Bot now shows the full selected local number so the user can recognize the line; it still masks long SIM IDs. Telegram rendering, touch behavior, focus, and responsive layout were not exercised.

## Existing baseline findings

These predate this feature and are not altered by its implementation. They need their own owner decision or issue before a wider copy repair. Copy changes alone would not fix the coupled behavior where noted.

1. `format_sms_send_result_message` shows `此回执不代表对方已收到。` for a module send-success result (`asterisk_bot.py:1870-1881`). This is prohibited defensive comparison wording. The actual state is module-confirmed send; a replacement should state that event and the next available delivery result without promising receipt.
2. `format_silent_sms_message` adds `普通TP-PID notice不再误判` and a Type 0/empty-content label (`asterisk_bot.py:349-382`). The first phrase narrates implementation history; the label is emitted whenever the classifier returns `is_silent`, which includes empty body and heuristic classifications. Separate the observed signal from the classification reason before rewriting this live alert.
3. The Type 0 log-notice alert says `正在等待内容` (`asterisk_bot.py:3510-3575`), while the source's `handle_sms_received` immediately returns and no later content flow consumes that notice. This is a behavior/copy mismatch: remove the promise only with a decision on the intended notice-to-content workflow.
4. Many existing exception branches send `str(e)` to the chat, including SMS replies, compose, diagnostics, and device queries (`asterisk_bot.py:643, 675, 698, 1197, 1404, 1514, 1553, 1595, 2102, 2136, 2590-2652`). These values can include raw command/output details and usually give no safe next action. Replace with condition-specific outcomes after tracing each operation's mutation state; keep details in restricted diagnostics.
5. `/add_user` and `/remove_user` announce a changed authorization list (`asterisk_bot.py:1411-1438`), but `Config.add_user/remove_user` update only an in-memory set (`config.py:67-79`). A Bot restart reloads `AUTHORIZED_USERS` from the environment. This is a functional persistence issue; the copy should follow the chosen persistence behavior rather than imply durable access changes.
6. `/check_sms_logs` lists possible causes when its command returns no text, including `grep命令执行失败` (`asterisk_bot.py:1520-1557`). That branch does not distinguish an empty result from a command failure. Correct the result handling before giving a reason or next action.

No existing user-facing string was changed under this feature. The findings are source evidence, not installed-Bot or live-message acceptance.
