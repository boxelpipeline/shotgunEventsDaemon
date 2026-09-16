"""
Regression tests for suppressing events from Scripts (ApiUser) that
have generate_event_log_entries disabled in ShotGrid itself - discovered
once at daemon startup rather than kept in a separately maintained list.

generate_event_log_entries=False on a Script suppresses events it logs
acting as itself - _getNewEvents()'s own "user" not_in filter already
excludes those directly in the query. A write made via sudo_as_login
(impersonating another user) bypasses that flag entirely though: it
still produces an EventLogEntry, attributed to the sudo'd human as
event["user"], with the real Script recorded in
event["meta"]["sudo_actual_user"] (confirmed live for one Script that
has generate_event_log_entries disabled). EventLogEntry.meta is a
"serializable" (blob) field - confirmed live that it can't be filtered
in the SG query itself (a ["meta.sudo_actual_user.id", "is", X] filter
raises a Fault) - so that case can only be caught after the event is
fetched, in Plugin._process(), while still letting the plugin's own
cursor/backlog bookkeeping advance normally (see _updateLastEventId).
"""

import sys
import threading
import unittest
from unittest import mock

for _module_name in (
    "shotgun_api3",
    "shotgun_api3.lib",
    "shotgun_api3.lib.sgtimezone",
    "bxl_triggers",
    "bxl_triggers.common",
    "bxl_triggers.common.slack_msj",
    "win32serviceutil",
    "win32service",
    "win32event",
    "servicemanager",
):
    sys.modules.setdefault(_module_name, mock.MagicMock())

import shotgunEventDaemon as sed


class EngineLoadDisabledEventLogScriptIdsTest(unittest.TestCase):
    def _make_engine(self, find_return=None, find_side_effect=None):
        engine = sed.Engine.__new__(sed.Engine)
        engine._continue = True
        engine._disabledEventLogScriptIds = set()
        engine._sg = mock.Mock()
        if find_side_effect is not None:
            engine._sg.find.side_effect = find_side_effect
        else:
            engine._sg.find.return_value = find_return or []
        engine.log = mock.Mock()
        engine._max_conn_retries = 30
        engine._conn_retry_sleep = 0
        return engine

    def test_populates_ids_from_disabled_scripts(self):
        engine = self._make_engine(find_return=[
            {"id": 1698, "firstname": "HieroExport"},
            {"id": 675, "firstname": "SmartBot"},
        ])

        engine._loadDisabledEventLogScriptIds()

        self.assertEqual(engine._disabledEventLogScriptIds, {1698, 675})
        # Queries ApiUser, filtered to generate_event_log_entries=False -
        # the single source of truth this replaces a manual list with.
        args, kwargs = engine._sg.find.call_args
        self.assertEqual(args[0], "ApiUser")
        self.assertIn(
            ["generate_event_log_entries", "is", False], args[1])

    def test_no_disabled_scripts_leaves_an_empty_set(self):
        engine = self._make_engine(find_return=[])

        engine._loadDisabledEventLogScriptIds()

        self.assertEqual(engine._disabledEventLogScriptIds, set())

    def test_stop_requested_before_success_leaves_default(self):
        """If the daemon is told to stop while this query is still
        being attempted, don't loop forever - leave the safe empty
        default from __init__ in place."""
        engine = self._make_engine()
        engine._continue = False

        engine._loadDisabledEventLogScriptIds()

        self.assertEqual(engine._disabledEventLogScriptIds, set())
        engine._sg.find.assert_not_called()


class EngineIsSuppressedSudoEventTest(unittest.TestCase):
    def _make_engine(self, disabled_ids):
        engine = sed.Engine.__new__(sed.Engine)
        engine._disabledEventLogScriptIds = disabled_ids
        return engine

    def test_no_disabled_scripts_never_suppresses(self):
        engine = self._make_engine(set())
        event = {"meta": {"sudo_actual_user": {"id": 1698}}}
        self.assertFalse(engine.isSuppressedSudoEvent(event))

    def test_matching_id_in_dict_meta_is_suppressed(self):
        engine = self._make_engine({1698})
        event = {
            "meta": {
                "sudo_actual_user": {"name": "HieroExport 1.0", "id": 1698},
            }
        }
        self.assertTrue(engine.isSuppressedSudoEvent(event))

    def test_matching_id_in_json_string_meta_is_suppressed(self):
        # farm_runner.py's own _load_event_log() shows meta sometimes
        # arrives as a JSON string rather than an already-parsed dict -
        # isSuppressedSudoEvent must handle both the same way.
        engine = self._make_engine({1698})
        event = {"meta": '{"sudo_actual_user": {"id": 1698}}'}
        self.assertTrue(engine.isSuppressedSudoEvent(event))

    def test_different_script_id_is_not_suppressed(self):
        engine = self._make_engine({1698})
        event = {"meta": {"sudo_actual_user": {"id": 42}}}
        self.assertFalse(engine.isSuppressedSudoEvent(event))

    def test_no_sudo_actual_user_key_is_not_suppressed(self):
        engine = self._make_engine({1698})
        event = {"meta": {"some_other_key": True}}
        self.assertFalse(engine.isSuppressedSudoEvent(event))

    def test_missing_or_malformed_meta_is_not_suppressed(self):
        engine = self._make_engine({1698})
        self.assertFalse(engine.isSuppressedSudoEvent({}))
        self.assertFalse(engine.isSuppressedSudoEvent({"meta": None}))
        self.assertFalse(engine.isSuppressedSudoEvent({"meta": "not json"}))
        self.assertFalse(engine.isSuppressedSudoEvent({"meta": ["a", "list"]}))


class GetNewEventsUserFilterTest(unittest.TestCase):
    """_getNewEvents() must also exclude direct writes from disabled
    scripts, alongside the event_type filter."""

    def _make_engine(self, disabled_ids):
        engine = sed.Engine.__new__(sed.Engine)
        engine._continue = True
        engine.config = mock.Mock()
        engine.config.getMaxEventBatchSize.return_value = 500
        engine._sg = mock.Mock()
        engine._sg.find.return_value = []
        engine.log = mock.Mock()
        engine._disabledEventLogScriptIds = disabled_ids

        collection = mock.Mock()
        collection.getNextUnprocessedEventId.return_value = 1000
        collection.getMatchedEventTypes.return_value = {"Shotgun_Version_Change"}
        engine._pluginCollections = [collection]
        return engine

    def test_adds_user_not_in_filter_when_scripts_are_disabled(self):
        engine = self._make_engine({1698, 675})

        engine._getNewEvents()

        filters = engine._sg.find.call_args[0][1]
        user_filter = next(f for f in filters if f[0] == "user")
        self.assertEqual(user_filter[1], "not_in")
        self.assertEqual(
            {entity["id"] for entity in user_filter[2]}, {1698, 675})
        self.assertTrue(
            all(entity["type"] == "ApiUser" for entity in user_filter[2]))

    def test_omits_user_filter_when_no_scripts_are_disabled(self):
        engine = self._make_engine(set())

        engine._getNewEvents()

        filters = engine._sg.find.call_args[0][1]
        self.assertFalse(any(f[0] == "user" for f in filters))


class _RecordingCallback:
    """Stands in for a real Callback - records whether it was asked to
    process anything, without needing a real Shotgun connection."""

    def __init__(self):
        self.processed_event_ids = []

    def isActive(self):
        return True

    def canProcess(self, event):
        return True

    def process(self, event):
        self.processed_event_ids.append(event["id"])
        return True


class PluginProcessSkipsSuppressedEventsTest(unittest.TestCase):
    """Plugin._process() must skip every callback's work for a
    suppressed event, but still report itself active so process()
    advances _lastEventId/backlog exactly as for any other event none
    of its callbacks matched."""

    def _make_plugin(self, engine, callback):
        plugin = sed.Plugin.__new__(sed.Plugin)
        plugin._engine = engine
        plugin._callbacks = [callback]
        plugin._lock = threading.RLock()  # process()/_process() nest it
        plugin._active = True
        plugin._lastEventId = None
        plugin._backlog = {}
        plugin.logger = mock.Mock()
        return plugin

    def test_suppressed_event_skips_the_callback(self):
        engine = mock.Mock()
        engine.isSuppressedSudoEvent.return_value = True
        callback = _RecordingCallback()
        plugin = self._make_plugin(engine, callback)

        still_active = plugin.process({"id": 555})

        self.assertEqual(callback.processed_event_ids, [])
        self.assertTrue(still_active)
        self.assertEqual(plugin._lastEventId, 555)

    def test_non_suppressed_event_still_reaches_the_callback(self):
        engine = mock.Mock()
        engine.isSuppressedSudoEvent.return_value = False
        callback = _RecordingCallback()
        plugin = self._make_plugin(engine, callback)

        plugin.process({"id": 556})

        self.assertEqual(callback.processed_event_ids, [556])

    def test_a_burst_of_suppressed_events_never_hits_the_backlog(self):
        """The scenario this exists for: a whole HieroExport batch of
        sudo_as_login-suppressed events must not look like a gap worth
        retrying."""
        engine = mock.Mock()
        engine.isSuppressedSudoEvent.return_value = True
        callback = _RecordingCallback()
        plugin = self._make_plugin(engine, callback)
        plugin._lastEventId = 1000

        for event_id in range(1001, 1021):  # 20 suppressed events in a row
            plugin.process({"id": event_id})

        self.assertEqual(callback.processed_event_ids, [])
        self.assertEqual(plugin._backlog, {})
        self.assertEqual(plugin._lastEventId, 1020)


if __name__ == "__main__":
    unittest.main()
