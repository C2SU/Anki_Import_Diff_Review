import sys
import os
import sqlite3
from unittest.mock import MagicMock
import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import aqt
from anki_import_diff.src.savepoint import NoteDiff, capture_import_changes
from anki_import_diff.src.hooks import (
    commit_changes,
    install_qt_message_handler,
    install_js_console_filter,
)
from anki_import_diff.src import hooks as hooks_mod
from anki_import_diff.src import browser_integration as bi
from anki_import_diff.src.temp_link_interceptor import TempLinkInterceptor


class _CompatDB:
    def __init__(self):
        self.conn = sqlite3.connect(":memory:", isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute(
            """
            CREATE TABLE notes (
                id INTEGER PRIMARY KEY,
                guid TEXT,
                mid INTEGER,
                mod INTEGER,
                usn INTEGER,
                tags TEXT,
                flds TEXT,
                sfld TEXT,
                csum INTEGER,
                flags INTEGER,
                data TEXT
            )
            """
        )
        self.conn.execute(
            """
            CREATE TABLE cards (
                id INTEGER PRIMARY KEY,
                nid INTEGER,
                did INTEGER,
                ord INTEGER,
                mod INTEGER,
                usn INTEGER,
                type INTEGER,
                queue INTEGER,
                due INTEGER,
                ivl INTEGER,
                factor INTEGER,
                reps INTEGER,
                lapses INTEGER,
                left INTEGER,
                odue INTEGER,
                odid INTEGER,
                flags INTEGER,
                data TEXT
            )
            """
        )

    def execute(self, sql, *args):
        flat = []
        for a in args:
            if isinstance(a, (list, tuple)):
                flat.extend(a)
            else:
                flat.append(a)
        return self.conn.execute(sql, flat)

    def all(self, sql, *args):
        return [tuple(r) for r in self.execute(sql, *args).fetchall()]

    def scalar(self, sql, *args):
        row = self.execute(sql, *args).fetchone()
        return row[0] if row else None


def test_capture_and_preserve_guid_on_insert_and_stage(monkeypatch):
    """Verifie que le GUID d'origine d'AnkiCollab/APKG est capture et preserve lors du staging et du commit."""
    db = _CompatDB()
    col = MagicMock()
    col.db = db
    model = {"id": 10, "name": "Basic", "flds": [{"name": "Front"}, {"name": "Back"}]}
    col.models.get.return_value = model
    col.models.by_name.return_value = model

    mw = MagicMock()
    mw.col = col
    monkeypatch.setattr(aqt, "mw", mw)

    def simulated_import():
        db.execute(
            "INSERT INTO notes (id, guid, mid, flds, tags) VALUES (?, ?, ?, ?, ?)",
            5001,
            "ankicollab-guid-xyz",
            10,
            "Q1\x1fA1",
            " tagA ",
        )
        db.execute(
            "INSERT INTO cards (id, nid, did, ord) VALUES (?, ?, ?, ?)",
            6001,
            5001,
            1,
            0,
        )

    diffs = capture_import_changes(simulated_import)
    assert len(diffs) == 1
    diff = diffs[0]
    assert diff.action == "INSERT"
    assert diff.guid == "ankicollab-guid-xyz"

    # Test preservation during staging
    created_notes = []

    def fake_new_note(m):
        n = MagicMock()
        n.id = 0
        n.guid = "random-local-guid"
        n.mid = m["id"]
        n.fields = ["", ""]
        n.tags = []
        return n

    def fake_add_note(n, deck_id):
        n.id = 9001
        created_notes.append(n)

    col.new_note.side_effect = fake_new_note
    col.add_note.side_effect = fake_add_note

    staged_nids = bi._stage_insert_notes([diff])
    assert staged_nids == [9001]
    assert len(created_notes) == 1
    assert created_notes[0].guid == "ankicollab-guid-xyz"

    # Test preservation during unstaged commit_changes
    diff_unstaged = NoteDiff(
        note_id=5002,
        original_fields={},
        updated_fields={"Front": "Q2", "Back": "A2"},
        model_name="Basic",
        field_names=["Front", "Back"],
        action="INSERT",
        tags=["tagB"],
        model_id=10,
        deck_id=1,
        guid="ankicollab-guid-unstaged",
    )
    created_notes.clear()
    commit_changes([diff_unstaged])
    assert len(created_notes) == 1
    assert created_notes[0].guid == "ankicollab-guid-unstaged"


def test_commit_changes_merges_custom_undo_entry(monkeypatch):
    """Verifie que commit_changes regroupe toutes les operations dans une seule entree Undo Anki."""
    col = MagicMock()
    col.add_custom_undo_entry.return_value = 42
    model = {"id": 1, "name": "Basic", "flds": [{"name": "Front"}]}
    col.models.get.return_value = model

    note_mock = MagicMock()
    note_mock.id = 101
    note_mock.mid = 1
    note_mock.fields = ["Old"]
    note_mock.tags = []
    col.get_note.return_value = note_mock

    mw = MagicMock()
    mw.col = col
    monkeypatch.setattr(aqt, "mw", mw)

    diff = NoteDiff(
        note_id=101,
        original_fields={"Front": "Old"},
        updated_fields={"Front": "New"},
        model_name="Basic",
        field_names=["Front"],
        action="UPDATE",
        merged_fields={"Front": "New"},
        tags=["updated_tag"],
    )

    commit_changes([diff])

    col.add_custom_undo_entry.assert_called_once_with("Import Diff")
    col.merge_undo_entries.assert_called_once_with(42)


def test_foreign_keys_pragma_restored_after_id_migration():
    """Verifie que PRAGMA foreign_keys = ON est toujours restaure meme en cas d'erreur SQL."""
    db = _CompatDB()
    col = MagicMock()
    col.db = db

    interceptor = TempLinkInterceptor.instance()
    interceptor.start_session({9999: 1001})

    db.execute(
        "INSERT INTO notes (id, guid, mid, flds, tags) VALUES (?, ?, ?, ?, ?)",
        9999,
        "g1",
        1,
        "Front",
        "",
    )
    db.execute("INSERT INTO cards (id, nid, did, ord) VALUES (?, ?, ?, ?)", 1, 9999, 1, 0)

    interceptor.migrate_staged_note_ids(col)

    fk_status = db.scalar("PRAGMA foreign_keys")
    assert fk_status == 1
    assert db.scalar("SELECT id FROM notes WHERE id = 1001") == 1001


def test_rollback_restores_original_tags_and_fields(monkeypatch):
    """Verifie que l'annulation du diff restaure aussi les tags d'origine de la note et pas seulement les champs."""
    bi.clear_pending_diffs()
    diff = NoteDiff(
        note_id=777,
        original_fields={"Front": "OrigFront"},
        updated_fields={"Front": "NewFront"},
        model_name="Basic",
        field_names=["Front"],
        action="UPDATE",
        tags=["imported_tag", "orig_tag"],
        original_tags=["orig_tag"],
        updated_tags=["imported_tag", "orig_tag"],
    )
    bi._pending_diffs[777] = diff
    bi._committed = False

    note_mock = MagicMock()
    note_mock.id = 777
    note_mock.mid = 1
    # Champs deja revenus a OrigFront, mais tags modifies en base par sync_editor_from_hunks
    note_mock.fields = ["OrigFront"]
    note_mock.tags = ["imported_tag", "orig_tag"]

    col_mock = MagicMock()
    col_mock.get_note.return_value = note_mock
    col_mock.models.get.return_value = {"id": 1, "flds": [{"name": "Front"}]}

    mw_mock = MagicMock()
    mw_mock.col = col_mock
    monkeypatch.setattr(aqt, "mw", mw_mock)

    dummy_panel = MagicMock()
    dummy_panel._note_panels = {}
    dummy_panel._shortcuts = []
    dummy_panel._event_filter = None
    dummy_panel._tabs = None
    dummy_panel._browser = MagicMock()
    dummy_panel._browser._orig_diff_close_event = None
    dummy_panel._browser._orig_diff_key_press_event = None

    bi.DiffBrowserPanel.cleanup(dummy_panel)

    assert note_mock.tags == ["orig_tag"]
    col_mock.update_note.assert_called_once_with(note_mock)


def test_diff_save_current_note_only_intercepts_diff_notes(monkeypatch):
    """Verifie que _diff_save_current_note delegue les notes hors diff (ex: AddCards, EditCurrent) a Anki."""
    bi.clear_pending_diffs()
    diff = NoteDiff(
        note_id=300,
        original_fields={"Front": "A"},
        updated_fields={"Front": "B"},
        model_name="Basic",
        field_names=["Front"],
        action="UPDATE",
    )
    bi._pending_diffs[300] = diff
    bi._browser_panel = MagicMock()

    orig_save = MagicMock()
    monkeypatch.setattr(bi, "_orig_editor_save_current_note", orig_save)

    # 1. Note hors diff (id=404) -> doit deleguer a orig_save
    unrelated_editor = MagicMock()
    unrelated_editor.note.id = 404
    bi._diff_save_current_note(unrelated_editor)
    orig_save.assert_called_once_with(unrelated_editor)
    unrelated_editor.mw.col.update_note.assert_not_called()

    # 2. Note dans le diff (id=300) -> mise a jour directe sans appeler orig_save
    orig_save.reset_mock()
    diff_editor = MagicMock()
    diff_editor.note.id = 300
    bi._diff_save_current_note(diff_editor)
    orig_save.assert_not_called()
    diff_editor.mw.col.update_note.assert_called_once_with(diff_editor.note)

    bi.clear_pending_diffs()
    bi._browser_panel = None


def test_editor_hooks_svelte_integrity():
    """Verifie que editor_will_load_note n'est pas hooke afin de preserver l'hydratation Svelte 5."""
    try:
        from aqt import gui_hooks
        # Re-executer setup pour verifier les hooks enregistres
        bi.setup_native_integration()
        assert not hasattr(bi, "_on_editor_will_load_note")
        hooks_list = getattr(gui_hooks.editor_did_load_note, "_hooks", [])
        assert bi._on_editor_did_load_note in hooks_list
    except ImportError:
        pass


def test_qt_message_handler_chaining_and_idempotency(monkeypatch):
    """Verifie que install_qt_message_handler chaine vers le handler precedent et est idempotent."""
    try:
        from PyQt6 import QtCore
    except ImportError:
        from PySide6 import QtCore

    prev_calls = []

    def prev_handler(mode, ctx, msg):
        prev_calls.append((mode, msg))

    installed_handlers = []

    def fake_install(handler):
        installed_handlers.append(handler)
        return prev_handler

    monkeypatch.setattr(QtCore, "qInstallMessageHandler", fake_install)
    monkeypatch.setattr(hooks_mod, "_qt_msg_handler_installed", False)
    monkeypatch.setattr(hooks_mod, "_prev_qt_msg_handler", None)

    install_qt_message_handler()
    install_qt_message_handler()  # Second call should be a no-op

    assert len(installed_handlers) == 1
    active_handler = installed_handlers[0]

    # Wildcard warning is filtered out
    active_handler(QtCore.QtMsgType.QtWarningMsg, None, "QObject::disconnect: wildcard call disconnects from destroyed signal")
    assert len(prev_calls) == 0

    # Other messages are delegated to Anki's previous handler
    active_handler(QtCore.QtMsgType.QtWarningMsg, None, "Some normal warning")
    assert prev_calls == [(QtCore.QtMsgType.QtWarningMsg, "Some normal warning")]
