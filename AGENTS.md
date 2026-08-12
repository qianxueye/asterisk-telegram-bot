# OpenAI Codex Guidance

> Scope: This file is for OpenAI Codex. Claude Code instructions remain in `CLAUDE.md` and `.claude/`; Claude agents should not treat this file as their instruction source.

## Code Review Rules

### Review discipline

- Report only defects introduced or materially worsened by the pull request when confidence is above 80%; otherwise remain silent.
- Keep one issue per comment and prefer one sentence that names the failure mode and a concrete fix; add a second sentence only when the impact is not obvious.
- Give actionable feedback, not observations. Do not comment on prose unless the ambiguity is likely to cause an incorrect or unsafe action.
- Do not comment on style, formatting, minor naming, redundant comments, speculative refactors, or requests to document self-explanatory code.
- Do not repeat deterministic failures already covered by repository CI, such as formatting, lint, type-check, build, test, or lockfile-install failures; CI configuration changes and semantic gaps that CI cannot detect remain reviewable.

### Security, correctness, and compatibility

- Flag concrete authorization bypasses, injection paths, path traversal, credential exposure, unsafe handling of external input, and errors or logs that disclose sensitive data.
- Flag concrete crashes, incorrect state transitions, races, resource leaks, boundary or precision errors, swallowed failures, and retries or fallbacks that can silently duplicate, corrupt, or authorize effects.
- Treat public APIs, wire formats, persisted data, identifiers, and cross-repository contracts as compatibility surfaces; require an explicit migration or backward-compatible path for breaking changes.
- Flag architectural deviations only when they create a concrete security, correctness, compatibility, ownership, or dependency-direction failure.

### Telephony and host-control boundaries

- Treat every Telegram-supplied device name, phone number, message, path, and command argument as untrusted; never concatenate it into an Asterisk, shell, sudo, or systemd command.
- Enforce stable numeric-ID authorization on every SMS, device-control, and user-administration path, including callback and reply flows.
- Keep sudoers, service units, sockets, log files, and configuration permissions least-privileged, and never expose bot tokens, API hashes, SMS content, or phone numbers in avoidable logs or errors.
- Subprocesses and log watchers need bounded timeouts, cancellation, and cleanup; retries must not send the same SMS or execute the same recovery action twice.
- Validate Asterisk and modem output before using it as state or command input, and fail safely when devices disappear or return partial output.
