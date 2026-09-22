"""Run real Bash watchdog functions with deterministic CLI/network substitutes."""
import os
import subprocess
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / 'asterisk_network_watchdog.sh'

class WatchdogTests(unittest.TestCase):
    def run_shell(self, body):
        env = dict(os.environ, WATCHDOG_RESTART_DEFER_ATTEMPTS='1', WATCHDOG_RESTART_DEFER_SECONDS='0')
        return subprocess.run(['bash','-c',
            'source "$1"\nlog() { printf "%s\\n" "$*"; }\nsleep() { :; }\n'+body,
            'watchdog-test',str(SCRIPT)],capture_output=True,text=True,env=env,timeout=5)

    def test_successful_refresh_never_restarts(self):
        r=self.run_shell('recover_pjsip() { return 0; }; restart_asterisk_when_idle() { echo BAD_RESTART; }; recover_after_flap')
        self.assertEqual(r.returncode,0);self.assertNotIn('BAD_RESTART',r.stdout)

    def test_unknown_count_never_restarts(self):
        for output in ('', 'No more connections allowed','garbage','0 active channels\n1 active channels'):
            body='''ping_ok() { return 0; }; registration_ok() { return 1; };
asterisk_cli() { if [[ "$1" == 'core show channels count' ]]; then printf '%s\\n' "$REPLY"; else echo BAD_RESTART; fi; }
REPLY=$OUTPUT
restart_asterisk_when_idle'''
            with self.subTest(output=output):
                # Shell values are passed through the environment, never evaluated.
                with __import__('unittest.mock',fromlist=['patch']).patch.dict(os.environ, {'OUTPUT':output}):
                    r=self.run_shell(body)
                self.assertNotIn('BAD_RESTART',r.stdout);self.assertNotEqual(r.returncode,0)

    def test_positive_call_count_and_cli_error_fail_closed(self):
        for command in ('echo "1 active channels"','echo "0 active channels"; return 1'):
            r=self.run_shell('ping_ok(){ return 0; }; registration_ok(){ return 1; }; asterisk_cli(){ '+command+'; }; restart_asterisk_when_idle')
            self.assertNotIn('Requested graceful',r.stdout);self.assertNotEqual(r.returncode,0)

    def test_only_idle_unhealthy_network_recovered_requests_graceful(self):
        r=self.run_shell('''ping_ok(){ return 0; }; registration_ok(){ return 1; };
asterisk_cli(){ case "$1" in 'core show channels count') echo '0 active channels';; 'core restart gracefully') echo 'accepted' >&2;; *) return 1;; esac; }
restart_asterisk_when_idle''')
        self.assertEqual(r.returncode,0);self.assertIn('Requested graceful',r.stdout);self.assertIn('accepted',r.stderr)

    def test_network_loss_or_pjsip_recovery_skips_restart(self):
        for setup in ('ping_ok(){ return 1; };','ping_ok(){ return 0; }; registration_ok(){ return 0; }; contact_ok(){ return 0; };'):
            r=self.run_shell(setup+'asterisk_cli(){ echo BAD_RESTART; }; restart_asterisk_when_idle')
            self.assertNotIn('BAD_RESTART',r.stdout);self.assertNotIn('Requested graceful',r.stdout)

    def test_rejected_restart_is_not_reported_success(self):
        r=self.run_shell('''ping_ok(){ return 0; }; registration_ok(){ return 1; };
asterisk_cli(){ if [[ "$1" == 'core show channels count' ]]; then echo '0 active channels'; else echo 'No more connections allowed'; fi; }
restart_asterisk_when_idle''')
        self.assertNotEqual(r.returncode,0);self.assertNotIn('Requested graceful',r.stdout)

    def test_health_matches_exact_endpoint_and_state(self):
        r=self.run_shell('''REGISTRATION=trunk; QUALIFY_ENDPOINT=endpoint;
asterisk_cli(){ case "$1" in *registrations) printf 'other/uri auth Registered\\ntrunk/uri auth Unregistered\\n';; *) printf 'Contact: endpoint/uri hash Unavail\\n';; esac; }
registration_ok && exit 11
contact_ok && exit 12
asterisk_cli(){ case "$1" in *registrations) echo 'trunk/uri auth Registered';; *) echo 'Contact: endpoint/uri hash Avail';; esac; }
registration_ok && contact_ok''')
        self.assertEqual(r.returncode,0)

    def test_cli_has_bounded_timeout_without_changing_sudo_command(self):
        r=self.run_shell('timeout(){ printf "%s\\n" "$*"; }; asterisk_cli "core show uptime"')
        self.assertEqual(r.returncode,0)
        self.assertIn('--signal=TERM --kill-after=2s 10s sudo -n /usr/sbin/asterisk -rx core show uptime',r.stdout)
