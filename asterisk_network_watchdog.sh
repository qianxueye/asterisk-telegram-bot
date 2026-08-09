#!/usr/bin/env bash
set -u

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

log() {
  logger -t "$LOG_TAG" "$*"
  printf '%s %s\n' "$(date '+%F %T')" "$*"
}

asterisk_cli() {
  sudo -n "$ASTERISK_BIN" -rx "$1" 2>&1
}

ping_ok() {
  ping -I "$TARGET_IF" -c 1 -W "$PING_TIMEOUT" "$TARGET_HOST" >/dev/null 2>&1
}

active_channels() {
  asterisk_cli "core show channels count" | awk '/active channels/ {print $1; exit}'
}

registration_ok() {
  asterisk_cli "pjsip show registrations" | grep -Eq "${REGISTRATION}/.*Registered"
}

contact_ok() {
  asterisk_cli "pjsip show contacts" | grep -Eq "${QUALIFY_ENDPOINT}/.*Avail"
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
    channels="$(active_channels || true)"
    channels="${channels:-0}"

    if [ "$channels" = "0" ]; then
      log "Restarting Asterisk after confirmed network flap; no active channels"
      asterisk_cli "core restart now" | while IFS= read -r line; do log "asterisk restart: ${line}"; done
      return 0
    fi

    log "Deferring Asterisk restart after network flap; active channels=${channels}, attempt=${attempt}/${RESTART_DEFER_ATTEMPTS}"
    sleep "$RESTART_DEFER_SECONDS"
  done

  log "Skipped Asterisk restart because active calls did not drain"
  return 1
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
        recover_pjsip || true

        if [ "$RESTART_AFTER_FLAP" = "true" ]; then
          restart_asterisk_when_idle || true
        fi
      else
        log "Skipping recovery because cooldown is active"
      fi
    elif [ "$state" != "down" ] && [ "$success_count" -ge "$SUCCESS_THRESHOLD" ]; then
      state="up"
    fi

    sleep "$CHECK_INTERVAL"
  done
}

main "$@"
