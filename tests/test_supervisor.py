"""
Unit tests for the supervisor module.
"""

import os
import subprocess
import unittest
from unittest import mock

import supervisor


class TestPidHelpers(unittest.TestCase):
    """Test case for the module-level pidfile helper functions."""

    @mock.patch("os.kill")
    def test_pid_is_alive_true(self, mock_kill):
        """os.kill(pid, 0) succeeding means the process is alive."""
        mock_kill.return_value = None
        self.assertTrue(supervisor._pid_is_alive(123))
        mock_kill.assert_called_once_with(123, 0)

    @mock.patch("os.kill")
    def test_pid_is_alive_false(self, mock_kill):
        """os.kill(pid, 0) raising OSError means the process is gone."""
        mock_kill.side_effect = OSError("No such process")
        self.assertFalse(supervisor._pid_is_alive(123))

    def test_read_pid_file_missing(self):
        """A missing pidfile resolves to None, not an exception."""
        self.assertIsNone(supervisor._read_pid_file("/no/such/pidfile"))

    def test_read_pid_file_invalid_content(self):
        """Non-integer pidfile content resolves to None."""
        with mock.patch(
            "builtins.open", mock.mock_open(read_data="not-a-pid\n")
        ):
            self.assertIsNone(supervisor._read_pid_file("/some/pidfile"))

    def test_read_pid_file_valid(self):
        """A valid pidfile returns the parsed integer PID."""
        with mock.patch("builtins.open", mock.mock_open(read_data="4321\n")):
            self.assertEqual(supervisor._read_pid_file("/some/pidfile"), 4321)


class TestSupervisorDaemon(unittest.TestCase):
    """Test case for SupervisorDaemon's child lifecycle management."""

    def setUp(self):
        """Build a supervisor instance without touching a real pidfile."""
        patcher = mock.patch.object(supervisor.daemonizer.Daemon, "__init__")
        self.addCleanup(patcher.stop)
        patcher.start()

    def _make_supervisor(self, child_mode):
        instance = supervisor.SupervisorDaemon.__new__(
            supervisor.SupervisorDaemon
        )
        instance.child_mode = child_mode
        instance._daemon_proc = None
        instance._restart_requested = False
        instance._stopping = False
        instance._child_stop_done = False
        return instance

    @mock.patch.object(supervisor.SupervisorDaemon, "_confirm_daemon_started")
    @mock.patch("subprocess.Popen")
    def test_launch_child_start_mode_reaps_and_confirms(
        self, mock_popen, mock_confirm
    ):
        """`start` mode waits on the launcher process, then confirms via pidfile."""
        instance = self._make_supervisor("start")
        mock_process = mock.Mock()
        mock_popen.return_value = mock_process

        instance._launch_child()

        called_cmd = mock_popen.call_args[0][0]
        self.assertIn("sudo", called_cmd)
        self.assertIn(supervisor.DAEMON_SCRIPT, called_cmd)
        self.assertIn("start", called_cmd)
        mock_process.wait.assert_called_once()
        mock_confirm.assert_called_once()
        self.assertIsNone(instance._daemon_proc)

    @mock.patch("time.sleep")
    @mock.patch("subprocess.Popen")
    def test_launch_child_foreground_mode_tracks_handle(
        self, mock_popen, mock_sleep
    ):
        """`foreground` mode keeps the Popen handle instead of waiting on it."""
        instance = self._make_supervisor("foreground")
        mock_process = mock.Mock()
        mock_process.poll.return_value = None  # still running
        mock_popen.return_value = mock_process

        instance._launch_child()

        called_cmd = mock_popen.call_args[0][0]
        self.assertIn("foreground", called_cmd)
        mock_process.wait.assert_not_called()
        self.assertIs(instance._daemon_proc, mock_process)

    @mock.patch("time.sleep")
    @mock.patch("os.kill")
    def test_confirm_daemon_started_detects_live_pid(
        self, mock_kill, mock_sleep
    ):
        """Confirmation succeeds once the pidfile appears with a live PID."""
        instance = self._make_supervisor("start")
        mock_kill.return_value = None
        with mock.patch(
            "supervisor._read_pid_file", return_value=999
        ):
            # Should return without raising and without looping forever.
            instance._confirm_daemon_started()
        mock_kill.assert_called_with(999, 0)

    @mock.patch("os.path.exists", return_value=False)
    @mock.patch("subprocess.run")
    def test_stop_child_via_cli(self, mock_run, mock_exists):
        """`start`-mode stop shells out to the daemon's own `stop` action."""
        instance = self._make_supervisor("start")
        instance._stop_child()

        called_cmd = mock_run.call_args[0][0]
        self.assertEqual(
            called_cmd,
            ["sudo", mock.ANY, supervisor.DAEMON_SCRIPT, "stop"],
        )

    def test_stop_child_via_process_handle_terminates_cleanly(self):
        """foreground-mode stop terminates the tracked process directly."""
        instance = self._make_supervisor("foreground")
        mock_process = mock.Mock()
        mock_process.poll.return_value = None
        instance._daemon_proc = mock_process

        instance._stop_child()

        mock_process.terminate.assert_called_once()
        mock_process.wait.assert_called_once()
        mock_process.kill.assert_not_called()
        self.assertIsNone(instance._daemon_proc)

    def test_stop_child_via_process_handle_gives_up_without_kill(self):
        """A foreground daemon ignoring SIGTERM is left alone, not SIGKILLed.

        By design: no automatic escalation. If SIGTERM doesn't work within
        the timeout, that's for a human to check on, not for the
        supervisor to force closed.
        """
        instance = self._make_supervisor("foreground")
        mock_process = mock.Mock()
        mock_process.poll.return_value = None
        mock_process.pid = 4242
        mock_process.wait.side_effect = subprocess.TimeoutExpired(
            cmd="daemon", timeout=1
        )
        instance._daemon_proc = mock_process

        instance._stop_child()

        mock_process.terminate.assert_called_once()
        mock_process.kill.assert_not_called()
        self.assertEqual(mock_process.wait.call_count, 1)
        # Left as-is (not cleared to None) since we don't actually know it
        # stopped.
        self.assertIs(instance._daemon_proc, mock_process)

    @mock.patch("os.path.exists", return_value=True)
    @mock.patch("subprocess.run")
    def test_stop_child_via_cli_timeout_is_caught_not_raised(
        self, mock_run, mock_exists
    ):
        """A `stop` that hangs past the timeout is reported, not retried
        and not left to raise TimeoutExpired uncaught.
        """
        instance = self._make_supervisor("start")
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="stop", timeout=1)

        instance._stop_child()  # must not raise

        mock_run.assert_called_once()

    def test_stop_child_via_process_handle_already_exited(self):
        """Nothing to do if the tracked process already exited on its own."""
        instance = self._make_supervisor("foreground")
        mock_process = mock.Mock()
        mock_process.poll.return_value = 0  # already exited
        instance._daemon_proc = mock_process

        instance._stop_child()

        mock_process.terminate.assert_not_called()
        self.assertIsNone(instance._daemon_proc)

    def test_reload_child_stops_then_relaunches(self):
        """Reload always stops the current child before launching a new one."""
        instance = self._make_supervisor("start")
        manager = mock.Mock()
        instance._stop_child = manager._stop_child
        instance._launch_child = manager._launch_child

        instance._reload_child()

        self.assertEqual(
            manager.mock_calls,
            [mock.call._stop_child(), mock.call._launch_child()],
        )

    def test_handle_reload_sets_flag(self):
        """SIGHUP marks a restart as requested; it does not act immediately."""
        instance = self._make_supervisor("start")
        instance._handle_reload(None, None)
        self.assertTrue(instance._restart_requested)

    def test_cleanup_only_flags_and_never_touches_the_child(self):
        """_cleanup must stay fast: it flags for exit and does nothing else.

        Regression test: an earlier version called _stop_child() directly
        from _cleanup, which runs synchronously inside daemonizer.Daemon's
        SIGTERM handler. Since stop() sends SIGTERM every 0.1s until the
        process is confirmed dead, and _stop_child's subprocess.run() call
        can take seconds, each additional SIGTERM arriving mid-call would
        re-enter this handler and spawn another overlapping shutdown
        attempt - observed in testing as 100+ duplicate 'stop' Slack
        notifications and a supervisor that never actually exited.
        """
        instance = self._make_supervisor("start")
        instance._stop_child = mock.Mock()

        instance._cleanup()
        instance._cleanup()  # simulates a second SIGTERM arriving

        self.assertTrue(instance._stopping)
        instance._stop_child.assert_not_called()

    def test_ensure_child_stopped_only_runs_once(self):
        """The real shutdown work happens exactly once, however often asked."""
        instance = self._make_supervisor("start")
        instance._stop_child = mock.Mock()

        instance._ensure_child_stopped()
        instance._ensure_child_stopped()
        instance._ensure_child_stopped()

        instance._stop_child.assert_called_once()

    @mock.patch("time.sleep")
    def test_run_stops_child_once_after_loop_exits(self, mock_sleep):
        """The child is stopped from _run's own control flow, once, after
        the loop notices _stopping - never from inside the signal handler.
        """
        instance = self._make_supervisor("start")
        instance._launch_child = mock.Mock()
        instance._stop_child = mock.Mock()

        # Simulate _cleanup() (the SIGTERM handler) firing partway through
        # the loop, the same way daemonizer.Daemon's termHandler would.
        def fake_sleep(_seconds):
            instance._cleanup()

        mock_sleep.side_effect = fake_sleep

        # signal.SIGHUP doesn't exist on Windows at all (create=True lets
        # us patch it in for the test); signal.signal is mocked so the
        # test never touches a real OS signal handler either way.
        with mock.patch.object(supervisor.signal, "SIGHUP", 1, create=True), \
                mock.patch.object(supervisor.signal, "signal"):
            instance._run()

        instance._stop_child.assert_called_once()


if __name__ == "__main__":
    unittest.main()
