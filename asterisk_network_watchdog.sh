#!/usr/bin/env bash
set -uo pipefail

TARGET_HOST="${WATCHDOG_TARGET_HOST:-10.9.40.45}"
TARGET_IF="${WATCHDOG_TARGET_IF:-ztjczphv5x}"
ASTERISK_BIN="${WATCHDOG_ASTERISK_BIN:-/usr/sbin/asterisk}"
REGISTRATION="${WATCHDOG_PJSIP_REGISTRATION:-3cx_registration}"
QUALIFY_ENDPOINT="${WATCHDOG_PJSIP_QUALIFY_ENDPOINT:-10002_out}"
CHECK_INTERVAL="${WATCHDOG_CHECK_INTERVAL:-10}"
PING_TIMEOUT="${WATCHDOG_PING_TIMEOUT:-2}"
FAIL_THRESHOLD="${WATCHDOG_FAIL_THRESHOLD:-3}"
SUCCESS_THRESHOLD="${WATCHDOG_SUCCESS_THRESHOLD:-2}"
RECOVERY_COOLDOWN="${WATCHDOG_RECOVERY_COOLDOWN:-180}"
RESTART_AFTER_FLAP="${WATCHDOG_RESTART_ASTERISK_AFTER_FLAP:-true}"
RESTART_DEFER_ATTEMPTS="${WATCHDOG_RESTART_DEFER_ATTEMPTS:-40}"
RESTART_DEFER_SECONDS="${WATCHDOG_RESTART_DEFER_SECONDS:-15}"
LOG_TAG="${WATCHDOG_LOG_TAG:-asterisk-network-watchdog}"
CLI_TIMEOUT="${WATCHDOG_CLI_TIMEOUT_SECONDS:-10}"

log() {
  logger -t "$LOG_TAG" "$*"
  printf '%s %s\n' "$(date '+%F %T')" "$*"
}

asterisk_cli() {
  timeout --signal=TERM --kill-after=2s "${CLI_TIMEOUT}s" sudo -n "$ASTERISK_BIN" -rx "$1" 2>&1
}

ping_ok() {
  ping -I "$TARGET_IF" -c 1 -W "$PING_TIMEOUT" "$TARGET_HOST" >/dev/null 2>&1
}

active_channels() {
  local output
  output="$(asterisk_cli "core show channels count")" || return 1
  # Missing/error/duplicate output is unknown, never an idle count.
  printf '%s\n' "$output" | awk '
    /^[[:space:]]*[0-9]+ active channels[[:space:]]*$/ { count++; value=$1 }
    END { if (count == 1) print value; else exit 1 }'
}

registration_ok() {
  asterisk_cli "pjsip show registrations" | awk -v target="$REGISTRATION/" '
    { for (j=1; j<=NF; j++) if (index($j, target) == 1)
        for (i=j+1; i<=NF; i++) if ($i == "Registered") ok=1 }
    END { exit !ok }'
}

contact_ok() {
  asterisk_cli "pjsip show contacts" | awk -v target="$QUALIFY_ENDPOINT/" '
    { for (j=1; j<=NF; j++) if (index($j, target) == 1)
        for (i=j+1; i<=NF; i++) if ($i == "Avail") ok=1 }
    END { exit !ok }'
}

recover_pjsip() {
  log "Refreshing PJSIP registration/contact after ZeroTier connectivity recovery"
  asterisk_cli "pjsip send register ${REGISTRATION}" | while IFS= read -r line; do log "pjsip register: ${line}"; done
  asterisk_cli "pjsip qualify ${QUALIFY_ENDPOINT}" | while IFS= read -r line; do log "pjsip qualify: ${line}"; done
  sleep 10

  if registration_ok && contact_ok; then
    log "PJSIP registration/contact are healthy after refresh"
    return 0
  fi

  log "PJSIP still not healthy after refresh"
  return 1
}

restart_asterisk_when_idle() {
  local attempt channels

  for attempt in $(seq 1 "$RESTART_DEFER_ATTEMPTS"); do
    if ! ping_ok; then
      log "Skipped Asterisk restart: network is unavailable again"
      return 1
    fi
    if registration_ok && contact_ok; then
      log "Skipped Asterisk restart: PJSIP recovered while waiting"
      return 0
    fi
    if ! channels="$(active_channels)"; then
      log "Skipped Asterisk restart: active channel count is unknown"
      return 1
    fi

    if [ "$channels" = "0" ]; then
      # A new call can arrive after the count query. Let Asterisk drain it
      # rather than using an immediate restart that can cut off that call.
      local response
      if response="$(asterisk_cli "core restart gracefully")" &&
          ! printf '%s\n' "$response" | grep -Eqi 'No more connections|Unable to connect|No such command|failed'; then
        log "Requested graceful Asterisk restart after unsuccessful PJSIP refresh"
        return 0
      fi
      log "Asterisk restart request was not confirmed; no forced restart attempted"
      return 1
    fi

    log "Deferring Asterisk restart after network flap; active channels=${channels}, attempt=${attempt}/${RESTART_DEFER_ATTEMPTS}"
    sleep "$RESTART_DEFER_SECONDS"
  done

  log "Skipped Asterisk restart because active calls did not drain"
  return 1
}

recover_after_flap() {
  if recover_pjsip; then
    log "Network recovery complete; keeping Asterisk running"
    return 0
  fi
  if [ "$RESTART_AFTER_FLAP" = "true" ]; then
    restart_asterisk_when_idle
  else
    log "PJSIP remains unhealthy; automatic restart is disabled"
    return 1
  fi
}

main() {
  local state="unknown"
  local fail_count=0
  local success_count=0
  local down_since=0
  local last_recovery=0
  local now outage

  log "Starting watchdog target=${TARGET_HOST} interface=${TARGET_IF} interval=${CHECK_INTERVAL}s"

  while true; do
    now="$(date +%s)"

    if ping_ok; then
      success_count=$((success_count + 1))
      fail_count=0
    else
      fail_count=$((fail_count + 1))
      success_count=0
    fi

    if [ "$fail_count" -ge "$FAIL_THRESHOLD" ] && [ "$state" != "down" ]; then
      state="down"
      down_since="$now"
      log "ZeroTier/3CX connectivity is down after ${fail_count} failed checks"
    fi

    if [ "$state" = "down" ] && [ "$success_count" -ge "$SUCCESS_THRESHOLD" ]; then
      state="up"
      outage=$((now - down_since))
      log "ZeroTier/3CX connectivity recovered after ${outage}s"

      if [ $((now - last_recovery)) -ge "$RECOVERY_COOLDOWN" ]; then
        last_recovery="$now"
        recover_after_flap || true
      else
        log "Skipping recovery because cooldown is active"
      fi
    elif [ "$state" != "down" ] && [ "$success_count" -ge "$SUCCESS_THRESHOLD" ]; then
      state="up"
    fi

    sleep "$CHECK_INTERVAL"
  done
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  main "$@"
fi
