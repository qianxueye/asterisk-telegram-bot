# Modem submission receipts

The queue acknowledgement only confirms that Asterisk accepted a request. The modem's final success or failure completes the Bot's send workflow. Success is labeled `模块已确认发出` and explicitly does not prove delivery to the recipient. The paired Quectel plugin must log final submission metadata at NOTICE level. No verbose/debug/trace logging is needed.

Carrier status reports do not complete the Bot's send workflow and are not pushed as additional send results. The plugin can still process and acknowledge them; this Bot-only change does not reconfigure the modem or remove plugin report handling. Unrelated acknowledgement errors do not complete a queued outgoing send.

Only requests still waiting for a modem result (`queued`) can receive the existing timeout warning: more than 600 seconds, checked every 300 seconds. A modem-confirmed send becomes `completed` before Telegram notification delivery, even when notifications are muted or delivery is uncertain. Completed records are removed after 60 seconds under the existing cleanup schedule. A missing carrier report never triggers a later warning or resend. This change does not reconstruct old reports or add persistent SMS storage.

## Reviewed notification copy

Scope: module result notifications and `cleanup_pending_sms_sends`, in the deployed Bot process after updating and restarting that service. Existing Telegram messages are unchanged. Both manual sends and replies use this result policy.

| State/key | Before | After | Reason |
| --- | --- | --- | --- |
| Module success title | 短信发送成功 | 模块已确认发出 | Name the evidence source without claiming recipient delivery. |
| Module success detail | No explanation | 此回执不代表对方已收到。 | Preserve the limit of a modem submission confirmation. |
| Post-success timeout | 短信已发出，未收到送达报告 | Removed | A modem result now completes the send workflow. |
| `queued` timeout title/detail | 短信发送尚未确认 / 尚未收到模块发送结果，发送状态未知。 | Unchanged | Warn only when the modem result is missing. |

Device, recipient, reference identifiers and escaped message previews retain their existing behavior. No notification or UI failure triggers an automatic SMS resend.
