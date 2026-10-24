# SMS submission and delivery receipts

The queue acknowledgement only confirms that Asterisk accepted a request. A subsequent modem confirmation means the modem reported successful submission; it does not prove delivery to the recipient. The network may return a separate delivery report. The paired Quectel plugin must enable direct status-report indications (`AT+CNMI`, `<ds>=1`) and log the final submission/report metadata at NOTICE level. No verbose/debug/trace logging is needed.

The existing cleanup schedule remains unchanged: requests in `queued` or `sent` state expire after more than 600 seconds, checked every 300 seconds. Timeout notifications preserve the known submission result. A missing report does not trigger a resend or turn a known successful submission into a failure. Expired records are removed from memory; this change does not reconstruct old reports or add persistent SMS storage.

## Reviewed notification copy

Scope: `cleanup_pending_sms_sends`, in the deployed Bot process after updating and restarting that service. Existing Telegram messages are unchanged.

| State/key | Before | After | Reason |
| --- | --- | --- | --- |
| `queued` timeout title | 短信发送未收到最终回执 | 短信发送尚未确认 | No modem result is available. |
| `queued` timeout detail | No explanation | 尚未收到模块发送结果，发送状态未知。 | Preserve uncertainty without claiming failure. |
| `sent` timeout title | 短信发送未收到最终回执 | 短信已发出，未收到送达报告 | Keep the known submission confirmation. |
| `sent` timeout detail | No explanation | 模块已确认发出，是否送达尚未确认；这不代表发送失败。 | Distinguish submission from recipient delivery. |

The queued notification retains the warning icon; the sent notification uses an information icon. Device, recipient, elapsed time, and escaped message preview retain their existing behavior.
