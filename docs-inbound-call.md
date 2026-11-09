# Per-SIM incoming call control

This package adds two choices to the existing `[incoming-mobile]` voice route. `forward` is the default and continues to the existing 3CX dialplan. `direct` answers a selected call, asks Asterisk to send `###` with a 1.5-second gap between digits, then ends the call. SMS, USSD, and outbound 3CX routes are unchanged. The 3CX destination itself is configured separately.

## Selection and behavior

- The receiving channel supplies ICCID and/or IMSI through its `QUECTEL` JSON variable. A missing or malformed identity follows 3CX. The Bot binds a local subscriber number to a stable SIM identity from a live device-state snapshot; it never binds to a `quectel0` or `quectel1` slot name. If both identifiers have separate policy entries, the call follows 3CX until that conflict is resolved.
- A caller qualifies when its normalized number is `021` plus seven or eight digits. Spaces, parentheses, and hyphens may separate digits. `+86` and `0086` are the supported country prefixes; `21` after either prefix gains the domestic `0`. An existing `021` keeps it. Other prefixes and withheld numbers follow 3CX.
- A per-SIM test setting temporarily bypasses only the caller-number filter. It expires automatically after at most 900 seconds; `0` cancels it. Identity and mode checks still apply. Active calls use the policy snapshot read before answering.
- Direct mode uses one synchronous `SendDTMF(###,1500,100)` application call. There is no audio capture, incoming DTMF decoding, secret, or caller replay.

## Bot commands

Authorized users can list live numbers and use the exact `+` number shown as `<LINE>`. The Bot resolves it again for every setting change. A full `iccid:...` or `imsi:...` value is also accepted when the modem does not report a number; verify that identity independently before use. Never use a Quectel slot name.

```text
/inbound_call status
/inbound_call lines
/inbound_call mode <LINE> forward|direct
/inbound_call test <LINE> 0-900
```

`status` shows the last number selected through the Bot, the final four SIM identity digits, the mode, and remaining test time. The stored number is a label; call routing always uses the channel's stable identity. Each update holds a writer lock and atomically replaces the policy file. A phone-number selection requires one matching ready line with a valid number and ICCID or IMSI in two unchanged device-state scans. Duplicate numbers or identities, unreadable active slots, changed state, and a number already bound to another SIM are rejected. A second inactive slot without metadata is shown as unavailable; it does not stop selection of an identified ready line. Its inactive state does not establish whether a SIM is inserted. If `Subscriber Number` is missing, use `/device_state` to inspect the line; no number is guessed.

## Staged installation and rollback

These instructions do not deploy the change or authorize a real call, SIM action, or service restart.

1. Identify the actual Asterisk service user/group, AGI directory, active dialplan file, and current 3CX route. Back up the dialplan, Bot files, and existing policy with restricted access. The old policy backup may contain credential material; remove it under the Owner's retention policy after migration and rollback verification. Confirm the selected local number and ICCID or IMSI appear together in `quectel show device state`, and that the receiving channel exposes the same identity in `QUECTEL`.
2. Install `inbound_call.py` and executable `inbound_call_agi.py` into the Asterisk AGI directory, normally `/var/lib/asterisk/agi-bin`. Install `inbound_call.py` beside `asterisk_bot.py` in the Bot checkout. Remove the retired `inbound_call_eagi.py` from the active AGI directory after the dialplan no longer references it. Python 3 standard library is sufficient on the AGI side.
3. The policy directory `/var/lib/asterisk-telegram-bot` must be owned by the Bot service user, grouped to the Asterisk service group, and mode `2750`. The Bot creates `inbound-call.json` mode `0640` and a private writer lock. Verify that the file inherits the Asterisk group and that the Asterisk account can read it.
4. If an existing version 1 policy is present, run `python3 -c 'from inbound_call import InboundCallPolicy; InboundCallPolicy().migrate_legacy()'` from the Bot checkout as the Bot service user before activating the new dialplan. This atomically writes version 2, removes stored secret, digest, and replay-volume fields, retains `direct` and `forward` settings, and changes every old `protected` entry to `forward`. Check `version` and field names in the file after migration without printing old values. If migration fails, keep calls routed to 3CX and resolve the policy error before enabling direct mode.
5. Merge only the added voice priorities from `asterisk-dialplan-integrated.conf` into the active `[incoming-mobile]` extension. Keep the existing 3CX `Dial`, `sms`, `ussd`, `[from-3cx]`, and other extensions intact. Validate dialplan syntax and route priorities before activation. Restart or reload only under the separate deployment approval and rollback plan.
6. Start with `forward` or no policy file. Verify the installed Bot and Asterisk versions, policy access from both service accounts, and that an unmatched call still reaches 3CX before setting a SIM to `direct`.

To roll back, set each SIM to `forward` while the Bot is available, then restore the backed-up voice dialplan priorities and reload Asterisk under the deployment plan. Preserve the policy file and the Owner's settings during rollback.

## Real-device acceptance reserved for the Owner

Automated tests cover policy selection, caller filtering, version 1 migration, Bot authorization, AGI branches, and the single `SendDTMF` command. They do not prove that the Quectel modem emits three remote `#` events at the requested timing. Asterisk's `SendDTMF` call waits 1.5 seconds between tones and also waits after the final digit before returning. The Quectel driver queues `AT+VTS` on DTMF begin and ignores the requested end duration, so modem and network timing require physical measurement. The AGI does not add another sleep and exits only after the application returns or the call ends.

Under a separately approved controlled call plan, test each selected SIM and a slot swap, matching and unmatched callers, withheld and unsupported numbers, test expiry, direct `###`, early disconnect, simultaneous calls, and 3CX/SMS/USSD regression. Bind the result to the installed source commit, Asterisk build, Quectel plugin build, active dialplan, and actual device/SIM.
