"""
Regression test for Engine.stop(): it must persist the final event-id
checkpoint after every plugin collection has finished shutting down, not
rely solely on _mainLoop()'s periodic saves.

Without this, any event a plugin's worker thread finished processing
after the last periodic save but before the process actually exited was
never written to disk. On restart, the on-disk checkpoint still pointed
before that event, so it was re-fetched and reprocessed - this is what
turned a mid-flight restart (e.g. the autopull/SIGHUP reload cycle) into
duplicate event processing across two process instances.
"""

import os
import pickle
import shutil
import sys
import tempfile
import unittest
from unittest import mock

# shotgunEventDaemon.py imports shotgun_api3 and bxl_triggers.common.slack_msj
# at module level - neither is on the test path (bxl_triggers is a separate
# repo entirely), and Engine.stop()/_saveEventIdData() never touch them, so
# stub them out the same way test_plugins.py stubs shotgun_api3/pytz.
for _module_name in (
    "shotgun_api3",
    "shotgun_api3.lib",
    "shotgun_api3.lib.sgtimezone",
    "bxl_triggers",
    "bxl_triggers.common",
    "bxl_triggers.common.slack_msj",
    # Only actually imported on sys.platform == "win32", but this test
    # environment is Windows without pywin32 installed - stub regardless
    # of platform, same reasoning as the modules above.
    "win32serviceutil",
    "win32service",
    "win32event",
    "servicemanager",
):
    sys.modules.setdefault(_module_name, mock.MagicMock())

import shotgunEventDaemon as sed


class _FakeCollection:
    """Minimal stand-in for PluginCollection.

    Its shutdown() advances its state, the same way PluginCollection's
    real shutdown() lets a plugin's worker thread finish draining its
    queue (and therefore advance Plugin._lastEventId) before returning.
    """

    def __init__(self, path):
        self.path = path
        self._state = 10  # "last processed event id" before shutdown
        self.shutdown_called = False
        self.shutdown_call_count = 0

    def shutdown(self):
        self.shutdown_called = True
        self.shutdown_call_count += 1
        self._state = 42

    def getState(self):
        return self._state


class TestEngineStopPersistsFinalState(unittest.TestCase):
    """Engine.stop() must save state that reflects what shutdown() did,
    not just whatever was true right before it was called."""

    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp_dir, ignore_errors=True)
        self.event_id_file = os.path.join(self.tmp_dir, "eventIdFile")

    def _make_engine(self, collections):
        engine = sed.Engine.__new__(sed.Engine)
        engine._continue = True
        engine._eventIdData = {}
        engine._pluginCollections = collections
        engine.config = mock.Mock()
        engine.config.getEventIdFile.return_value = self.event_id_file
        return engine

    def test_stop_saves_state_advanced_during_shutdown(self):
        collection = _FakeCollection("some/plugin/path")
        engine = self._make_engine([collection])

        engine.stop()

        self.assertTrue(collection.shutdown_called)
        self.assertFalse(engine._continue)
        with open(self.event_id_file, "rb") as fh:
            saved = pickle.load(fh)
        self.assertEqual(saved["some/plugin/path"], 42)

    def test_stop_saves_after_every_collection_has_shut_down(self):
        """Order matters: the save must happen once all collections are
        confirmed shut down, not interleaved with/before any of them -
        otherwise a later collection's shutdown() could still advance
        state that never makes it into the save.
        """
        first = _FakeCollection("plugins/a")
        second = _FakeCollection("plugins/b")
        engine = self._make_engine([first, second])

        engine.stop()

        with open(self.event_id_file, "rb") as fh:
            saved = pickle.load(fh)
        self.assertEqual(saved["plugins/a"], 42)
        self.assertEqual(saved["plugins/b"], 42)

    def test_stop_is_a_noop_the_second_time(self):
        """A second call to stop() must not touch the collections again.

        daemonizer.Daemon invokes _cleanup() (which calls Engine.stop())
        from two independent places: the SIGTERM/SIGINT handler, and an
        unconditional atexit.register(self._delpid) that always fires
        again once the process is actually exiting - including after a
        signal-triggered stop already ran this method to completion.
        That second call used to be a harmless no-op, but
        PluginCollection.shutdown() building a fresh ThreadPoolExecutor
        unconditionally is not: by the time atexit callbacks run,
        concurrent.futures.thread's own shutdown machinery may already
        be tearing down, and submitting to a brand new executor at that
        point raises "cannot schedule new futures after interpreter
        shutdown" - observed directly in production after a foreground
        Ctrl+C. stop() must recognize it already ran and bail out before
        touching any collection a second time.
        """
        collection = _FakeCollection("some/plugin/path")
        engine = self._make_engine([collection])

        engine.stop()
        engine.stop()  # simulates the atexit-triggered second call

        self.assertEqual(collection.shutdown_call_count, 1)


if __name__ == "__main__":
    unittest.main()
