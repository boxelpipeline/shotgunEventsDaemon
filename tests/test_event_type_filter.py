"""
Regression tests for narrowing _getNewEvents()'s EventLogEntry query to
only the event types some registered callback actually cares about.

Covers three things:
- Callback/Plugin/PluginCollection/Engine correctly compute the union of
  registered event types, and correctly fall back to None ("don't
  filter") the moment any callback matches everything.
- Engine._getNewEvents() actually adds the event_type filter to the SG
  query when it's safe to, and omits it when it's not.
- Plugin._updateLastEventId() no longer backlogs a gap once it's large
  enough to be an excluded event type rather than a genuine
  visibility-ordering hiccup - without this, every gap the new filter
  creates (tens of thousands/day on a busy site) would get added to
  _backlog and retried for 5 minutes, unbounded.
"""

import datetime
import sys
import threading
import unittest
from unittest import mock

# Same stubbing as test_engine_stop.py: shotgunEventDaemon.py imports
# shotgun_api3 and bxl_triggers.common.slack_msj at module level, neither
# on the test path, and nothing under test here touches them.
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


def _make_callback(matchEvents, active=True):
    cb = sed.Callback.__new__(sed.Callback)
    cb._matchEvents = matchEvents
    cb._active = active
    return cb


def _make_plugin(callbacks, active=True):
    plugin = sed.Plugin.__new__(sed.Plugin)
    plugin._callbacks = list(callbacks)
    plugin._active = active
    plugin._lock = threading.Lock()
    return plugin


def _make_collection(plugins):
    coll = sed.PluginCollection.__new__(sed.PluginCollection)
    coll._plugins = {str(i): p for i, p in enumerate(plugins)}
    return coll


class CallbackMatchedEventTypesTest(unittest.TestCase):
    def test_returns_declared_event_types(self):
        cb = _make_callback({"Shotgun_Version_Change": ["sg_status_list"]})
        self.assertEqual(cb.getMatchedEventTypes(), {"Shotgun_Version_Change"})

    def test_none_matchevents_means_everything(self):
        cb = _make_callback(None)
        self.assertIsNone(cb.getMatchedEventTypes())

    def test_wildcard_key_means_everything(self):
        cb = _make_callback({"*": None})
        self.assertIsNone(cb.getMatchedEventTypes())


class PluginMatchedEventTypesTest(unittest.TestCase):
    def test_unions_across_its_callbacks(self):
        plugin = _make_plugin([
            _make_callback({"Shotgun_Version_Change": ["sg_status_list"]}),
            _make_callback({"Shotgun_Task_Change": ["content"]}),
        ])
        self.assertEqual(
            plugin.getMatchedEventTypes(),
            {"Shotgun_Version_Change", "Shotgun_Task_Change"})

    def test_skips_inactive_callbacks(self):
        plugin = _make_plugin([
            _make_callback({"Shotgun_Version_Change": ["sg_status_list"]}),
            _make_callback({"Shotgun_Task_Change": ["content"]}, active=False),
        ])
        self.assertEqual(
            plugin.getMatchedEventTypes(), {"Shotgun_Version_Change"})

    def test_any_wildcard_callback_forces_none(self):
        plugin = _make_plugin([
            _make_callback({"Shotgun_Version_Change": ["sg_status_list"]}),
            _make_callback(None),
        ])
        self.assertIsNone(plugin.getMatchedEventTypes())

    def test_no_callbacks_forces_none(self):
        plugin = _make_plugin([])
        self.assertIsNone(plugin.getMatchedEventTypes())


class PluginCollectionMatchedEventTypesTest(unittest.TestCase):
    def test_unions_across_active_plugins(self):
        p1 = _make_plugin(
            [_make_callback({"Shotgun_Version_Change": ["sg_status_list"]})])
        p2 = _make_plugin(
            [_make_callback({"Shotgun_Task_Change": ["content"]})])
        collection = _make_collection([p1, p2])
        self.assertEqual(
            collection.getMatchedEventTypes(),
            {"Shotgun_Version_Change", "Shotgun_Task_Change"})

    def test_skips_inactive_plugins(self):
        p1 = _make_plugin(
            [_make_callback({"Shotgun_Version_Change": ["sg_status_list"]})])
        p2 = _make_plugin(
            [_make_callback({"Shotgun_Task_Change": ["content"]})],
            active=False)
        collection = _make_collection([p1, p2])
        self.assertEqual(
            collection.getMatchedEventTypes(), {"Shotgun_Version_Change"})

    def test_any_wildcard_plugin_forces_none(self):
        p1 = _make_plugin(
            [_make_callback({"Shotgun_Version_Change": ["sg_status_list"]})])
        p2 = _make_plugin([_make_callback(None)])
        collection = _make_collection([p1, p2])
        self.assertIsNone(collection.getMatchedEventTypes())


class EngineGetRegisteredEventTypesTest(unittest.TestCase):
    def _make_engine(self, collections):
        engine = sed.Engine.__new__(sed.Engine)
        engine._pluginCollections = collections
        return engine

    def test_unions_across_collections(self):
        p1 = _make_plugin(
            [_make_callback({"Shotgun_Version_Change": ["sg_status_list"]})])
        p2 = _make_plugin(
            [_make_callback({"Shotgun_Task_Change": ["content"]})])
        engine = self._make_engine(
            [_make_collection([p1]), _make_collection([p2])])
        self.assertEqual(
            engine._getRegisteredEventTypes(),
            {"Shotgun_Version_Change", "Shotgun_Task_Change"})

    def test_any_collection_wildcard_forces_none(self):
        p1 = _make_plugin(
            [_make_callback({"Shotgun_Version_Change": ["sg_status_list"]})])
        p2 = _make_plugin([_make_callback(None)])
        engine = self._make_engine(
            [_make_collection([p1]), _make_collection([p2])])
        self.assertIsNone(engine._getRegisteredEventTypes())


class EngineGetNewEventsFilterTest(unittest.TestCase):
    """_getNewEvents() must add the event_type filter when it's safe,
    and must not when any callback needs every type."""

    def _make_engine(self, registered_event_types, found_events=None):
        engine = sed.Engine.__new__(sed.Engine)
        engine._continue = True
        engine.config = mock.Mock()
        engine.config.getMaxEventBatchSize.return_value = 500
        engine._sg = mock.Mock()
        engine._sg.find.return_value = found_events or []
        engine.log = mock.Mock()
        # Unrelated to this test (see test_sudo_suppression.py), but
        # _getNewEvents() also reads this now - keep it a no-op here.
        engine._disabledEventLogScriptIds = set()

        collection = mock.Mock()
        collection.getNextUnprocessedEventId.return_value = 1000
        collection.getMatchedEventTypes.return_value = registered_event_types
        engine._pluginCollections = [collection]
        return engine

    def test_adds_event_type_filter_when_no_wildcard_registered(self):
        engine = self._make_engine({"Shotgun_Version_Change", "Shotgun_Task_Change"})

        engine._getNewEvents()

        filters = engine._sg.find.call_args[0][1]
        event_type_filter = next(
            f for f in filters if f[0] == "event_type")
        self.assertEqual(event_type_filter[1], "in")
        self.assertEqual(
            set(event_type_filter[2]),
            {"Shotgun_Version_Change", "Shotgun_Task_Change"})

    def test_omits_event_type_filter_when_a_wildcard_is_registered(self):
        engine = self._make_engine(None)

        engine._getNewEvents()

        filters = engine._sg.find.call_args[0][1]
        self.assertFalse(any(f[0] == "event_type" for f in filters))


class UpdateLastEventIdGapBoundTest(unittest.TestCase):
    """The event_type filter turns "no plugin wants this type" into a
    permanent, often huge id gap - _updateLastEventId must stop treating
    a gap that large as a transient hiccup worth backlogging/retrying,
    while still doing so for small gaps (its original purpose)."""

    def _make_plugin_with_state(self, last_event_id):
        plugin = sed.Plugin.__new__(sed.Plugin)
        plugin._lastEventId = last_event_id
        plugin._backlog = {}
        plugin.logger = mock.Mock()
        return plugin

    def test_small_gap_is_still_backlogged(self):
        plugin = self._make_plugin_with_state(last_event_id=100)

        plugin._updateLastEventId({"id": 110})

        self.assertEqual(set(plugin._backlog.keys()), set(range(101, 110)))
        self.assertEqual(plugin._lastEventId, 110)

    def test_large_gap_is_not_backlogged(self):
        plugin = self._make_plugin_with_state(last_event_id=100)

        # 40,000-id gap: exactly the shape Shotgun_PublishedFile_Change/
        # Shotgun_Attachment_View exclusion produces on a busy day, per
        # the 8h load report (tens of thousands of excluded events).
        plugin._updateLastEventId({"id": 40100})

        self.assertEqual(plugin._backlog, {})
        self.assertEqual(plugin._lastEventId, 40100)

    def test_gap_exactly_at_the_boundary_is_still_backlogged(self):
        plugin = self._make_plugin_with_state(last_event_id=100)

        # Gap size 50 (ids 101..150): at MAX_BACKLOG_GAP, not over it.
        plugin._updateLastEventId({"id": 151})

        self.assertEqual(len(plugin._backlog), 50)
        self.assertEqual(plugin._lastEventId, 151)


if __name__ == "__main__":
    unittest.main()
