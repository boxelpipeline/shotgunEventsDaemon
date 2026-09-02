#!/usr/bin/env python

# Taken and modified from:
# http://www.jejik.com/articles/2007/02/a_simple_unix_linux_daemon_in_python/

from __future__ import print_function
import atexit
import importlib
import os
import signal
import sys
import time
import traceback


if hasattr(os, "devnull"):
    DEVNULL = os.devnull
else:
    DEVNULL = "/dev/null"


def _notify_slack_failure(context, detail):
    """Best-effort Slack notification for daemonizer failures."""
    try:
        slack_msj = importlib.import_module("bxl_triggers.common.slack_msj")

        msg = (
            "Boxel: daemonizer failure\n"
            "context={0}\n"
            "detail={1}\n"
            "cwd={2}\n"
            "argv={3}"
        ).format(context, detail, os.getcwd(), sys.argv)
        slack_msj.send_slack_message(msg)
    except Exception:
        # Never block daemon operations on Slack diagnostics.
        pass


class Daemon(object):
    """
    A generic daemon class.

    Usage: subclass the Daemon class and override the _run() method
    """

    def __init__(
        self, serviceName, pidfile, stdin=DEVNULL, stdout=DEVNULL, stderr=DEVNULL
    ):
        super(Daemon, self).__init__()

        self._serviceName = serviceName
        self._stdin = stdin
        self._stdin = stdin
        self._stdout = stdout
        self._stderr = stderr
        self._pidfile = pidfile
        self._shutting_down = False

    def _daemonize(self):
        """
        Do the UNIX double-fork magic, see Stevens' "Advanced
        Programming in the UNIX Environment" for details (ISBN 0201563177)
        http://www.erlenstar.demon.co.uk/unix/faq_2.html#SEC16
        """
        try:
            pid = os.fork()
            if pid > 0:
                # exit first parent
                sys.exit(0)
        except OSError as e:
            sys.stderr.write("fork #1 failed: %d (%s)\n" % (e.errno, e.strerror))
            _notify_slack_failure("fork_1", traceback.format_exc())
            sys.exit(1)

        # decouple from parent environment
        os.chdir("/")
        os.setsid()
        os.umask(0)

        # do second fork
        try:
            pid = os.fork()
            if pid > 0:
                # exit from second parent
                sys.exit(0)
        except OSError as e:
            sys.stderr.write("fork #2 failed: %d (%s)\n" % (e.errno, e.strerror))
            _notify_slack_failure("fork_2", traceback.format_exc())
            sys.exit(1)

        # redirect standard file descriptors.
        # Unless specified when instantiating the class, it will
        # by default redirect the stdin, stdout, stderr to a null file
        # which is the equivalent of discarding the output.
        sys.stdout.flush()
        sys.stderr.flush()
        si = open(self._stdin, "r")
        so = open(self._stdout, "a+")
        se = open(self._stderr, "a+b", 0)
        os.dup2(si.fileno(), sys.stdin.fileno())
        os.dup2(so.fileno(), sys.stdout.fileno())
        os.dup2(se.fileno(), sys.stderr.fileno())

        # write pidfile and subsys file
        pid = str(os.getpid())
        with open(self._pidfile, "w+") as f:
            f.write("%s\n" % pid)
        if os.path.exists("/var/lock/subsys"):
            try:
                with open(os.path.join("/var/lock/subsys", self._serviceName), "w") as f:
                    pass
            except PermissionError:
                # Best-effort compatibility with systems where /var/lock/subsys
                # is root-owned or not used by the service manager.
                _notify_slack_failure(
                    "subsys_lock_write",
                    "Permission denied writing /var/lock/subsys marker",
                )
                pass

    def _delpid(self):
        if os.path.exists(self._pidfile):
            os.remove(self._pidfile)

        subsysPath = os.path.join("/var/lock/subsys", self._serviceName)
        if os.path.exists(subsysPath):
            os.remove(subsysPath)

        self._cleanup()

    def start(self, daemonize=True):
        """
        Start the daemon
        """
        # Check for a pidfile to see if the daemon already runs
        try:
            with open(self._pidfile, "r") as pf:
                pid = int(pf.read().strip())
        except IOError:
            pid = None
        except ValueError:
            _notify_slack_failure(
                "pidfile_parse",
                "Invalid PID value in pidfile %s" % self._pidfile,
            )
            pid = None

        if pid:
            message = "pidfile %s already exist. Daemon already running?\n"
            sys.stderr.write(message % self._pidfile)
            _notify_slack_failure(
                "pidfile_exists",
                "Existing PID file detected at %s with pid=%s"
                % (self._pidfile, pid),
            )
            sys.exit(1)

        # Start the daemon
        if daemonize:
            try:
                self._daemonize()
            except Exception:
                _notify_slack_failure("daemonize", traceback.format_exc())
                raise

        # Cleanup handling
        def termHandler(signum, frame):
            # stop() sends SIGTERM repeatedly (every 0.1s) until this
            # process actually exits, so more signals keep arriving
            # while _delpid()/_cleanup() (which waits on the plugin
            # executors to drain) is still running. Without this guard,
            # each repeat re-enters this handler on top of the one
            # already in progress instead of replacing it, and stacks
            # up recursively until Python's recursion limit crashes the
            # process. Same fix already applied to supervisor.py's own
            # stop handling for the identical reason.
            if self._shutting_down:
                return
            self._shutting_down = True
            self._delpid()

        signal.signal(signal.SIGTERM, termHandler)
        atexit.register(self._delpid)

        # Run the daemon
        self._run()

    def stop(self):
        """
        Stop the daemon
        """
        # Get the pid from the pidfile
        try:
            with open(self._pidfile, "r") as pf:
                pid = int(pf.read().strip())
        except IOError:
            pid = None

        if not pid:
            message = "pidfile %s does not exist. Daemon not running?\n"
            sys.stderr.write(message % self._pidfile)
            return  # not an error in a restart

        # Try killing the daemon process
        try:
            while 1:
                os.kill(pid, signal.SIGTERM)
                time.sleep(0.1)
        except OSError as err:
            err = str(err)
            if err.find("No such process") > 0:
                if os.path.exists(self._pidfile):
                    os.remove(self._pidfile)
            else:
                print(str(err))
                _notify_slack_failure("stop", str(err))
                sys.exit(1)

    def foreground(self):
        self.start(daemonize=False)

    def restart(self, daemonize=True):
        """
        Restart the daemon
        """
        self.stop()
        self.start(daemonize)

    def _run(self):
        """
        You should override this method when you subclass Daemon. It will be
        called after the process has been daemonized by start() or restart().
        """
        raise NotImplementedError("You must implement the method in your class.")

    def _cleanup(self):
        """
        You should override this method when you subclass Daemon. It will be
        called when the daemon exits.
        """
        raise NotImplementedError("You must implement the method in your class.")
