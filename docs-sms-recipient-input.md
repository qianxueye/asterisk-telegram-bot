# SMS recipient input and confirmation

Manual SMS entry through the menu or `/send_sms <device> <number> <message>` normalizes the recipient before showing a confirmation. Bare mainland mobile numbers default to `+86`. Explicit international numbers retain their prefix. `00` becomes `+`; a bare `86` followed by a complete mainland mobile number becomes `+86` without adding a second country code. ASCII spaces and hyphens are removed, and full-width digits/plus signs use their NFKC equivalents. Command-line recipient arguments cannot contain spaces; use the menu for spaced numbers.

Mainland mobile inputs must match `1[3-9][0-9]{9}` after removing the country code. Explicit international inputs require 7–15 ASCII digits after a leading `+`, with a nonzero first digit. These are structural checks, not proof of an assigned number, active subscriber, carrier support, or the intended recipient. Invalid characters, repeated country codes, ambiguous bare numbers, empty input and excessive length are rejected. Do not automatically repair missing digits, leading zeroes, letter/digit substitutions or multiple pasted numbers.

The Bot displays the complete normalized destination, device and escaped message body. Only the confirmation button submits. Editing the number retains the body and replaces the flow token; invalid input retains the draft. Cancelled, replaced, foreign-user, foreign-chat and consumed confirmation buttons cannot submit the draft. The token is consumed before any awaited submission work, so repeated taps do not resubmit. Empty message input remains in the content-entry step. An unconfirmed submission is never automatically retried.

Replies use the original sender address, including service short codes, without adding `+86`. The shared send boundary rejects nonnumeric/control-bearing destinations before invoking the CLI. It does not rewrite reply addresses.

## Reviewed interface copy

Activation scope: the updated Bot process after deployment/restart. Existing Telegram messages are unchanged. Menu and direct-command manual sends now require confirmation; reply buttons retain their existing send behavior.

| Screen/key | Before | After | Reason |
| --- | --- | --- | --- |
| Content entry / empty body | Advanced to number entry | 请输入短信正文，内容不能为空。 | Retain the draft step without submitting an empty SMS. |
| Recipient prompt | 请输入接收手机号（例如 +8613800138000） | 请输入接收手机号，例如 13800138000。不写国际区号时默认 +86；其他地区请加 +国家区号。 | State the default and the explicit international form. |
| Recipient / empty or excessive input | 手机号格式不正确 | 请输入接收手机号。 | Ask for a bounded recipient input. |
| Recipient / invalid characters | 手机号格式不正确 | 号码只能包含数字、开头的 +、空格或连字符。 | Explain accepted input without echoing invalid text. |
| Recipient / ambiguous bare number | 手机号格式不正确 | 未写国际区号时，请输入11位大陆手机号；其他地区请加 +国家区号。 | Avoid guessing a foreign country code. |
| Recipient / international format | 手机号格式不正确 | 国际号码格式不正确，请核对国家区号和号码长度。 | Identify what to check. |
| Recipient / malformed mainland number | 手机号格式不正确 | 请检查大陆手机号的位数和号段，并确认没有重复填写86区号。 | Reject obvious omissions and duplicate prefixes. |
| Recipient / retry suffix | Generic retry prompt | 请重新输入号码，短信内容已保留。 | Preserve the body while correcting a number. |
| Confirmation title/instruction | Number input submitted immediately | 确认发送短信 / 请核对完整号码和内容，点击确认后才会发送。 | Make submission timing explicit. |
| Confirmation controls | No confirmation controls | 确认发送 / 修改号码 / 取消 | Allow destination correction before submission. |
| Confirmation / expired | No SMS confirmation token | 此发送确认已失效，请重新输入号码。 | Prevent stale or foreign callbacks from submitting. |
| Confirmation / edit | No edit step | 请输入新的接收手机号；短信内容已保留。省略国际区号时默认 +86。 | Keep the body and return to recipient entry. |
| Confirmation / pending | No confirmation step | 正在提交短信。 | Describe progress without claiming delivery. |
| Confirmation / submitted | Existing queue-result message only | 已提交，请查看后续发送结果。 | Retain the distinction between submission and final results. |
| Confirmation / uncertain | Existing error message only | 未能确认提交，请先核对发送结果，避免重复发送。 | Avoid implying that a retry is safe. |
| Confirmation / unknown action | Generic unknown-operation message | 未知操作。 | Reject unrecognized callbacks. |
| Send boundary / invalid destination | CLI error after submission attempt | 接收号码格式不正确，未提交短信。 | Reject malformed addresses before the CLI. |

Source checks and mock submissions do not replace the Owner's real SMS acceptance. Agent validation must not send SMS.
