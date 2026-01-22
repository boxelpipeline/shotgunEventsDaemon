"""
Unit tests for daemonizer module.
"""
import os
import tempfile
import unittest
from unittest import mock
import signal

import daemonizer


class DaemonImpl(daemonizer.Daemon):
    """Test implementation of Daemon class."""

    def _run(self):
        """Minimal implementation for testing."""
        pass

    def _cleanup(self):
        """Minimal implementation for testing."""
        pass


class TestDaemon(unittest.TestCase):
    """Test case for Daemon class."""

    def setUp(self):
        """Set up test fixtures."""
        self.service_name = "test_service"
        self.temp_pidfile = tempfile.NamedTemporaryFile(delete=False)
        self.pidfile = self.temp_pidfile.name
        self.temp_pidfile.close()

    def tearDown(self):
        """Clean up test fixtures."""
        if os.path.exists(self.pidfile):
            os.unlink(self.pidfile)

    def test_daemon_initialization(self):
        """Test Daemon class initialization."""
        daemon = DaemonImpl(self.service_name, self.pidfile)

        self.assertEqual(daemon._serviceName, self.service_name)
        self.assertEqual(daemon._pidfile, self.pidfile)
        self.assertEqual(daemon._stdin, daemonizer.DEVNULL)
        self.assertEqual(daemon._stdout, daemonizer.DEVNULL)
        self.assertEqual(daemon._stderr, daemonizer.DEVNULL)

    def test_daemon_custom_streams(self):
        """Test Daemon class with custom stream paths."""
        stdin_path = "/tmp/test_stdin"
        stdout_path = "/tmp/test_stdout"
        stderr_path = "/tmp/test_stderr"

        daemon = DaemonImpl(
            self.service_name,
            self.pidfile,
            stdin=stdin_path,
            stdout=stdout_path,
            stderr=stderr_path
        )

        self.assertEqual(daemon._stdin, stdin_path)
        self.assertEqual(daemon._stdout, stdout_path)
        self.assertEqual(daemon._stderr, stderr_path)

    def test_delpid_removes_pidfile(self):
        """Test that _delpid removes the pidfile."""
        daemon = DaemonImpl(self.service_name, self.pidfile)

        # Create a pidfile
        with open(self.pidfile, 'w') as f:
            f.write('12345\n')

        self.assertTrue(os.path.exists(self.pidfile))
        daemon._delpid()
        self.assertFalse(os.path.exists(self.pidfile))

    @mock.patch('sys.stdin')
    @mock.patch('sys.stdout')
    @mock.patch('sys.stderr')
    @mock.patch('os.fork')
    @mock.patch('os.setsid')
    @mock.patch('os.chdir')
    @mock.patch('os.umask')
    @mock.patch('os.dup2')
    @mock.patch('builtins.open', new_callable=mock.mock_open)
    def test_daemonize_double_fork(self, mock_open_func, mock_dup2, mock_umask,
                                    mock_chdir, mock_setsid, mock_fork,
                                    mock_stderr, mock_stdout, mock_stdin):
        """Test that daemonize performs double fork correctly."""
        # Mock fork to return 0 (child process) both times
        mock_fork.side_effect = [0, 0]

        # Mock file descriptors
        mock_stdin.fileno.return_value = 0
        mock_stdout.fileno.return_value = 1
        mock_stderr.fileno.return_value = 2

        daemon = DaemonImpl(self.service_name, self.pidfile)
        daemon._daemonize()

        # Verify fork was called twice
        self.assertEqual(mock_fork.call_count, 2)
        # Verify session was created
        mock_setsid.assert_called_once()
        # Verify changed to root directory
        mock_chdir.assert_called_once_with('/')
        # Verify umask was set
        mock_umask.assert_called_once_with(0)

    @mock.patch('sys.exit')
    @mock.patch('os.kill')
    @mock.patch('time.sleep')
    def test_stop_daemon(self, mock_sleep, mock_kill, mock_exit):
        """Test stopping a daemon."""
        daemon = DaemonImpl(self.service_name, self.pidfile)

        # Create a pidfile
        test_pid = 12345
        with open(self.pidfile, 'w') as f:
            f.write(f'{test_pid}\n')

        # Mock kill to raise OSError after first call (process terminated)
        mock_kill.side_effect = [None, OSError("No such process")]

        daemon.stop()

        # Verify kill was called with correct PID and signal
        self.assertEqual(mock_kill.call_args_list[0][0], (test_pid, signal.SIGTERM))

    def test_stop_no_pidfile(self):
        """Test stopping daemon when pidfile doesn't exist."""
        daemon = DaemonImpl(self.service_name, self.pidfile)

        # Ensure pidfile doesn't exist
        if os.path.exists(self.pidfile):
            os.unlink(self.pidfile)

        # Should not raise an error
        daemon.stop()

    @mock.patch.object(DaemonImpl, 'stop')
    @mock.patch.object(DaemonImpl, 'start')
    def test_restart_calls_stop_and_start(self, mock_start, mock_stop):
        """Test that restart calls stop and start."""
        daemon = DaemonImpl(self.service_name, self.pidfile)
        daemon.restart()

        mock_stop.assert_called_once()
        mock_start.assert_called_once_with(True)

    def test_foreground_calls_start_without_daemonize(self):
        """Test that foreground calls start with daemonize=False."""
        daemon = DaemonImpl(self.service_name, self.pidfile)

        with mock.patch.object(daemon, 'start') as mock_start:
            daemon.foreground()
            mock_start.assert_called_once_with(daemonize=False)

    def test_run_not_implemented(self):
        """Test that _run raises NotImplementedError in base class."""
        class MinimalDaemon(daemonizer.Daemon):
            def _cleanup(self):
                pass

        daemon = MinimalDaemon(self.service_name, self.pidfile)
        with self.assertRaises(NotImplementedError):
            daemon._run()

    def test_cleanup_not_implemented(self):
        """Test that _cleanup raises NotImplementedError in base class."""
        class MinimalDaemon(daemonizer.Daemon):
            def _run(self):
                pass

        daemon = MinimalDaemon(self.service_name, self.pidfile)
        with self.assertRaises(NotImplementedError):
            daemon._cleanup()


if __name__ == '__main__':
    unittest.main()
