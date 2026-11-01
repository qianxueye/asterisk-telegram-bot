# Per-SIM incoming call control

This package adds an opt-in path to the existing `[incoming-mobile]` voice extension. With no readable policy, no matching SIM identity, no matching caller, or `forward` mode, the call continues through the existing 3CX dialplan. SMS, USSD, and outbound 3CX extensions are unchanged.

## Behavior

- The receiving Quectel channel supplies `iccid` and/or `imsi` through its `QUECTEL` JSON variable. A missing or malformed identity never falls back to a `quectel0`/`quectel1` slot name. Configure each SIM using one full `iccid:` or `imsi:` identity. If both identifiers are present and both have policy entries, the call follows 3CX instead of choosing one silently. Remove the conflicting entry before enabling a call mode.
- A caller qualifies only when its normalized number is `021` plus seven or eight digits. Spaces, parentheses, and hyphens may separate digits; `+86` and `0086` are the only supported country prefixes. Other prefixes and withheld caller IDs continue to 3CX.
- `forward` is the default mode for every SIM. `direct` answers and sends three `#` events with 1.5 seconds between them, then hangs up. `protected` answers and reads incoming 8 kHz signed-linear audio from EAGI fd 3 for at most 15 seconds. The dialplan requests `slin` explicitly. It needs the full configured DTMF sequence; partial input, a mismatch, disconnect, or audio failure never sends the final `###`.
- After a full sequence, `protected` plays the recognized symbols back as a low-volume generated WAV, then sends `###` only if the salted digest matches. The replay uses a random per-call filename under a private directory. It does not place symbols in AGI commands, dialplan arguments, routine Bot replies, or logs. The WAV is removed after playback. Interrupted old files are removed on a later call after two minutes.
- `/inbound_call test <SIM> <seconds>` bypasses only the caller-number filter for that SIM. It lasts at most 900 seconds and expires by wall clock. `0` cancels it. It does not bypass identity, mode, or protected-secret checks.

The Bot stores only a salted PBKDF2-SHA256 digest and secret length. The Telegram command containing a new secret is deleted on a best-effort basis; Telegram clients and servers may still retain it. Do not paste a real secret into shell commands, issue comments, or logs. The policy file must be shared only with the Bot account and the Asterisk account.

## Bot commands

Authorized users can send these commands to the existing Bot. Replace `<SIM>` with the full `iccid:...` or `imsi:...` value from the receiving SIM, not its Quectel slot name.

```text
/inbound_call status
/inbound_call secret <SIM> <DTMF sequence>
/inbound_call mode <SIM> forward|direct|protected
/inbound_call volume <SIM> 1-30
/inbound_call test <SIM> 0-900
```

Set a secret before choosing `protected`. The sequence may contain `0-9`, `*`, `#`, and `A-D`. No configured sequence length limit is imposed, but the 15-second audio collection limit still applies. `volume` is the percentage used for replay; it does not change the final `#` events. `status` shows only the SIM type and last four digits, mode, volume, and remaining test time. Settings for a SIM follow its stable identity if slot names change. Each update is serialized and atomically replaces the policy file; active calls use the snapshot read before answering.

## Staged installation and rollback

These are preparation instructions. This change does not deploy anything or authorize a real call, SIM action, or service restart.

1. Identify the actual Asterisk service user/group, AGI directory, sounds directory, active dialplan file, and current 3CX route on the target host. Back up the active dialplan and Bot files. Confirm the receiving channel exposes the intended ICCID or IMSI in `QUECTEL` before enabling a mode. Do not print private numbers or secrets into routine logs.
2. Install `inbound_call.py` and executable `inbound_call_eagi.py` together into the Asterisk AGI directory, normally `/var/lib/asterisk/agi-bin`. The Bot checkout also needs `inbound_call.py` beside `asterisk_bot.py`. Python 3 standard library is sufficient for the EAGI side.
3. Create `/var/lib/asterisk-telegram-bot` owned by the Bot service user, grouped to the Asterisk service group, mode `2750` (setgid). Give the Bot account write access and the Asterisk account group read/traverse access. The Bot creates `inbound-call.json` mode `0640` and a private writer lock. Verify the saved file inherits the Asterisk group before enabling a mode. The shared directory is required; a command fails without it.
4. Create `/var/lib/asterisk/sounds/inbound-dtmf-private` owned by the Asterisk service user, mode `0700`. It holds only short-lived per-call WAV files. Ensure the Asterisk WAV format module and `SendDTMF` application are available.
5. Merge only the added voice priorities from `asterisk-dialplan-integrated.conf` into the active `[incoming-mobile]` extension. Keep the existing 3CX `Dial`, `sms`, `ussd`, `[from-3cx]`, and other extensions intact. Validate dialplan syntax and route priorities before any activation. Restart/reload only under the separate deployment approval and with a rollback timer.
6. Start with `forward` or no policy file. Verify the installed Bot and Asterisk versions, the policy file access from both service accounts, and that an unmatched call still reaches 3CX before changing a SIM to `direct` or `protected`.

To roll back, set each SIM to `forward` while the Bot is available, then restore the backed-up voice dialplan priorities and reload Asterisk under the deployment plan. Leave the policy file secured during rollback; remove it only after preserving the Owner's settings and confirming no calls use it.

## Real-device acceptance reserved for the Owner

Synthetic tests cover the Python decoder and AGI branches. They do not prove Quectel UAC captures the caller's acoustic DTMF, that EAGI fd 3 has the expected signed-linear format on the installed Asterisk build, that `STREAM FILE` emits the generated WAV through the UAC path, or that `SendDTMF` produces exactly three remote `#` events at the requested timing. The Quectel driver queues `AT+VTS` on DTMF begin and ignores the requested DTMF end duration. The fixed 1.5-second waits are between Asterisk send operations; modem and network timing need physical measurement.

The routing uses Asterisk's [EAGI fd 3](https://docs.asterisk.org/Asterisk_22_Documentation/API_Documentation/Dialplan_Applications/EAGI/), [CHANNEL(state)](https://docs.asterisk.org/Latest_API/API_Documentation/Dialplan_Functions/CHANNEL/), [STREAM FILE](https://docs.asterisk.org/Asterisk_22_Documentation/API_Documentation/AGI_Commands/stream_file/), and [SendDTMF](https://docs.asterisk.org/Certified-Asterisk_22.8_Documentation/API_Documentation/Dialplan_Applications/SendDTMF/) behavior. The installed Asterisk and Quectel builds still require the device test above.

Under a separately approved controlled call plan, test both SIM identities and slot changes, normal and withheld callers, unsupported prefixes, disabled and corrupt config, test expiry, direct `###`, protected success and mismatch, noisy/quiet DTMF, partial/slow input, caller disconnect during collection and playback, simultaneous calls, and a 3CX/SMS/USSD regression. Inspect the caller's heard replay volume and the receiving end's DTMF events. Bind the results to the installed source commit, Asterisk build, plugin build, active dialplan, and actual device/SIM.

## Copy record

The Bot's full source-copy baseline, display conditions, new `/inbound_call` strings, and existing findings are recorded in [docs-bot-copy-ledger.md](docs-bot-copy-ledger.md). Rendered Telegram and installed-device copy acceptance remain part of the reserved deployment validation.
