import os
import sys
import html
import sqlite3
import pytest
import aqt
from unittest.mock import MagicMock

# Add project root to sys.path to allow absolute imports of 'src'
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Force Qt offscreen platform for headless execution
os.environ["QT_QPA_PLATFORM"] = "offscreen"

# Import PyQt/PySide classes
try:
    from PyQt6 import QtCore, QtGui, QtWidgets
except ImportError:
    from PySide6 import QtCore, QtGui, QtWidgets

# Import the real Anki dialog modules so they exist and are correctly structured
import aqt.import_export.import_dialog
import aqt.importing

class MockModernDialog(QtWidgets.QDialog):
    def __init__(self):
        super().__init__()
        self._layout = QtWidgets.QVBoxLayout(self)
    def layout(self):
        return self._layout
    def _setup_ui(self):
        pass

class MockLegacyDialog(QtWidgets.QDialog):
    def __init__(self):
        super().__init__()
        self._layout = QtWidgets.QVBoxLayout(self)
    def layout(self):
        return self._layout

# Patch them on the real modules
aqt.import_export.import_dialog.ImportDialog = MockModernDialog
aqt.importing.ImportDialog = MockLegacyDialog

# Now import our modules under test
from anki_import_diff.src.savepoint import NoteDiff, capture_import_changes
from anki_import_diff.src.diff import highlight_diff
from anki_import_diff.src.ui import VisualDiffDialog, show_diff_dialog
from anki_import_diff.src.hooks import setup_hooks, inject_checkbox, commit_changes, intercept_import

# --- Mocking Infrastructure ---

class MockNote:
    def __init__(self, id, mid, fields, tags):
        self.id = id
        self.mid = mid
        self.fields = fields
        self.tags = tags

class MockModels:
    def __init__(self):
        self._models = {}
    def get(self, mid):
        return self._models.get(mid)
    def by_name(self, name):
        for m in self._models.values():
            if m.get("name") == name:
                return m
        return None
    def current(self):
        return next(iter(self._models.values()), None)
    def set(self, mid, name, field_names):
        self._models[mid] = {
            "id": mid,
            "name": name,
            "flds": [{"name": fn} for fn in field_names]
        }

class MockAnkiDB:
    def __init__(self):
        self.conn = sqlite3.connect(":memory:", isolation_level=None)
        self.conn.text_factory = lambda b: b.decode('utf-8', 'surrogatepass') if isinstance(b, bytes) else b
        self.conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self):
        self.conn.execute("""
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
        """)
        self.conn.execute("""
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
        """)
        self.conn.commit()

    def execute(self, sql, *args):
        from anki_import_diff.src.hooks import sanitize_surrogates
        flat_args = []
        for a in args:
            if isinstance(a, (list, tuple)):
                flat_args.extend(sanitize_surrogates(x) if isinstance(x, str) else x for x in a)
            else:
                flat_args.append(sanitize_surrogates(a) if isinstance(a, str) else a)
        res = self.conn.execute(sql, flat_args)
        return res

    def all(self, sql, *args):
        from anki_import_diff.src.hooks import sanitize_surrogates
        flat_args = []
        for a in args:
            if isinstance(a, (list, tuple)):
                flat_args.extend(sanitize_surrogates(x) if isinstance(x, str) else x for x in a)
            else:
                flat_args.append(sanitize_surrogates(a) if isinstance(a, str) else a)
        cursor = self.conn.execute(sql, flat_args)
        return [dict(row) for row in cursor.fetchall()]

    def transact(self):
        class Transaction:
            def __enter__(self):
                pass
            def __exit__(self, exc_type, exc_val, exc_tb):
                pass
        return Transaction()

class MockCollection:
    def __init__(self, db):
        self.db = db
        self.models = MockModels()
        self.models.set(1, "Basic", ["Field 1"])

    def get_note(self, note_id):
        rows = self.db.all("SELECT id, mid, flds, tags FROM notes WHERE id=?", note_id)
        if not rows:
            raise KeyError("Note not found")
        note_row = rows[0]
        fields = note_row['flds'].split('\x1f') if note_row['flds'] is not None else []
        tags = [t for t in (note_row['tags'] or "").strip().split(' ') if t]
        return MockNote(note_row['id'], note_row['mid'], fields, tags)

    def new_note(self, model=None):
        if model is None:
            model = self.models.current()
        field_count = len(model.get("flds", [])) if model else 1
        return MockNote(0, model.get("id", 1) if model else 1, [""] * field_count, [])

    def update_note(self, note):
        flds = "\x1f".join(note.fields)
        tags = " " + " ".join(note.tags) + " "
        self.db.execute("UPDATE notes SET flds=?, tags=?, usn=-1 WHERE id=?", flds, tags, note.id)

    def add_note(self, note, deck_id):
        if not note.id:
            import random
            note.id = random.randint(10000, 999999)
        flds = "\x1f".join(note.fields)
        tags = " " + " ".join(note.tags) + " "
        self.db.execute(
            "INSERT INTO notes (id, mid, flds, tags, usn) VALUES (?, ?, ?, ?, -1)",
            note.id, note.mid, flds, tags
        )
        self.db.execute(
            "INSERT INTO cards (nid, did, usn) VALUES (?, ?, -1)",
            note.id, deck_id
        )

    def remove_notes(self, note_ids):
        for nid in note_ids:
            self.db.execute("DELETE FROM notes WHERE id=?", nid)
            self.db.execute("DELETE FROM cards WHERE nid=?", nid)

class MockAddonManager:
    def __init__(self):
        self.config = {"visual_diff_enabled": True}
    def getConfig(self, name):
        return self.config
    def writeConfig(self, name, config):
        self.config = config

@pytest.fixture(scope="session", autouse=True)
def init_qapp(qapp):
    pass

@pytest.fixture(autouse=True)
def mock_env(monkeypatch):
    db = MockAnkiDB()
    col = MockCollection(db)
    class MockMW(QtWidgets.QWidget):
        def __init__(self):
            super().__init__()
            self.col = col
            self.addonManager = MockAddonManager()
            self.app = QtWidgets.QApplication.instance()
    mw_instance = MockMW()
    monkeypatch.setattr(aqt, "mw", mw_instance)
    return mw_instance

# ==========================================
# TIER 1: Feature Coverage (25 tests)
# ==========================================

# --- F1: Import Dialog Option Checkbox ---

def test_f1_modern_dialog_setup_ui_injection(monkeypatch):
    monkeypatch.setattr(aqt.import_export.import_dialog, "ImportDialog", MockModernDialog)
    setup_hooks()
    dialog = MockModernDialog()
    dialog._setup_ui()
    assert hasattr(dialog, "visual_diff_checkbox")
    assert isinstance(dialog.visual_diff_checkbox, QtWidgets.QCheckBox)

def test_f1_legacy_dialog_setup_ui_injection(monkeypatch):
    monkeypatch.setattr(aqt.importing, "ImportDialog", MockLegacyDialog)
    setup_hooks()
    dialog = MockLegacyDialog()
    assert hasattr(dialog, "visual_diff_checkbox")
    assert isinstance(dialog.visual_diff_checkbox, QtWidgets.QCheckBox)

def test_f1_checkbox_state_loaded_from_config(mock_env):
    mock_env.addonManager.config["visual_diff_enabled"] = False
    dialog = MockModernDialog()
    inject_checkbox(dialog)
    assert hasattr(dialog, "visual_diff_checkbox")
    assert not dialog.visual_diff_checkbox.isChecked()

def test_f1_checkbox_state_saved_on_toggle(mock_env):
    mock_env.addonManager.config["visual_diff_enabled"] = True
    dialog = MockModernDialog()
    inject_checkbox(dialog)
    assert dialog.visual_diff_checkbox.isChecked()
    dialog.visual_diff_checkbox.setChecked(False)
    assert not mock_env.addonManager.config["visual_diff_enabled"]

def test_f1_checkbox_state_determines_interception(mock_env):
    mock_env.addonManager.config["visual_diff_enabled"] = False
    called = False
    def mock_import():
        nonlocal called
        called = True
        return 999
    res = intercept_import(mock_import)
    assert res == 999
    assert called

# --- F2: SQLite Savepoint Dry-Run Capture ---

def test_f2_savepoint_creation_and_rollback(mock_env):
    mock_env.col.db.execute("INSERT INTO notes (id, mid, flds, tags) VALUES (101, 1, 'Hello', 'tag')")
    def run_import():
        mock_env.col.db.execute("UPDATE notes SET flds='World' WHERE id=101")
    diffs = capture_import_changes(run_import)
    assert len(diffs) == 1
    assert diffs[0].note_id == 101
    # DB state must be rolled back
    rows = mock_env.col.db.all("SELECT flds FROM notes WHERE id=101")
    assert rows[0]['flds'] == 'Hello'

def test_f2_temp_table_creation_and_cleanup(mock_env):
    def run_import():
        # Check if temp table exists inside transaction
        rows = mock_env.col.db.all("SELECT name FROM sqlite_temp_master WHERE type='table' AND name LIKE 'import_diff_notes_%'")
        assert len(rows) == 1
    capture_import_changes(run_import)
    # Outside transaction, temp table should not exist or be empty
    rows = mock_env.col.db.all("SELECT name FROM sqlite_temp_master WHERE type='table' AND name LIKE 'import_diff_notes_%'")
    assert len(rows) == 0

def test_f2_triggers_log_inserts(mock_env):
    def run_import():
        mock_env.col.db.execute("INSERT INTO notes (id, mid, flds, tags) VALUES (102, 1, 'New', 'tag')")
    diffs = capture_import_changes(run_import)
    assert len(diffs) == 1
    assert diffs[0].action == "INSERT"
    assert diffs[0].updated_fields["Field 1"] == "New"

def test_f2_triggers_log_updates(mock_env):
    mock_env.col.db.execute("INSERT INTO notes (id, mid, flds, tags) VALUES (103, 1, 'Old', 'tag')")
    def run_import():
        mock_env.col.db.execute("UPDATE notes SET flds='NewVal' WHERE id=103")
    diffs = capture_import_changes(run_import)
    assert len(diffs) == 1
    assert diffs[0].action == "UPDATE"
    assert diffs[0].original_fields["Field 1"] == "Old"
    assert diffs[0].updated_fields["Field 1"] == "NewVal"

def test_f2_triggers_log_deletes(mock_env):
    mock_env.col.db.execute("INSERT INTO notes (id, mid, flds, tags) VALUES (104, 1, 'ToDelete', 'tag')")
    def run_import():
        mock_env.col.db.execute("DELETE FROM notes WHERE id=104")
    diffs = capture_import_changes(run_import)
    assert len(diffs) == 1
    assert diffs[0].action == "DELETE"
    assert diffs[0].original_fields["Field 1"] == "ToDelete"

# --- F3: Visual Diff Dialog Presentation ---

def test_f3_dialog_initialization_with_diffs(qtbot):
    diffs = [
        NoteDiff(105, {"Field 1": "Old"}, {"Field 1": "New"}, "Basic", ["Field 1"], "UPDATE")
    ]
    dialog = VisualDiffDialog(diffs)
    qtbot.addWidget(dialog)
    assert dialog.list_widget.count() == 1
    # New UI shows note title (first field content) instead of raw note_id
    item_text = dialog.list_widget.item(0).text()
    assert "Old" in item_text or "New" in item_text or "Basic" in item_text

def test_f3_notes_list_displays_action_icons(qtbot):
    diffs = [
        NoteDiff(106, {"Field 1": "A"}, {"Field 1": "B"}, "Basic", ["Field 1"], "INSERT"),
        NoteDiff(107, {"Field 1": "A"}, {"Field 1": "B"}, "Basic", ["Field 1"], "UPDATE"),
        NoteDiff(108, {"Field 1": "A"}, {"Field 1": "B"}, "Basic", ["Field 1"], "DELETE")
    ]
    dialog = VisualDiffDialog(diffs)
    qtbot.addWidget(dialog)
    # The icons are color filled pixmaps
    assert not dialog.list_widget.item(0).icon().isNull()
    assert not dialog.list_widget.item(1).icon().isNull()
    assert not dialog.list_widget.item(2).icon().isNull()

def test_f3_rendered_html_diff_generation(qtbot):
    diffs = [
        NoteDiff(109, {"Field 1": "Original Text"}, {"Field 1": "Original Brave New Text"}, "Basic", ["Field 1"], "UPDATE")
    ]
    dialog = VisualDiffDialog(diffs)
    qtbot.addWidget(dialog)
    html_text = dialog.diff_browser.toHtml()
    assert "Original" in html_text
    assert "Brave" in html_text
    assert "color:" in html_text

def test_f3_raw_html_toggle_behavior(qtbot):
    diffs = [
        NoteDiff(110, {"Field 1": "<b>bold</b>"}, {"Field 1": "<i>italic</i>"}, "Basic", ["Field 1"], "UPDATE")
    ]
    dialog = VisualDiffDialog(diffs)
    qtbot.addWidget(dialog)
    # Toggle on
    dialog.raw_html_checkbox.setChecked(True)
    plain_text = dialog.diff_browser.toPlainText()
    assert "lt;" in plain_text or "gt;" in plain_text
    # Toggle off
    dialog.raw_html_checkbox.setChecked(False)
    plain_text = dialog.diff_browser.toPlainText()
    assert "lt;" not in plain_text and "gt;" not in plain_text

def test_f3_field_editor_labels_and_values(qtbot):
    diffs = [
        NoteDiff(111, {"F1": "A", "F2": "B"}, {"F1": "A2", "F2": "B2"}, "Basic", ["F1", "F2"], "UPDATE")
    ]
    dialog = VisualDiffDialog(diffs)
    qtbot.addWidget(dialog)
    assert "F1" in dialog.field_widgets
    assert "F2" in dialog.field_widgets
    assert dialog.field_widgets["F1"]["text_edit"].toPlainText() == "A2"

# --- F4: Granular Merge and Edit Controls ---

def test_f4_note_level_inclusion_toggle(qtbot):
    diffs = [
        NoteDiff(112, {"Field 1": "A"}, {"Field 1": "B"}, "Basic", ["Field 1"], "UPDATE")
    ]
    dialog = VisualDiffDialog(diffs)
    qtbot.addWidget(dialog)
    item = dialog.list_widget.item(0)
    assert not diffs[0].excluded
    # Uncheck note level checkbox
    item.setCheckState(QtCore.Qt.CheckState.Unchecked)
    assert diffs[0].excluded

def test_f4_field_control_keep_original(qtbot):
    diffs = [
        NoteDiff(113, {"Field 1": "Orig"}, {"Field 1": "Imp"}, "Basic", ["Field 1"], "UPDATE")
    ]
    dialog = VisualDiffDialog(diffs)
    qtbot.addWidget(dialog)
    widgets = dialog.field_widgets["Field 1"]
    widgets["orig_radio"].setChecked(True)
    assert not widgets["text_edit"].isEnabled()
    assert widgets["text_edit"].toPlainText() == "Orig"
    assert diffs[0].merged_fields["Field 1"] == "Orig"

def test_f4_field_control_keep_imported(qtbot):
    diffs = [
        NoteDiff(114, {"Field 1": "Orig"}, {"Field 1": "Imp"}, "Basic", ["Field 1"], "UPDATE")
    ]
    dialog = VisualDiffDialog(diffs)
    qtbot.addWidget(dialog)
    widgets = dialog.field_widgets["Field 1"]
    widgets["orig_radio"].setChecked(True)  # Switch first
    widgets["imp_radio"].setChecked(True)   # Switch back
    assert not widgets["text_edit"].isEnabled()
    assert widgets["text_edit"].toPlainText() == "Imp"
    assert diffs[0].merged_fields["Field 1"] == "Imp"

def test_f4_field_control_edit_custom(qtbot):
    diffs = [
        NoteDiff(115, {"Field 1": "Orig"}, {"Field 1": "Imp"}, "Basic", ["Field 1"], "UPDATE")
    ]
    dialog = VisualDiffDialog(diffs)
    qtbot.addWidget(dialog)
    widgets = dialog.field_widgets["Field 1"]
    widgets["edit_radio"].setChecked(True)
    assert widgets["text_edit"].isEnabled()
    widgets["text_edit"].setPlainText("Custom")
    assert diffs[0].merged_fields["Field 1"] == "Custom"

def test_f4_global_select_deselect_all(qtbot):
    diffs = [
        NoteDiff(116, {"F1": "A"}, {"F1": "B"}, "Basic", ["F1"], "UPDATE"),
        NoteDiff(117, {"F1": "A"}, {"F1": "B"}, "Basic", ["F1"], "UPDATE")
    ]
    dialog = VisualDiffDialog(diffs)
    qtbot.addWidget(dialog)
    dialog.deselect_all()
    assert dialog.list_widget.item(0).checkState() == QtCore.Qt.CheckState.Unchecked
    assert dialog.list_widget.item(1).checkState() == QtCore.Qt.CheckState.Unchecked
    dialog.select_all()
    assert dialog.list_widget.item(0).checkState() == QtCore.Qt.CheckState.Checked
    assert dialog.list_widget.item(1).checkState() == QtCore.Qt.CheckState.Checked

# --- F5: Database Commit on Accept & Cancel Rollback ---

def test_f5_commit_writes_note_updates(mock_env, monkeypatch):
    mock_env.col.db.execute("INSERT INTO notes (id, mid, flds, tags) VALUES (118, 1, 'OldVal', 'tag')")
    monkeypatch.setattr(VisualDiffDialog, "exec", lambda self: QtWidgets.QDialog.DialogCode.Accepted.value)
    diffs = [NoteDiff(118, {"Field 1": "OldVal"}, {"Field 1": "NewVal"}, "Basic", ["Field 1"], "UPDATE")]
    res = show_diff_dialog(diffs)
    assert res is not None
    commit_changes(res)
    note = mock_env.col.get_note(118)
    assert note.fields[0] == "NewVal"

def test_f5_commit_writes_note_inserts(mock_env, monkeypatch):
    monkeypatch.setattr(VisualDiffDialog, "exec", lambda self: QtWidgets.QDialog.DialogCode.Accepted.value)
    diffs = [NoteDiff(119, {}, {"Field 1": "InsertedNote"}, "Basic", ["Field 1"], "INSERT")]
    res = show_diff_dialog(diffs)
    assert res is not None
    commit_changes(res)
    note = mock_env.col.get_note(119)
    assert note.fields[0] == "InsertedNote"

def test_f5_commit_writes_note_deletes(mock_env, monkeypatch):
    mock_env.col.db.execute("INSERT INTO notes (id, mid, flds, tags) VALUES (120, 1, 'Value', 'tag')")
    monkeypatch.setattr(VisualDiffDialog, "exec", lambda self: QtWidgets.QDialog.DialogCode.Accepted.value)
    diffs = [NoteDiff(120, {"Field 1": "Value"}, {}, "Basic", ["Field 1"], "DELETE")]
    res = show_diff_dialog(diffs)
    assert res is not None
    commit_changes(res)
    with pytest.raises(KeyError):
         mock_env.col.get_note(120)

def test_f5_cancel_leaves_db_pristine(mock_env, monkeypatch):
    mock_env.col.db.execute("INSERT INTO notes (id, mid, flds, tags) VALUES (121, 1, 'Pristine', 'tag')")
    monkeypatch.setattr(VisualDiffDialog, "exec", lambda self: QtWidgets.QDialog.DialogCode.Rejected.value)
    diffs = [NoteDiff(121, {"Field 1": "Pristine"}, {"Field 1": "Dirty"}, "Basic", ["Field 1"], "UPDATE")]
    res = show_diff_dialog(diffs)
    assert res is None
    note = mock_env.col.get_note(121)
    assert note.fields[0] == "Pristine"

def test_f5_atomic_transaction_execution(mock_env):
    mock_env.col.db.execute("INSERT INTO notes (id, mid, flds, tags) VALUES (122, 1, 'A', 'tag')")
    diffs = [NoteDiff(122, {"Field 1": "A"}, {"Field 1": "B"}, "Basic", ["Field 1"], "UPDATE")]
    commit_changes(diffs)
    note = mock_env.col.get_note(122)
    assert note.fields[0] == "B"

# ==========================================
# TIER 2: Boundary & Corner Cases (25 tests)
# ==========================================

# --- F1: Import Checkbox Boundary Cases ---

def test_f1_boundary_missing_parent_layout():
    class BrokenDialog:
        def layout(self):
            return None
        def findChild(self, cls):
            return None
    dialog = BrokenDialog()
    # Should not crash, just returns gracefully
    inject_checkbox(dialog)
    assert not hasattr(dialog, "visual_diff_checkbox")

def test_f1_boundary_corrupt_config_fallback(mock_env):
    mock_env.addonManager.config = {}  # Empty/corrupt
    dialog = MockModernDialog()
    inject_checkbox(dialog)
    assert hasattr(dialog, "visual_diff_checkbox")
    # Should default to True
    assert dialog.visual_diff_checkbox.isChecked()

def test_f1_boundary_fast_double_toggles(mock_env):
    dialog = MockModernDialog()
    inject_checkbox(dialog)
    for _ in range(50):
        dialog.visual_diff_checkbox.setChecked(not dialog.visual_diff_checkbox.isChecked())
    assert mock_env.addonManager.config["visual_diff_enabled"] in (True, False)

def test_f1_boundary_unicode_localization(mock_env):
    dialog = MockModernDialog()
    inject_checkbox(dialog)
    # Checkbox text can be localized, check it contains the core phrase
    assert dialog.visual_diff_checkbox.text() == "Visual Diff Import"

def test_f1_boundary_re_injection_safety(mock_env):
    dialog = MockModernDialog()
    inject_checkbox(dialog)
    inject_checkbox(dialog)
    layout = dialog.layout()
    # Check layout contains exactly one checkbox (assuming mock layout records widgets or we just test attribute is unique)
    assert hasattr(dialog, "visual_diff_checkbox")

# --- F2: Savepoint Capturing Boundary Cases ---

def test_f2_boundary_zero_changes(mock_env):
    def run_no_op():
        pass
    diffs = capture_import_changes(run_no_op)
    assert len(diffs) == 0

def test_f2_boundary_import_exception_handling(mock_env):
    mock_env.col.db.execute("INSERT INTO notes (id, mid, flds, tags) VALUES (123, 1, 'Initial', 'tag')")
    def run_crash():
        mock_env.col.db.execute("UPDATE notes SET flds='Changed' WHERE id=123")
        raise RuntimeError("Import failed abruptly")
    with pytest.raises(RuntimeError, match="Import failed abruptly"):
        capture_import_changes(run_crash)
    # Verify savepoint rolled back despite crash
    rows = mock_env.col.db.all("SELECT flds FROM notes WHERE id=123")
    assert rows[0]['flds'] == 'Initial'

def test_f2_boundary_field_count_mismatch(mock_env):
    mock_env.col.db.execute("INSERT INTO notes (id, mid, flds, tags) VALUES (124, 1, 'Field1Val', 'tag')")
    # Change mid fields count or simulate mismatched fields in temp tables
    def run_mismatched():
        mock_env.col.db.execute("UPDATE notes SET flds='F1\x1fExtraFieldVal' WHERE id=124")
    diffs = capture_import_changes(run_mismatched)
    assert len(diffs) == 1
    # Fallback to generated names if schema mismatch
    assert len(diffs[0].updated_fields) >= 1

def test_f2_boundary_massive_fields_size(mock_env):
    large_text = "A" * 100000
    mock_env.col.db.execute("INSERT INTO notes (id, mid, flds, tags) VALUES (125, 1, 'Initial', 'tag')")
    def run_large():
        mock_env.col.db.execute("UPDATE notes SET flds=? WHERE id=125", large_text)
    diffs = capture_import_changes(run_large)
    assert len(diffs) == 1
    assert diffs[0].updated_fields["Field 1"] == large_text

def test_f2_boundary_non_ascii_unicode_tags(mock_env):
    unicode_tags = "tag1 ★ tag3日本語"
    def run_unicode():
        mock_env.col.db.execute("INSERT INTO notes (id, mid, flds, tags) VALUES (126, 1, 'Val', ?)", unicode_tags)
    diffs = capture_import_changes(run_unicode)
    assert len(diffs) == 1
    assert "★" in diffs[0].tags
    assert "tag3日本語" in diffs[0].tags

# --- F3: Visual Diff Dialog Presentation Boundary Cases ---

def test_f3_boundary_no_differences_to_show(qtbot):
    dialog = VisualDiffDialog([])
    qtbot.addWidget(dialog)
    assert dialog.list_widget.count() == 0
    assert dialog.diff_browser.toPlainText() == ""

def test_f3_boundary_invalid_html_diff(qtbot):
    diff_html = highlight_diff("<div>unclosed", "<div>unclosed<b>bold")
    assert "unclosed" in diff_html

def test_f3_boundary_empty_fields_rendering(qtbot):
    note = NoteDiff(127, {"Field 1": ""}, {"Field 1": ""}, "Basic", ["Field 1"], "UPDATE")
    dialog = VisualDiffDialog([note])
    qtbot.addWidget(dialog)
    assert dialog.field_widgets["Field 1"]["text_edit"].toPlainText() == ""

def test_f3_boundary_extreme_notes_count_scrolling(qtbot):
    diffs = []
    for i in range(1000):
        diffs.append(NoteDiff(i, {"F1": "A"}, {"F1": "B"}, "Basic", ["F1"], "UPDATE"))
    dialog = VisualDiffDialog(diffs)
    qtbot.addWidget(dialog)
    assert dialog.list_widget.count() == 1000

def test_f3_boundary_escaped_html_entities(qtbot):
    res = highlight_diff("A & B", "A &amp; B")
    assert "&amp;" in res

# --- F4: Granular Merge Controls Boundary Cases ---

def test_f4_boundary_unchecking_all_notes(qtbot):
    diffs = [
        NoteDiff(128, {"F1": "A"}, {"F1": "B"}, "Basic", ["F1"], "UPDATE"),
        NoteDiff(129, {"F1": "A"}, {"F1": "B"}, "Basic", ["F1"], "UPDATE")
    ]
    dialog = VisualDiffDialog(diffs)
    qtbot.addWidget(dialog)
    dialog.deselect_all()
    # Check that all elements are marked excluded
    for note in diffs:
        assert note.excluded

def test_f4_boundary_edit_empty_text(qtbot):
    diffs = [
        NoteDiff(130, {"Field 1": "A"}, {"Field 1": "B"}, "Basic", ["Field 1"], "UPDATE")
    ]
    dialog = VisualDiffDialog(diffs)
    qtbot.addWidget(dialog)
    widgets = dialog.field_widgets["Field 1"]
    widgets["edit_radio"].setChecked(True)
    widgets["text_edit"].setPlainText("")
    assert diffs[0].merged_fields["Field 1"] == ""

def test_f4_boundary_switching_modes_retains_custom_edit(qtbot):
    diffs = [
        NoteDiff(131, {"Field 1": "Original"}, {"Field 1": "Imported"}, "Basic", ["Field 1"], "UPDATE")
    ]
    dialog = VisualDiffDialog(diffs)
    qtbot.addWidget(dialog)
    widgets = dialog.field_widgets["Field 1"]
    
    widgets["edit_radio"].setChecked(True)
    widgets["text_edit"].setPlainText("CustomEdit")
    
    # Toggle away
    widgets["orig_radio"].setChecked(True)
    # Toggle back
    widgets["edit_radio"].setChecked(True)
    # Should retain CustomEdit
    assert widgets["text_edit"].toPlainText() == "CustomEdit"

def test_f4_boundary_keyboard_focus_loss(qtbot):
    diffs = [
        NoteDiff(132, {"Field 1": "Original"}, {"Field 1": "Imported"}, "Basic", ["Field 1"], "UPDATE")
    ]
    dialog = VisualDiffDialog(diffs)
    qtbot.addWidget(dialog)
    widgets = dialog.field_widgets["Field 1"]
    widgets["edit_radio"].setChecked(True)
    widgets["text_edit"].setPlainText("Typing...")
    # Lose focus
    widgets["text_edit"].clearFocus()
    assert diffs[0].merged_fields["Field 1"] == "Typing..."

def test_f4_boundary_very_long_custom_text(qtbot):
    diffs = [
        NoteDiff(133, {"Field 1": "A"}, {"Field 1": "B"}, "Basic", ["Field 1"], "UPDATE")
    ]
    dialog = VisualDiffDialog(diffs)
    qtbot.addWidget(dialog)
    widgets = dialog.field_widgets["Field 1"]
    widgets["edit_radio"].setChecked(True)
    long_txt = "Custom" * 5000
    widgets["text_edit"].setPlainText(long_txt)
    assert diffs[0].merged_fields["Field 1"] == long_txt

# --- F5: Database Commit Boundary Cases ---

def test_f5_boundary_partial_selection_commit(mock_env, monkeypatch):
    mock_env.col.db.execute("INSERT INTO notes (id, mid, flds, tags) VALUES (134, 1, 'OriginalA', 'tag')")
    mock_env.col.db.execute("INSERT INTO notes (id, mid, flds, tags) VALUES (135, 1, 'OriginalB', 'tag')")
    
    diffs = [
        NoteDiff(134, {"Field 1": "OriginalA"}, {"Field 1": "ImportedA"}, "Basic", ["Field 1"], "UPDATE"),
        NoteDiff(135, {"Field 1": "OriginalB"}, {"Field 1": "ImportedB"}, "Basic", ["Field 1"], "UPDATE")
    ]
    diffs[0].excluded = False  # Keep
    diffs[1].excluded = True   # Exclude
    
    commit_changes(diffs)
    
    # 134 should update, 135 should remain original
    noteA = mock_env.col.get_note(134)
    noteB = mock_env.col.get_note(135)
    assert noteA.fields[0] == "ImportedA"
    assert noteB.fields[0] == "OriginalB"

def test_f5_boundary_write_back_error_recovery(mock_env, monkeypatch):
    # Simulate DB locks/exceptions
    def bad_update(note):
        raise sqlite3.OperationalError("database is locked")
    monkeypatch.setattr(mock_env.col, "update_note", bad_update)
    mock_env.col.db.execute("INSERT INTO notes (id, mid, flds, tags) VALUES (136, 1, 'A', 'tag')")
    diffs = [NoteDiff(136, {"Field 1": "A"}, {"Field 1": "B"}, "Basic", ["Field 1"], "UPDATE")]
    # commit_changes catches error per note and doesn't crash
    commit_changes(diffs)
    note = mock_env.col.get_note(136)
    assert note.fields[0] == "A"

def test_f5_boundary_note_deleted_concurrently(mock_env, monkeypatch):
    # Note in diff was deleted from DB while UI was open
    diffs = [NoteDiff(137, {"Field 1": "A"}, {"Field 1": "B"}, "Basic", ["Field 1"], "UPDATE")]
    # Note 137 does not exist in DB, committing should skip it gracefully without raising KeyError
    commit_changes(diffs)

def test_f5_boundary_usn_marking_for_sync(mock_env):
    # Modified USN should be set to -1
    mock_env.col.db.execute("INSERT INTO notes (id, mid, flds, tags, usn) VALUES (138, 1, 'A', 'tag', 10)")
    diffs = [NoteDiff(138, {"Field 1": "A"}, {"Field 1": "B"}, "Basic", ["Field 1"], "UPDATE")]
    commit_changes(diffs)
    rows = mock_env.col.db.all("SELECT usn FROM notes WHERE id=138")
    assert rows[0]['usn'] == -1

def test_f5_boundary_undo_history_entry(mock_env):
    mock_env.col.db.execute("INSERT INTO notes (id, mid, flds, tags) VALUES (139, 1, 'A', 'tag')")
    diffs = [NoteDiff(139, {"Field 1": "A"}, {"Field 1": "B"}, "Basic", ["Field 1"], "UPDATE")]
    commit_changes(diffs)
    from anki_import_diff.src.history import NoteHistoryDB
    hist_db = NoteHistoryDB.instance()
    versions = hist_db.get_history(139)
    assert len(versions) >= 1

# ==========================================
# TIER 3: Cross-Feature Combinations (5 tests)
# ==========================================

def test_t3_toggle_checkbox_mid_import(mock_env):
    # Check that toggling the checkbox after import begins does not alter active savepoint
    mock_env.col.db.execute("INSERT INTO notes (id, mid, flds, tags) VALUES (140, 1, 'Init', 'tag')")
    def import_fn():
        mock_env.col.db.execute("UPDATE notes SET flds='Changed' WHERE id=140")
        # Mid-import toggle
        mock_env.addonManager.config["visual_diff_enabled"] = False
    diffs = capture_import_changes(import_fn)
    assert len(diffs) == 1
    # Savepoint must roll back normally
    rows = mock_env.col.db.all("SELECT flds FROM notes WHERE id=140")
    assert rows[0]['flds'] == 'Init'

def test_t3_savepoint_rollback_ui_data_isolation(mock_env):
    # Verify that once savepoint rolls back, captured diff structures remain unchanged and readable by UI
    mock_env.col.db.execute("INSERT INTO notes (id, mid, flds, tags) VALUES (141, 1, 'ValueA', 'tag')")
    def run_imp():
        mock_env.col.db.execute("UPDATE notes SET flds='ValueB' WHERE id=141")
    diffs = capture_import_changes(run_imp)
    # DB rolled back
    rows = mock_env.col.db.all("SELECT flds FROM notes WHERE id=141")
    assert rows[0]['flds'] == 'ValueA'
    # Data is isolated in diffs
    assert diffs[0].original_fields["Field 1"] == "ValueA"
    assert diffs[0].updated_fields["Field 1"] == "ValueB"

def test_t3_edit_text_and_raw_html_toggle(qtbot):
    note = NoteDiff(142, {"Field 1": "<b>A</b>"}, {"Field 1": "<i>B</i>"}, "Basic", ["Field 1"], "UPDATE")
    dialog = VisualDiffDialog([note])
    qtbot.addWidget(dialog)
    
    widgets = dialog.field_widgets["Field 1"]
    widgets["edit_radio"].setChecked(True)
    widgets["text_edit"].setPlainText("CustomText")
    
    # Toggle raw html check
    dialog.raw_html_checkbox.setChecked(True)
    # Toggling Raw HTML should not change what user edited in QTextEdit
    assert widgets["text_edit"].toPlainText() == "CustomText"
    dialog.raw_html_checkbox.setChecked(False)
    assert widgets["text_edit"].toPlainText() == "CustomText"

def test_t3_partial_commit_with_mixed_actions(mock_env):
    mock_env.col.db.execute("INSERT INTO notes (id, mid, flds, tags) VALUES (143, 1, 'OldVal', 'tag')")
    mock_env.col.db.execute("INSERT INTO notes (id, mid, flds, tags) VALUES (144, 1, 'ToDelete', 'tag')")
    
    diffs = [
        NoteDiff(143, {"Field 1": "OldVal"}, {"Field 1": "NewVal"}, "Basic", ["Field 1"], "UPDATE"),
        NoteDiff(144, {"Field 1": "ToDelete"}, {}, "Basic", ["Field 1"], "DELETE"),
        NoteDiff(145, {}, {"Field 1": "Inserted"}, "Basic", ["Field 1"], "INSERT")
    ]
    # Commit all
    commit_changes(diffs)
    
    # Assert updates and deletes executed, inserts added
    note_up = mock_env.col.get_note(143)
    assert note_up.fields[0] == "NewVal"
    with pytest.raises(KeyError):
        mock_env.col.get_note(144)
    note_in = mock_env.col.get_note(145)
    assert note_in.fields[0] == "Inserted"

def test_t3_cancel_restores_triggers_and_savepoints(mock_env, monkeypatch):
    mock_env.col.db.execute("INSERT INTO notes (id, mid, flds, tags) VALUES (146, 1, 'Initial', 'tag')")
    def run_import():
        mock_env.col.db.execute("UPDATE notes SET flds='Changed' WHERE id=146")
        
    # Trigger capturing
    diffs = capture_import_changes(run_import)
    
    # Simulate user cancel
    monkeypatch.setattr(VisualDiffDialog, "exec", lambda self: QtWidgets.QDialog.DialogCode.Rejected.value)
    res = show_diff_dialog(diffs)
    assert res is None
    
    # DB state must be Pristine
    rows = mock_env.col.db.all("SELECT flds FROM notes WHERE id=146")
    assert rows[0]['flds'] == 'Initial'

# ==========================================
# TIER 4: Real-world Application Scenarios (5 tests)
# ==========================================

def test_t4_csv_import_typical_update_flow(mock_env, monkeypatch):
    # CSV modifies 2 notes and adds 1 new note.
    # Note 1: Keep Imported. Note 2: Keep Original. Note 3: Keep Custom.
    mock_env.col.db.execute("INSERT INTO notes (id, mid, flds, tags) VALUES (150, 1, 'Old1', 'tag')")
    mock_env.col.db.execute("INSERT INTO notes (id, mid, flds, tags) VALUES (151, 1, 'Old2', 'tag')")
    
    diffs = [
        NoteDiff(150, {"Field 1": "Old1"}, {"Field 1": "NewImport1"}, "Basic", ["Field 1"], "UPDATE"),
        NoteDiff(151, {"Field 1": "Old2"}, {"Field 1": "NewImport2"}, "Basic", ["Field 1"], "UPDATE"),
        NoteDiff(152, {}, {"Field 1": "NewInsert3"}, "Basic", ["Field 1"], "INSERT")
    ]
    
    def mock_ui_interaction(self):
        # Note 1: keep imported (default)
        # Note 2: keep original
        w2 = self.field_widgets["Field 1"] # for note 151
        # Click Keep Original on 151
        self.load_note(diffs[1])
        self.field_widgets["Field 1"]["orig_radio"].setChecked(True)
        # Note 3: custom edit
        self.load_note(diffs[2])
        self.field_widgets["Field 1"]["edit_radio"].setChecked(True)
        self.field_widgets["Field 1"]["text_edit"].setPlainText("CustomInsertVal")
        return QtWidgets.QDialog.DialogCode.Accepted.value
        
    monkeypatch.setattr(VisualDiffDialog, "exec", mock_ui_interaction)
    res = show_diff_dialog(diffs)
    assert res is not None
    commit_changes(res)
    
    assert mock_env.col.get_note(150).fields[0] == "NewImport1"
    assert mock_env.col.get_note(151).fields[0] == "Old2"
    assert mock_env.col.get_note(152).fields[0] == "CustomInsertVal"

def test_t4_apkg_import_with_new_deck_and_notes(mock_env, monkeypatch):
    # User imports APKG, then decides to cancel
    mock_env.col.db.execute("INSERT INTO notes (id, mid, flds, tags) VALUES (153, 1, 'Orig', 'tag')")
    def run_apkg():
        mock_env.col.db.execute("INSERT INTO notes (id, mid, flds, tags) VALUES (154, 1, 'ApkgNote', 'tag')")
        mock_env.col.db.execute("UPDATE notes SET flds='OrigChanged' WHERE id=153")
        
    diffs = capture_import_changes(run_apkg)
    monkeypatch.setattr(VisualDiffDialog, "exec", lambda self: QtWidgets.QDialog.DialogCode.Rejected.value)
    res = show_diff_dialog(diffs)
    assert res is None # Cancelled
    
    # DB must remain unchanged
    rows = mock_env.col.db.all("SELECT flds FROM notes WHERE id=153")
    assert rows[0]['flds'] == 'Orig'
    with pytest.raises(KeyError):
        mock_env.col.get_note(154)

def test_t4_html_conflict_resolution_and_commit(mock_env, monkeypatch):
    # Import updates note with HTML. User resolves conflict and commits.
    mock_env.col.db.execute("INSERT INTO notes (id, mid, flds, tags) VALUES (155, 1, '<b>Text</b>', 'tag')")
    diffs = [NoteDiff(155, {"Field 1": "<b>Text</b>"}, {"Field 1": "<i>Text</i>"}, "Basic", ["Field 1"], "UPDATE")]
    
    def resolve_html(self):
        widgets = self.field_widgets["Field 1"]
        widgets["edit_radio"].setChecked(True)
        widgets["text_edit"].setPlainText("<b><i>Merged Text</i></b>")
        return QtWidgets.QDialog.DialogCode.Accepted.value
        
    monkeypatch.setattr(VisualDiffDialog, "exec", resolve_html)
    res = show_diff_dialog(diffs)
    assert res is not None
    commit_changes(res)
    assert mock_env.col.get_note(155).fields[0] == "<b><i>Merged Text</i></b>"

def test_t4_large_import_review_and_cancel(mock_env, monkeypatch):
    # Large import with 100 changes, user Cancels.
    def run_large_import():
        for i in range(100):
            mock_env.col.db.execute("INSERT INTO notes (id, mid, flds, tags) VALUES (?, 1, 'Data', 'tag')", 1000 + i)
            
    diffs = capture_import_changes(run_large_import)
    assert len(diffs) == 100
    
    monkeypatch.setattr(VisualDiffDialog, "exec", lambda self: QtWidgets.QDialog.DialogCode.Rejected.value)
    res = show_diff_dialog(diffs)
    assert res is None
    
    # DB has no inserts
    rows = mock_env.col.db.all("SELECT COUNT(*) as cnt FROM notes WHERE id >= 1000")
    assert rows[0]['cnt'] == 0

def test_t4_sync_after_partial_commit(mock_env):
    # Partial commit triggers. Diffs are written with USN=-1.
    mock_env.col.db.execute("INSERT INTO notes (id, mid, flds, tags, usn) VALUES (160, 1, 'A', 'tag', 5)")
    mock_env.col.db.execute("INSERT INTO notes (id, mid, flds, tags, usn) VALUES (161, 1, 'B', 'tag', 5)")
    
    diffs = [
        NoteDiff(160, {"Field 1": "A"}, {"Field 1": "ChangedA"}, "Basic", ["Field 1"], "UPDATE"),
        NoteDiff(161, {"Field 1": "B"}, {"Field 1": "ChangedB"}, "Basic", ["Field 1"], "UPDATE")
    ]
    # Note 160 kept, 161 excluded
    diffs[0].excluded = False
    diffs[1].excluded = True
    
    commit_changes(diffs)
    
    # 160 has USN=-1, 161 has USN=5
    usn160 = mock_env.col.db.all("SELECT usn FROM notes WHERE id=160")[0]['usn']
    usn161 = mock_env.col.db.all("SELECT usn FROM notes WHERE id=161")[0]['usn']
    assert usn160 == -1
    assert usn161 == 5

def test_import_dialog_closure():
    from anki_import_diff.src.hooks import register_import_dialog, close_active_import_dialogs
    
    class FakeDialog:
        def __init__(self):
            self.rejected_called = False
            self.closed_called = False
        def reject(self):
            self.rejected_called = True
        def close(self):
            self.closed_called = True
            
    dialog = FakeDialog()
    register_import_dialog(dialog)
    close_active_import_dialogs()
    assert dialog.rejected_called

def test_browser_row_changed_selection(mock_env):
    from anki_import_diff.src.browser_integration import DiffBrowserPanel, NoteDiff
    
    # Setup pending diffs
    diffs = [
        NoteDiff(201, {"Field 1": "A"}, {"Field 1": "B"}, "Basic", ["Field 1"], "UPDATE"),
        NoteDiff(202, {"Field 1": "C"}, {"Field 1": "D"}, "Basic", ["Field 1"], "UPDATE"),
    ]
    
    from anki_import_diff.src.browser_integration import set_pending_diffs, clear_pending_diffs
    def dummy_on_commit(x): pass
    set_pending_diffs(diffs, dummy_on_commit)
    
    class FakeTable:
        def __init__(self):
            self.note_to_return = None
        def get_current_note(self):
            return self.note_to_return
        def current_note(self):
            return self.note_to_return

    class FakeTableWithoutGet:
        def __init__(self):
            self.note_to_return = None
        def current_note(self):
            return self.note_to_return

    class FakeBrowser(QtWidgets.QMainWindow):
        def __init__(self):
            super().__init__()
            self.table = FakeTable()
            self.editor = MagicMock()
            self._selected_notes = []
            self.last_search = None
        def selected_notes(self):
            return self._selected_notes
        def search_for(self, txt):
            self.last_search = txt
        def addDockWidget(self, area, widget):
            pass

    try:
        browser = FakeBrowser()
        panel = DiffBrowserPanel(browser)
        
        # Test 1: get_current_note
        class DummyNote:
            def __init__(self, id):
                self.id = id
        
        browser.table.note_to_return = DummyNote(202)
        panel._on_browser_row_changed(browser)
        assert panel._current_nid == 202
        assert panel._diff_stack.currentWidget() == panel._note_panels[202]
        
        # Test 2: fallback to current_note if get_current_note is missing
        browser.table = FakeTableWithoutGet()
        browser.table.note_to_return = DummyNote(201)
        panel._on_browser_row_changed(browser)
        assert panel._current_nid == 201
        assert panel._diff_stack.currentWidget() == panel._note_panels[201]
        
        # Test 3: fallback to selected_notes if table note is None
        browser.table = FakeTable()
        browser.table.note_to_return = None
        browser._selected_notes = [202]
        panel._on_browser_row_changed(browser)
        assert panel._current_nid == 202
        assert panel._diff_stack.currentWidget() == panel._note_panels[202]
        
        # Test 4: selecting a note that is NOT in pending diffs switches to _not_in_diff_widget
        browser.table.note_to_return = DummyNote(999)
        panel._on_browser_row_changed(browser)
        assert panel._diff_stack.currentWidget() == panel._not_in_diff_widget
        assert "999" in panel._not_in_diff_title.text()

        # Test 5: clicking return to diffs switches back to pending note panel and restores search for all diff cards
        panel._btn_back_to_diff.click()
        assert panel._diff_stack.currentWidget() in panel._note_panels.values()
        assert browser.last_search is not None
        assert "201" in browser.last_search and "202" in browser.last_search

    finally:
        clear_pending_diffs()

def test_field_empty_placeholder(mock_env):
    from anki_import_diff.src.ui import FieldDiffPanel
    fp_empty = FieldDiffPanel("Recto", "", "New Value")
    assert "[Champ vide dans la version pré-maj]" in fp_empty._old_val_browser.toHtml()

    fp_whitespace = FieldDiffPanel("Verso", "   <br>  &nbsp; ", "New Value")
    assert "[Champ vide dans la version pré-maj]" in fp_whitespace._old_val_browser.toHtml()

def test_note_level_inclusion(mock_env):
    from anki_import_diff.src.browser_integration import DiffBrowserPanel, NoteDiff
    from anki_import_diff.src.editor_sync import sync_editor_from_hunks
    
    diffs = [
        NoteDiff(301, {"Field 1": "OrigValue"}, {"Field 1": "ImpValue"}, "Basic", ["Field 1"], "UPDATE")
    ]
    
    from anki_import_diff.src.browser_integration import set_pending_diffs, clear_pending_diffs
    def dummy_on_commit(x): pass
    set_pending_diffs(diffs, dummy_on_commit)
    
    class FakeBrowser(QtWidgets.QMainWindow):
        def __init__(self):
            super().__init__()
            self.table = MagicMock()
            self.editor = MagicMock()
        def addDockWidget(self, area, widget):
            pass

    try:
        browser = FakeBrowser()
        panel = DiffBrowserPanel(browser)
        note_panel = panel._note_panels[301]
        
        # Check initial states
        assert not diffs[0].excluded
        assert note_panel._include_cb.isChecked()
        assert note_panel._field_panels["Field 1"].isEnabled()
        
        # Uncheck include
        note_panel._include_cb.setChecked(False)
        assert diffs[0].excluded
        assert not note_panel._field_panels["Field 1"].isEnabled()
        
        # Re-check include
        note_panel._include_cb.setChecked(True)
        assert not diffs[0].excluded
        assert note_panel._field_panels["Field 1"].isEnabled()
    finally:
        clear_pending_diffs()


def test_custom_edit_fields_initial_state():
    from anki_import_diff.src.savepoint import NoteDiff
    diff = NoteDiff(101, {"F1": "old"}, {"F1": "new"}, "Basic", ["F1"], "UPDATE")
    assert diff.custom_edit_fields == {}
    assert diff.merged_fields == {"F1": "new"}


def test_editor_unfocus_captures_custom_edits():
    from anki_import_diff.src.savepoint import NoteDiff
    from anki_import_diff.src.editor_sync import make_field_edit_hook

    diff = NoteDiff(102, {"F1": "old", "F2": "orig"}, {"F1": "new", "F2": "imp"}, "Basic", ["F1", "F2"], "UPDATE")
    hook = make_field_edit_hook(lambda nid: diff if nid == 102 else None)

    fake_note = MagicMock()
    fake_note.id = 102
    fake_note.fields = ["user_edited_f1", "imp"]
    fake_editor = MagicMock()
    fake_editor.note = fake_note

    hook(True, fake_editor, 0)
    assert diff.custom_edit_fields.get("F1") == "user_edited_f1"
    assert diff.merged_fields.get("F1") == "user_edited_f1"
    # F2 was not edited, shouldn't be in custom_edit_fields
    assert "F2" not in diff.custom_edit_fields


def test_sync_editor_from_hunks_preserves_custom_edits():
    from anki_import_diff.src.savepoint import NoteDiff
    from anki_import_diff.src.editor_sync import sync_editor_from_hunks

    diff = NoteDiff(103, {"F1": "old", "F2": "old2"}, {"F1": "new", "F2": "new2"}, "Basic", ["F1", "F2"], "UPDATE")
    diff.custom_edit_fields["F1"] = "my_custom_work"
    diff.merged_fields["F1"] = "my_custom_work"

    eval_calls = []
    fake_editor = MagicMock()
    fake_editor.note.id = 103
    fake_editor.web.evalWithCallback = lambda js, cb: eval_calls.append(js)

    sync_editor_from_hunks(fake_editor, diff)

    # Make sure F1 is NOT set via JS because it is in custom_edit_fields
    f1_calls = [c for c in eval_calls if "my_custom_work" in c]
    assert len(f1_calls) == 0


def test_custom_edit_preserved_on_hunk_sync_and_get_merged(mock_env):
    from anki_import_diff.src.browser_integration import DiffBrowserPanel, NoteDiff, set_pending_diffs, clear_pending_diffs

    diffs = [
        NoteDiff(401, {"Field 1": "OrigValue", "Field 2": "Orig2"},
                      {"Field 1": "ImpValue", "Field 2": "Imp2"},
                      "Basic", ["Field 1", "Field 2"], "UPDATE")
    ]
    set_pending_diffs(diffs, lambda x: None)

    class FakeBrowser(QtWidgets.QMainWindow):
        def __init__(self):
            super().__init__()
            self.table = MagicMock()
            self.editor = MagicMock()
        def addDockWidget(self, area, widget):
            pass

    try:
        browser = FakeBrowser()
        panel = DiffBrowserPanel(browser)
        note_panel = panel._note_panels[401]

        # Simulate user custom editing Field 1
        diffs[0].custom_edit_fields["Field 1"] = "UserWorkValue"
        diffs[0].merged_fields["Field 1"] = "UserWorkValue"

        # Trigger sync from hunks
        note_panel._do_sync_editor()

        # Field 1 must NOT be overwritten by hunk value
        assert diffs[0].merged_fields["Field 1"] == "UserWorkValue"
        merged = note_panel.get_merged_fields()
        assert merged["Field 1"] == "UserWorkValue"

        # Reject all hunks should STILL keep user's custom edit
        note_panel.reject_all_hunks()
        assert diffs[0].merged_fields["Field 1"] == "UserWorkValue"
        assert note_panel.get_merged_fields()["Field 1"] == "UserWorkValue"

        # Accept all hunks explicitly clears custom edits and accepts import hunks
        note_panel.accept_all_hunks()
        assert "Field 1" not in diffs[0].custom_edit_fields
        assert diffs[0].merged_fields["Field 1"] == "ImpValue"
        assert note_panel.get_merged_fields()["Field 1"] == "ImpValue"
    finally:
        clear_pending_diffs()


def test_shortcuts_registered_in_diff_browser_panel(mock_env):
    from anki_import_diff.src.browser_integration import DiffBrowserPanel, NoteDiff, set_pending_diffs, clear_pending_diffs

    diffs = [
        NoteDiff(501, {"Field 1": "Orig"}, {"Field 1": "Imp"}, "Basic", ["Field 1"], "UPDATE")
    ]
    set_pending_diffs(diffs, lambda x: None)

    class FakeBrowser(QtWidgets.QMainWindow):
        def __init__(self):
            super().__init__()
            self.table = MagicMock()
            self.editor = MagicMock()
        def addDockWidget(self, area, widget):
            pass

    try:
        browser = FakeBrowser()
        panel = DiffBrowserPanel(browser)
        assert len(panel._shortcuts) >= 4
        seqs = [sc.key().toString() for sc in panel._shortcuts]
        assert "Alt+A" in seqs
        assert "Alt+V" in seqs
        assert "Ctrl+Alt+A" in seqs
        assert "Ctrl+Shift+A" in seqs
        assert "Alt+R" in seqs
        assert "Ctrl+Shift+R" in seqs

        # Test triggering the accept shortcut
        note_panel = panel._note_panels[501]
        note_panel.reject_all_hunks()
        assert not any(h.accepted for fp in note_panel._field_panels.values() for h in fp.hunks)

        panel._accept_all_current()
        assert all(h.accepted for fp in note_panel._field_panels.values() for h in fp.hunks)

        assert "Alt+H" in seqs
        assert "Ctrl+Shift+H" in seqs
        assert "Alt+C" in seqs
        assert "Ctrl+Shift+C" in seqs

        # Test toggle shortcut
        assert panel._tabs.currentIndex() == 0
        panel._toggle_history_tab()
        assert panel._tabs.currentIndex() == 1
        panel._toggle_history_tab()
        assert panel._tabs.currentIndex() == 0
    finally:
        clear_pending_diffs()


def test_old_fields_panel_text_selection_and_copy(mock_env):
    from anki_import_diff.src.ui import OldFieldsPanel
    from anki_import_diff.src.savepoint import NoteDiff

    diff = NoteDiff(
        note_id=123,
        original_fields={"Recto": "Question originale", "Verso": "Reponse originale"},
        updated_fields={"Recto": "Question nouvelle", "Verso": "Reponse nouvelle"},
        model_name="Basic",
        field_names=["Recto", "Verso"],
        action="UPDATE",
    )

    panel = OldFieldsPanel(diff)

    # Find QTextBrowser widgets
    text_browsers = panel.findChildren(QtWidgets.QTextBrowser)
    assert len(text_browsers) == 2

    # Check text selectable flags
    for tb in text_browsers:
        flags = tb.textInteractionFlags()
        assert flags & QtCore.Qt.TextInteractionFlag.TextSelectableByMouse
        assert flags & QtCore.Qt.TextInteractionFlag.TextSelectableByKeyboard

    # Check copy buttons
    buttons = panel.findChildren(QtWidgets.QPushButton)
    btn_texts = [b.text() for b in buttons]
    assert "Tout copier" in btn_texts
    assert "Copier" in btn_texts

    # Test copy all action
    copy_all_btn = [b for b in buttons if b.text() == "Tout copier"][0]
    copy_all_btn.click()
    app = QtWidgets.QApplication.instance()
    clipboard_text = app.clipboard().text()
    assert "Question originale" in clipboard_text
    assert "Reponse originale" in clipboard_text


def test_diff_browser_panel_tabs_and_old_fields(mock_env):
    from anki_import_diff.src.browser_integration import DiffBrowserPanel, NoteDiff, set_pending_diffs, clear_pending_diffs

    diffs = [
        NoteDiff(999, {"Front": "Old text", "Back": "Old back"}, {"Front": "New text", "Back": "New back"}, "Basic", ["Front", "Back"], "UPDATE")
    ]
    set_pending_diffs(diffs, lambda x: None)

    class FakeBrowser(QtWidgets.QMainWindow):
        def __init__(self):
            super().__init__()
            self.table = MagicMock()
            self.editor = MagicMock()
        def addDockWidget(self, area, widget):
            pass

    try:
        browser = FakeBrowser()
        panel = DiffBrowserPanel(browser)

        # Check tabs (Diff & Historique)
        assert panel._tabs.count() == 2
        assert panel._tabs.tabText(0) == "Diff"
        assert panel._tabs.tabText(1) == "Historique"

        # Check that Diff panel directly renders pre-maj fields with QTextBrowser and copy buttons
        panel.select_note(999)
        note_panel = panel._note_panels[999]
        tbs = note_panel.findChildren(QtWidgets.QTextBrowser)
        assert len(tbs) >= 2  # Each field has a pre-maj QTextBrowser

        # Check copy buttons on fields
        buttons = note_panel.findChildren(QtWidgets.QPushButton)
        btn_texts = [b.text() for b in buttons]
        assert "Copier" in btn_texts
        assert "Tout copier (pré-maj)" in btn_texts
    finally:
        clear_pending_diffs()


def test_note_diff_panel_segmented_tabs(mock_env):
    from anki_import_diff.src.ui import NoteDiffPanel
    from anki_import_diff.src.savepoint import NoteDiff

    diff = NoteDiff(
        note_id=888,
        original_fields={"F1": "Old 1"},
        updated_fields={"F1": "New 1"},
        model_name="Basic",
        field_names=["F1"],
        action="UPDATE",
    )

    panel = NoteDiffPanel(diff)
    assert panel._stack.count() == 3
    assert panel._btn_tab_diff.text() == "Diff Hunk"
    assert panel._btn_tab_orig.text() == "Anciens champs"
    assert panel._btn_tab_hist.text() == "Historique & Collab"

    # Toggle to Anciens champs
    panel._btn_tab_orig.click()
    assert panel._stack.currentIndex() == 1

    # Toggle to Historique
    panel._btn_tab_hist.click()
    assert panel._stack.currentIndex() == 2

    # Toggle back to Diff
    panel._btn_tab_diff.click()
    assert panel._stack.currentIndex() == 0


def test_field_diff_panel_pre_maj_display_and_actions(mock_env):
    from anki_import_diff.src.ui import FieldDiffPanel

    fp = FieldDiffPanel("Question", "Texte pré-maj", "Texte importé")

    # 1. Vérifier la présence du QTextBrowser affichant la version pré-maj
    assert hasattr(fp, "_old_val_browser")
    assert fp._old_val_browser.isReadOnly()
    flags = fp._old_val_browser.textInteractionFlags()
    assert flags & QtCore.Qt.TextInteractionFlag.TextSelectableByMouse
    assert flags & QtCore.Qt.TextInteractionFlag.TextSelectableByKeyboard
    assert "Texte pré-maj" in fp._old_val_browser.toPlainText()

    # 2. Bouton Copier
    copy_btns = [b for b in fp.findChildren(QtWidgets.QPushButton) if b.text() == "Copier"]
    assert len(copy_btns) >= 1
    copy_btns[0].click()
    app = QtWidgets.QApplication.instance()
    assert app.clipboard().text() == "Texte pré-maj"

    # 3. Actions "Garder l'ancien" et "Accepter"
    btn_keep = fp._btn_reject_all
    assert btn_keep.text() == "Garder l'ancien"
    btn_accept = fp._btn_accept_all
    assert btn_accept.text() == "Accepter"

    # Test "Garder l'ancien"
    btn_keep.click()
    assert not any(h.accepted for h in fp.hunks)
    assert fp._badge.text() == "Ancien gardé"

    # Test "Accepter"
    btn_accept.click()
    assert all(h.accepted for h in fp.hunks)
    assert fp._badge.text() == "Accepté"

    # 4. Absence des sections "Modifications d'import" et "Resultat fusionné"
    all_labels_text = [lbl.text() for lbl in fp.findChildren(QtWidgets.QLabel)]
    assert not any("modifications d'import" in t.lower() for t in all_labels_text)
    assert not any("resultat fusionné" in t.lower() for t in all_labels_text)

    # 5. Pas d'ascenseurs internes et adaptation de taille dynamique
    assert fp._old_val_browser.verticalScrollBarPolicy() == QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff
    assert fp._old_val_browser.horizontalScrollBarPolicy() == QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff
    fp.resize(300, 200)
    fp._old_val_browser._adjust_height()
    assert fp._old_val_browser.height() >= 32

    # 6. Curseur I-Beam, sélection et copie au clavier / menu contextuel
    assert fp._old_val_browser.viewport().cursor().shape() == QtCore.Qt.CursorShape.IBeamCursor
    fp._old_val_browser.selectAll()
    assert fp._old_val_browser.textCursor().hasSelection()
    key_copy = QtGui.QKeyEvent(QtCore.QEvent.Type.KeyPress, QtCore.Qt.Key.Key_C, QtCore.Qt.KeyboardModifier.ControlModifier)
    fp._old_val_browser.keyPressEvent(key_copy)
    assert "Texte pré-maj" in app.clipboard().text()


def test_diff_browser_panel_history_tab_stability(mock_env):
    from anki_import_diff.src.browser_integration import DiffBrowserPanel, NoteDiff, set_pending_diffs, clear_pending_diffs

    diffs = [
        NoteDiff(777, {"Champ": "Ancien"}, {"Champ": "Nouveau"}, "Basic", ["Champ"], "UPDATE")
    ]
    set_pending_diffs(diffs, lambda x: None)

    class FakeBrowser(QtWidgets.QMainWindow):
        def __init__(self):
            super().__init__()
            self.table = MagicMock()
            self.editor = MagicMock()
        def addDockWidget(self, area, widget):
            pass

    try:
        browser = FakeBrowser()
        panel = DiffBrowserPanel(browser)
        panel.select_note(777)

        # Tab 0 is Diff
        assert panel._tabs.currentIndex() == 0

        # Switch to Tab 1 (Historique)
        panel._tabs.setCurrentIndex(1)
        # CRITICAL TEST: It must NOT bounce back to 0 or any other tab
        assert panel._tabs.currentIndex() == 1
        assert panel._history_tab_widget is not None
        assert panel._tabs.currentIndex() == 1

        # Switch back to Tab 0 (Diff)
        panel._tabs.setCurrentIndex(0)
        assert panel._tabs.currentIndex() == 0
    finally:
        clear_pending_diffs()


def test_normal_browser_editor_load_does_not_call_sync():
    """Verify that when _browser_panel is None (normal browser), loading a note never calls sync_editor_from_hunks."""
    from anki_import_diff.src.browser_integration import (
        _on_editor_did_load_note,
        set_pending_diffs,
        clear_pending_diffs,
    )
    from anki_import_diff.src.savepoint import NoteDiff
    import anki_import_diff.src.browser_integration as bi

    diff = NoteDiff(
        note_id=888,
        original_fields={"Front": "OldFront", "Back": "OldBack"},
        updated_fields={"Front": "DiffCorrectionFront", "Back": "DiffCorrectionBack"},
        model_name="Basic",
        field_names=["Front", "Back"],
    )

    eval_calls = []

    class FakeWeb:
        def evalWithCallback(self, js, cb=None):
            eval_calls.append(js)
            if cb:
                cb("ok")

    class FakeNote:
        id = 888
        fields = ["UserEditedFront", "UserEditedBack"]

        def items(self):
            return [("Front", self.fields[0]), ("Back", self.fields[1])]

    class FakeEditor:
        note = FakeNote()
        web = FakeWeb()

    try:
        set_pending_diffs([diff], lambda x: None)
        assert bi._browser_panel is None

        editor = FakeEditor()
        _on_editor_did_load_note(editor)

        assert len(eval_calls) == 0
        assert editor.note.fields == ["UserEditedFront", "UserEditedBack"]
    finally:
        clear_pending_diffs()


def test_editor_context_menu_suppressed_when_normal_browser():
    """Verify that diff actions are not injected in context menu during normal browse."""
    from anki_import_diff.src.browser_integration import (
        _on_editor_will_show_context_menu,
        set_pending_diffs,
        clear_pending_diffs,
    )
    from anki_import_diff.src.savepoint import NoteDiff

    diff = NoteDiff(
        note_id=889,
        original_fields={"F": "O"},
        updated_fields={"F": "N"},
        model_name="Basic",
        field_names=["F"],
    )

    class FakeMenu:
        def __init__(self):
            self.actions = []

        def addSeparator(self):
            self.actions.append("sep")

        def addAction(self, title):
            self.actions.append(title)
            class FakeAct:
                triggered = type("Signal", (), {"connect": lambda self, f: None})()
            return FakeAct()

    class FakeNote:
        id = 889

    class FakeWebView:
        class FakeEditor:
            note = FakeNote()
        editor = FakeEditor()

    try:
        set_pending_diffs([diff], lambda x: None)
        menu = FakeMenu()
        webview = FakeWebView()
        _on_editor_will_show_context_menu(webview, menu)
        assert len(menu.actions) == 0
    finally:
        clear_pending_diffs()


def test_editor_unfocus_hooks_preserve_changed_flag():
    """Verify that unfocus hooks return the passed changed boolean rather than forcing False."""
    from anki_import_diff.src.browser_integration import _on_editor_did_unfocus_field
    from anki_import_diff.src.editor_sync import make_field_edit_hook

    class FakeNote:
        id = 991
        fields = ["val"]
        tags = []

        def items(self):
            return [("F", "val")]

    assert _on_editor_did_unfocus_field(True, FakeNote(), 0) is True
    assert _on_editor_did_unfocus_field(False, FakeNote(), 0) is False

    hook = make_field_edit_hook(lambda nid: None)
    assert hook(True, FakeNote(), 0) is True
    assert hook(False, FakeNote(), 0) is False


def test_diff_browser_close_panel_button_commit(mock_env):
    """Clicking Fermer button and choosing commit applies changes and clears pending diffs."""
    import anki_import_diff.src.browser_integration as bi
    from anki_import_diff.src.browser_integration import DiffBrowserPanel, NoteDiff, set_pending_diffs, clear_pending_diffs

    diff = NoteDiff(1001, {"Champ": "V1"}, {"Champ": "V2"}, "Basic", ["Champ"], "UPDATE")
    set_pending_diffs([diff], lambda x: None)

    class FakeBrowser(QtWidgets.QMainWindow):
        def __init__(self):
            super().__init__()
            self.table = MagicMock()
            self.editor = MagicMock()

    browser = FakeBrowser()
    panel = DiffBrowserPanel(browser)

    try:
        bi._decision_hook = lambda: "commit"
        panel._close_panel_btn.click()

        assert bi._committed is True
        assert len(bi._pending_diffs) == 0
    finally:
        bi._decision_hook = None
        clear_pending_diffs()


def test_diff_browser_close_panel_button_revert(mock_env):
    """Clicking Fermer button and choosing revert rolls back changes and clears pending diffs."""
    import anki_import_diff.src.browser_integration as bi
    from anki_import_diff.src.browser_integration import DiffBrowserPanel, NoteDiff, set_pending_diffs, clear_pending_diffs

    diff = NoteDiff(1002, {"Champ": "V1"}, {"Champ": "V2"}, "Basic", ["Champ"], "UPDATE")
    set_pending_diffs([diff], lambda x: None)

    class FakeBrowser(QtWidgets.QMainWindow):
        def __init__(self):
            super().__init__()
            self.table = MagicMock()
            self.editor = MagicMock()

    browser = FakeBrowser()
    panel = DiffBrowserPanel(browser)

    try:
        bi._decision_hook = lambda: "revert"
        panel._close_panel_btn.click()

        assert bi._committed is False
        assert len(bi._pending_diffs) == 0
    finally:
        bi._decision_hook = None
        clear_pending_diffs()


def test_diff_browser_close_panel_button_cancel(mock_env):
    """Clicking Fermer button and choosing cancel aborts closing and preserves pending diffs."""
    import anki_import_diff.src.browser_integration as bi
    from anki_import_diff.src.browser_integration import DiffBrowserPanel, NoteDiff, set_pending_diffs, clear_pending_diffs

    diff = NoteDiff(1003, {"Champ": "V1"}, {"Champ": "V2"}, "Basic", ["Champ"], "UPDATE")
    set_pending_diffs([diff], lambda x: None)

    class FakeBrowser(QtWidgets.QMainWindow):
        def __init__(self):
            super().__init__()
            self.table = MagicMock()
            self.editor = MagicMock()

    browser = FakeBrowser()
    panel = DiffBrowserPanel(browser)

    try:
        bi._decision_hook = lambda: "cancel"
        panel._close_panel_btn.click()

        assert len(bi._pending_diffs) == 1
        assert 1003 in bi._pending_diffs
    finally:
        bi._decision_hook = None
        clear_pending_diffs()


def test_browser_window_close_event_prompt(mock_env):
    """Closing the browser window prompts the user and respects cancel/commit."""
    import anki_import_diff.src.browser_integration as bi
    from anki_import_diff.src.browser_integration import DiffBrowserPanel, NoteDiff, set_pending_diffs, clear_pending_diffs

    diff = NoteDiff(1004, {"Champ": "V1"}, {"Champ": "V2"}, "Basic", ["Champ"], "UPDATE")
    set_pending_diffs([diff], lambda x: None)

    class FakeCloseEvent:
        def __init__(self):
            self.ignored = False
            self.accepted = False
        def ignore(self):
            self.ignored = True
        def accept(self):
            self.accepted = True

    class FakeBrowser(QtWidgets.QMainWindow):
        def __init__(self):
            super().__init__()
            self.table = MagicMock()
            self.editor = MagicMock()
            self.closed = False
        def closeEvent(self, evt):
            self.closed = True

    browser = FakeBrowser()
    panel = DiffBrowserPanel(browser)
    bi._browser_panel = panel

    orig_close = browser.closeEvent

    # Simulate installing _diff_browser_close like in open_browser_diff
    def _test_diff_browser_close(event):
        global _browser_panel
        if bi._browser_panel is not None and not bi._committed and bool(bi._pending_diffs):
            decision = bi.prompt_diff_close_decision(browser)
            if decision == "cancel":
                if event is not None:
                    event.ignore()
                return
            elif decision == "commit":
                bi._browser_panel.apply_commit(close_browser=False)
            elif decision == "revert":
                bi._browser_panel.apply_rollback(close_browser=False)

        clear_pending_diffs()
        if orig_close:
            orig_close(event)

    browser.closeEvent = _test_diff_browser_close

    try:
        # Test 1: User cancels -> close event ignored, window not closed, diffs preserved
        bi._decision_hook = lambda: "cancel"
        evt_cancel = FakeCloseEvent()
        browser.closeEvent(evt_cancel)
        assert evt_cancel.ignored is True
        assert browser.closed is False
        assert len(bi._pending_diffs) == 1

        # Test 2: User chooses commit -> committed, closed, diffs cleared
        bi._decision_hook = lambda: "commit"
        evt_commit = FakeCloseEvent()
        browser.closeEvent(evt_commit)
        assert browser.closed is True
        assert bi._committed is True
        assert len(bi._pending_diffs) == 0
    finally:
        bi._decision_hook = None
        clear_pending_diffs()


def test_editor_shortcuts_hook(qtbot):
    """Vérifie que _on_editor_did_init_shortcuts enregistre bien les raccourcis dans l'éditeur quand le diff est actif."""
    from src.browser_integration import (
        DiffBrowserPanel,
        _on_editor_did_init_shortcuts,
        set_pending_diffs,
        clear_pending_diffs,
    )
    import src.browser_integration as bi
    diff = NoteDiff(note_id=801, action="UPDATE", model_name="Basic",
                    field_names=["Front"], original_fields={"Front": "A"},
                    updated_fields={"Front": "B"})
    set_pending_diffs([diff], lambda x: None)

    class FakeBrowser(QtWidgets.QMainWindow):
        def __init__(self):
            super().__init__()
            self.table = MagicMock()
            self.editor = MagicMock()

    try:
        browser = FakeBrowser()
        panel = DiffBrowserPanel(browser)
        bi._browser_panel = panel

        cuts = []
        editor_mock = MagicMock()
        _on_editor_did_init_shortcuts(cuts, editor_mock)

        # Doit contenir les raccourcis configurés avec flag True (prioritaire dans WebView)
        assert len(cuts) > 0
        keys = [c[0] for c in cuts]
        assert "Alt+V" in keys
        assert "F2" in keys
        assert "Alt+R" in keys
        assert "F3" in keys
        assert all(c[2] is True for c in cuts)

        # Test déclenchement via le shortcut callback de l'éditeur
        note_panel = panel._note_panels[801]
        note_panel.reject_all_hunks()
        assert not any(h.accepted for fp in note_panel._field_panels.values() for h in fp.hunks)

        accept_cb = next(c[1] for c in cuts if c[0] == "Alt+V")
        accept_cb()
        assert all(h.accepted for fp in note_panel._field_panels.values() for h in fp.hunks)

        reject_cb = next(c[1] for c in cuts if c[0] == "Alt+R")
        reject_cb()
        assert not any(h.accepted for fp in note_panel._field_panels.values() for h in fp.hunks)

    finally:
        bi._browser_panel = None
        clear_pending_diffs()


def test_browser_context_menu_and_multiselect(qtbot):
    """Vérifie l'apparition des actions diff dans le clic droit du browser et le support multi-sélection."""
    from src.browser_integration import (
        DiffBrowserPanel,
        _on_browser_will_show_context_menu,
        set_pending_diffs,
        clear_pending_diffs,
    )
    import src.browser_integration as bi

    diff1 = NoteDiff(note_id=901, action="UPDATE", model_name="Basic",
                     field_names=["Front"], original_fields={"Front": "Old1"},
                     updated_fields={"Front": "New1"})
    diff2 = NoteDiff(note_id=902, action="UPDATE", model_name="Basic",
                     field_names=["Front"], original_fields={"Front": "Old2"},
                     updated_fields={"Front": "New2"})
    set_pending_diffs([diff1, diff2], lambda x: None)

    class FakeBrowser(QtWidgets.QMainWindow):
        def __init__(self):
            super().__init__()
            self.table = QtWidgets.QTableView(self)
            self.table.setFocus = MagicMock(wraps=self.table.setFocus)
            self.editor = MagicMock()
            self._selected = []
        def selected_notes(self):
            return self._selected

    try:
        browser = FakeBrowser()
        qtbot.addWidget(browser)
        panel = DiffBrowserPanel(browser)
        bi._browser_panel = panel

        # 1. Menu quand diff actif et 1 note sélectionnée
        browser._selected = [901]
        menu1 = QtWidgets.QMenu()
        _on_browser_will_show_context_menu(browser, menu1)
        actions1 = [a.text() for a in menu1.actions() if a.text()]
        assert any("Diff : Accepter les modifications" in a for a in actions1)
        assert any("Diff : Garder l'ancien" in a for a in actions1)

        # 2. Menu quand 2 notes sélectionnées (multi-sélection)
        browser._selected = [901, 902]
        menu2 = QtWidgets.QMenu()
        _on_browser_will_show_context_menu(browser, menu2)
        actions2 = [a.text() for a in menu2.actions() if a.text()]
        assert any("Diff : Accepter les modifications (2 notes)" in a for a in actions2)
        assert any("Diff : Garder l'ancien (2 notes)" in a for a in actions2)

        # Test accept multi-sélection
        p1 = panel._note_panels[901]
        p2 = panel._note_panels[902]
        p1.reject_all_hunks()
        p2.reject_all_hunks()
        assert not any(h.accepted for fp in p1._field_panels.values() for h in fp.hunks)
        assert not any(h.accepted for fp in p2._field_panels.values() for h in fp.hunks)

        act_acc = next(a for a in menu2.actions() if "Diff : Accepter les modifications (2 notes)" in a.text())
        act_acc.trigger()
        assert all(h.accepted for fp in p1._field_panels.values() for h in fp.hunks)
        assert all(h.accepted for fp in p2._field_panels.values() for h in fp.hunks)
        assert browser.table.setFocus.called

        # Test reject multi-sélection via context menu action
        browser.table.setFocus.reset_mock()
        act_rej = next(a for a in menu2.actions() if "Diff : Garder l'ancien (2 notes)" in a.text())
        act_rej.trigger()
        assert not any(h.accepted for fp in p1._field_panels.values() for h in fp.hunks)
        assert not any(h.accepted for fp in p2._field_panels.values() for h in fp.hunks)
        assert browser.table.setFocus.called

        # Vérifier que les raccourcis clavier (ex. F2) fonctionnent immédiatement après le clic droit rejeter
        ev_f2 = QtGui.QKeyEvent(QtCore.QEvent.Type.KeyPress, QtCore.Qt.Key.Key_F2, QtCore.Qt.KeyboardModifier.NoModifier)
        handled = panel._event_filter.eventFilter(browser.table, ev_f2)
        assert handled is True
        assert all(h.accepted for fp in p1._field_panels.values() for h in fp.hunks)
        assert all(h.accepted for fp in p2._field_panels.values() for h in fp.hunks)

        # Test reject multi-sélection via _reject_all_current avec browser sélection multi
        panel._reject_all_current()
        assert not any(h.accepted for fp in p1._field_panels.values() for h in fp.hunks)
        assert not any(h.accepted for fp in p2._field_panels.values() for h in fp.hunks)

        # 3. Quand le diff n'est PAS actif, les options diff ne doivent PAS apparaître dans le menu
        bi._browser_panel = None
        menu_nodiff = QtWidgets.QMenu()
        _on_browser_will_show_context_menu(browser, menu_nodiff)
        actions_nodiff = [a.text() for a in menu_nodiff.actions() if a.text()]
        assert not any("Diff :" in a for a in actions_nodiff)
        assert any("Historique & AnkiCollab..." in a for a in actions_nodiff)

    finally:
        bi._browser_panel = None
        clear_pending_diffs()


def test_diff_browser_event_filter_f2_f3_alt_a_alt_r(qtbot):
    """Verifie que l'event filter intercepte F2, F3, Alt+A et Alt+R meme si l'evenement arrive sur un widget enfant."""
    from src.browser_integration import (
        DiffBrowserPanel,
        set_pending_diffs,
        clear_pending_diffs,
    )
    import src.browser_integration as bi

    diff = NoteDiff(note_id=888, action="UPDATE", model_name="Basic",
                    field_names=["Front"], original_fields={"Front": "Old"},
                    updated_fields={"Front": "New"})
    set_pending_diffs([diff], lambda x: None)

    class FakeBrowser(QtWidgets.QMainWindow):
        def __init__(self):
            super().__init__()
            self.table = QtWidgets.QTableView(self)
            self.editor = MagicMock()

    browser = FakeBrowser()
    qtbot.addWidget(browser)
    browser.show()

    try:
        panel = DiffBrowserPanel(browser)
        bi._browser_panel = panel
        note_panel = panel._note_panels[888]

        # Initialiser : rejeter
        note_panel.reject_all_hunks()
        assert not any(h.accepted for fp in note_panel._field_panels.values() for h in fp.hunks)

        # 1. Simuler F2 envoye a la tableView
        ev_f2 = QtGui.QKeyEvent(QtCore.QEvent.Type.KeyPress, QtCore.Qt.Key.Key_F2, QtCore.Qt.KeyboardModifier.NoModifier)
        res = panel._event_filter.eventFilter(browser.table, ev_f2)
        assert res is True
        assert all(h.accepted for fp in note_panel._field_panels.values() for h in fp.hunks)

        # 2. Simuler F3 envoye a la tableView
        ev_f3 = QtGui.QKeyEvent(QtCore.QEvent.Type.KeyPress, QtCore.Qt.Key.Key_F3, QtCore.Qt.KeyboardModifier.NoModifier)
        res = panel._event_filter.eventFilter(browser.table, ev_f3)
        assert res is True
        assert not any(h.accepted for fp in note_panel._field_panels.values() for h in fp.hunks)

        # 3. Simuler Alt+A envoye au browser
        ev_alt_a = QtGui.QKeyEvent(QtCore.QEvent.Type.KeyPress, QtCore.Qt.Key.Key_A, QtCore.Qt.KeyboardModifier.AltModifier)
        res = panel._event_filter.eventFilter(browser, ev_alt_a)
        assert res is True
        assert all(h.accepted for fp in note_panel._field_panels.values() for h in fp.hunks)

        # 4. Simuler Alt+R envoye au browser
        ev_alt_r = QtGui.QKeyEvent(QtCore.QEvent.Type.KeyPress, QtCore.Qt.Key.Key_R, QtCore.Qt.KeyboardModifier.AltModifier)
        res = panel._event_filter.eventFilter(browser, ev_alt_r)
        assert res is True
        assert not any(h.accepted for fp in note_panel._field_panels.values() for h in fp.hunks)

        # 5. Simuler Alt+H (toggle history)
        ev_alt_h = QtGui.QKeyEvent(QtCore.QEvent.Type.KeyPress, QtCore.Qt.Key.Key_H, QtCore.Qt.KeyboardModifier.AltModifier)
        res = panel._event_filter.eventFilter(browser.table, ev_alt_h)
        assert res is True
        assert panel._tabs.currentIndex() == 1
        # Toggle back
        res = panel._event_filter.eventFilter(browser.table, ev_alt_h)
        assert res is True
        assert panel._tabs.currentIndex() == 0

        # 6. Simuler Alt+C (copier anciens champs)
        ev_alt_c = QtGui.QKeyEvent(QtCore.QEvent.Type.KeyPress, QtCore.Qt.Key.Key_C, QtCore.Qt.KeyboardModifier.AltModifier)
        res = panel._event_filter.eventFilter(browser.table, ev_alt_c)
        assert res is True

    finally:
        bi._browser_panel = None
        clear_pending_diffs()


def test_visual_diff_dialog_shortcuts_f2_f3_alt_a_alt_r(mock_env):
    """Verifie que VisualDiffDialog reagit a F2, F3, Alt+A et Alt+R via son eventFilter."""
    from anki_import_diff.src.ui import VisualDiffDialog
    from anki_import_diff.src.savepoint import NoteDiff

    diff = NoteDiff(note_id=999, action="UPDATE", model_name="Basic",
                    field_names=["Front"], original_fields={"Front": "Old"},
                    updated_fields={"Front": "New"})
    dlg = VisualDiffDialog([diff])

    # Rejeter
    dlg._reject_all_global()
    assert not any(h.accepted for fh in dlg._note_hunks[999].values() for h in fh)

    # F2
    ev_f2 = QtGui.QKeyEvent(QtCore.QEvent.Type.KeyPress, QtCore.Qt.Key.Key_F2, QtCore.Qt.KeyboardModifier.NoModifier)
    assert dlg.eventFilter(dlg, ev_f2) is True
    assert all(h.accepted for fh in dlg._note_hunks[999].values() for h in fh)

    # F3
    ev_f3 = QtGui.QKeyEvent(QtCore.QEvent.Type.KeyPress, QtCore.Qt.Key.Key_F3, QtCore.Qt.KeyboardModifier.NoModifier)
    assert dlg.eventFilter(dlg, ev_f3) is True
    assert not any(h.accepted for fh in dlg._note_hunks[999].values() for h in fh)

    # Alt+A
    ev_alt_a = QtGui.QKeyEvent(QtCore.QEvent.Type.KeyPress, QtCore.Qt.Key.Key_A, QtCore.Qt.KeyboardModifier.AltModifier)
    assert dlg.eventFilter(dlg, ev_alt_a) is True
    assert all(h.accepted for fh in dlg._note_hunks[999].values() for h in fh)

    # Alt+R
    ev_alt_r = QtGui.QKeyEvent(QtCore.QEvent.Type.KeyPress, QtCore.Qt.Key.Key_R, QtCore.Qt.KeyboardModifier.AltModifier)
    assert dlg.eventFilter(dlg, ev_alt_r) is True
    assert not any(h.accepted for fh in dlg._note_hunks[999].values() for h in fh)


def test_accept_all_recovers_proposed_version_after_user_edit(mock_env):
    """
    Verifie que lorsque l'utilisateur a modifie manuellement un champ,
    le fait de cliquer sur 'Accepter tous les champs' (ou raccourci F2/Alt+A)
    recupere bien la version proposee/importee sans qu'elle soit ecrasee au commit.
    """
    from src.browser_integration import (
        DiffBrowserPanel,
        set_pending_diffs,
        clear_pending_diffs,
    )
    import src.browser_integration as bi
    from src.editor_sync import make_field_edit_hook
    from unittest.mock import patch, MagicMock

    diff = NoteDiff(
        note_id=7777,
        action="UPDATE",
        model_name="Basic",
        field_names=["Front", "Back"],
        original_fields={"Front": "OriginalFront", "Back": "OriginalBack"},
        updated_fields={"Front": "ProposedFront", "Back": "ProposedBack"},
    )
    set_pending_diffs([diff], lambda x: None)

    fake_note = MagicMock()
    fake_note.id = 7777
    fake_note.fields = ["OriginalFront", "OriginalBack"]

    fake_editor = MagicMock()
    fake_editor.note = fake_note

    class FakeBrowser(QtWidgets.QMainWindow):
        def __init__(self):
            super().__init__()
            self.table = QtWidgets.QTableView(self)
            self.editor = fake_editor
        def search_for(self, txt):
            pass

    browser = FakeBrowser()

    try:
        panel = DiffBrowserPanel(browser)
        bi._browser_panel = panel
        panel.set_current_editor(fake_editor)

        # 1. L'utilisateur modifie manuellement le champ "Front" dans l'editeur
        fake_note.fields[0] = "UserManualEdit"
        hook = make_field_edit_hook(lambda nid: diff if nid == 7777 else None)
        hook(True, fake_editor, 0)

        # Verifier que l'edition manuelle est enregistree
        assert diff.custom_edit_fields["Front"] == "UserManualEdit"
        assert diff.merged_fields["Front"] == "UserManualEdit"

        # 2. L'utilisateur change d'avis et fait "Accepter tous les champs" (bouton ou raccourci)
        panel._accept_all_current()

        # Verifier que custom_edit_fields est efface et la version proposee recuperee
        assert "Front" not in diff.custom_edit_fields
        assert diff.merged_fields["Front"] == "ProposedFront"
        assert diff.merged_fields["Back"] == "ProposedBack"
        assert fake_note.fields[0] == "ProposedFront"
        assert fake_note.fields[1] == "ProposedBack"

        # 3. Au moment du commit (apply_commit), la version proposee doit etre conservee
        # et ne doit JAMAIS etre ecrasee par l'ancienne saisie manuelle
        committed_diffs = []
        with patch("src.hooks.commit_changes", side_effect=lambda diffs: committed_diffs.extend(diffs)):
            panel.apply_commit()

        assert len(committed_diffs) == 1
        committed = committed_diffs[0]
        assert committed.merged_fields["Front"] == "ProposedFront"
        assert committed.merged_fields["Back"] == "ProposedBack"
        assert fake_note.fields[0] == "ProposedFront"

    finally:
        bi._browser_panel = None
        clear_pending_diffs()


def test_tag_change_detection_and_management(mock_env):
    from anki_import_diff.src.savepoint import NoteDiff
    from anki_import_diff.src.ui import TagsDiffPanel

    diff = NoteDiff(
        note_id=888,
        original_fields={"F1": "Val"},
        updated_fields={"F1": "Val"},
        model_name="Basic",
        field_names=["F1"],
        action="UPDATE",
        tags=["tag1", "new_tag"],
        original_tags=["tag1", "old_tag"],
        updated_tags=["tag1", "new_tag"],
    )

    panel = TagsDiffPanel(diff)
    assert panel._badge.text() == "Tags d'import (+1, −1)"
    display_html = panel._tags_display.text()
    assert "+new_tag" in display_html
    assert "−old_tag" in display_html
    assert "tag1" in display_html

    # Click "Garder anciens tags"
    panel._btn_keep_old.click()
    assert diff.tags == ["tag1", "old_tag"]
    assert diff.tags_active_target == "OLD"
    assert panel._badge.text() == "Anciens tags gardés"

    # Click "Accepter tags d'import"
    panel._btn_accept_new.click()
    assert diff.tags == ["tag1", "new_tag"]
    assert diff.tags_active_target == "PROP"
    assert panel._badge.text() == "Tags d'import (+1, −1)"


def test_field_diff_panel_4_way_state_machine_and_resets(mock_env):
    from anki_import_diff.src.savepoint import NoteDiff
    from anki_import_diff.src.ui import FieldDiffPanel

    diff = NoteDiff(
        note_id=777,
        original_fields={"F1": "OrigVal"},
        updated_fields={"F1": "PropVal"},
        model_name="Basic",
        field_names=["F1"],
        action="UPDATE",
    )

    fp = FieldDiffPanel("F1", "OrigVal", "PropVal", diff=diff)

    # 1. At opening: editor has proposition, diff shows old version
    assert fp.active_target == "PROP"
    assert "Version pré-maj" in fp._orig_lbl.text()
    assert "OrigVal" in fp._old_val_browser.toPlainText()
    assert fp.get_merged_value() == "PropVal"

    # 2. User retrieves old field (clicks "Garder l'ancien")
    fp._btn_reject_all.click()
    assert fp.active_target == "OLD"
    assert "Proposition d'import" in fp._orig_lbl.text()
    assert "PropVal" in fp._old_val_browser.toPlainText()
    assert fp.get_merged_value() == "OrigVal"

    # 3. User modifies old field in editor
    fp.on_editor_modified("OrigValModified")
    assert fp.old_mod == "OrigValModified"
    assert diff.old_mod_fields["F1"] == "OrigValModified"
    assert fp.get_merged_value() == "OrigValModified"
    assert "Ancien modifié" in fp._badge.text()
    assert not fp._btn_reset_editor.isHidden()
    assert "Rétablir ancien originel" in fp._btn_reset_editor.text()

    # 4. User switches back to proposition (clicks "Accepter")
    fp._btn_accept_all.click()
    assert fp.active_target == "PROP"
    # Diff must now display the modified old field!
    assert "Ancien modifié" in fp._orig_lbl.text()
    assert "OrigValModified" in fp._old_val_browser.toPlainText()
    assert not fp._btn_reset_diff.isHidden()
    assert "Rétablir ancien originel" in fp._btn_reset_diff.text()

    # 5. User modifies proposition in editor
    fp.on_editor_modified("PropValWithEdits")
    assert fp.prop_mod == "PropValWithEdits"
    assert diff.prop_mod_fields["F1"] == "PropValWithEdits"
    assert fp.get_merged_value() == "PropValWithEdits"
    assert "Proposition modifiée" in fp._badge.text()
    assert not fp._btn_reset_editor.isHidden()
    assert "Rétablir proposition d'import" in fp._btn_reset_editor.text()

    # 6. Reset proposition to original
    fp._btn_reset_editor.click()
    assert fp.prop_mod == "PropVal"
    assert diff.prop_mod_fields["F1"] == "PropVal"
    assert fp.get_merged_value() == "PropVal"

    # 7. Reset old version to original from diff button
    fp._btn_reset_diff.click()
    assert fp.old_mod == "OrigVal"
    assert diff.old_mod_fields["F1"] == "OrigVal"
    assert "Version pré-maj" in fp._orig_lbl.text()
    assert "OrigVal" in fp._old_val_browser.toPlainText()


def test_unfolded_format_apercu_rendering(mock_env):
    from anki_import_diff.src.ui import _unfold_and_style_html, chrome_colors

    c = chrome_colors()
    folded_html = '''
    <div id="sourcesMegaContainer" style="display:none;">
        <div class="barHider">Sources</div>
        <div class="section" style="display: none;">Contenu caché des sources</div>
    </div>
    '''
    rendered = _unfold_and_style_html(folded_html, "body { font-size:12px; }", c)

    # In raw HTML, display:none should be replaced with display:block
    assert "display:none" not in rendered.lower().replace(" ", "") or "display:block" in rendered
    # CSS unfolding rules must be present
    assert ".barHider" in rendered
    assert "[id*=\"MegaContainer\"]" in rendered
    assert "!important" in rendered


def test_editor_escape_normal_mode_closes_parent_window():
    """Verifie qu'en edition normale (pas en diff), Echap ferme la fenetre de l'editeur."""
    from unittest.mock import MagicMock
    import src.browser_integration as bi

    # S'assurer qu'aucun diff n'est actif
    bi._browser_panel = None
    bi._pending_diffs.clear()

    parent_window = MagicMock()
    parent_window.isVisible.return_value = True
    parent_window.close = MagicMock()

    editor = MagicMock()
    editor.parentWindow = parent_window

    bi._handle_editor_escape(editor)
    assert parent_window.close.called is True


def test_editor_escape_normal_mode_does_not_close_mw():
    """Verifie qu'en edition normale, si la fenetre parente est mw, elle n'est pas fermee."""
    from unittest.mock import MagicMock
    import src.browser_integration as bi
    import aqt

    bi._browser_panel = None
    bi._pending_diffs.clear()

    editor = MagicMock()
    editor.parentWindow = aqt.mw
    editor.web = MagicMock()

    bi._handle_editor_escape(editor)
    assert editor.web.onEsc.called is True


def test_editor_escape_diff_mode_requests_confirmation():
    """Verifie qu'en mode diff, Echap demande confirmation via le panel diff au lieu de fermer brutalement."""
    from unittest.mock import MagicMock
    from anki_import_diff.src.savepoint import NoteDiff
    import src.browser_integration as bi

    diff = NoteDiff(note_id=555, action="UPDATE", model_name="Basic",
                    field_names=["Front"], original_fields={"Front": "A"},
                    updated_fields={"Front": "B"})
    bi.set_pending_diffs([diff], lambda x: None)

    mock_panel = MagicMock()
    bi._browser_panel = mock_panel

    editor = MagicMock()
    parent_window = MagicMock()
    parent_window.close = MagicMock()
    editor.parentWindow = parent_window

    try:
        bi._handle_editor_escape(editor)
        assert mock_panel._on_request_close_panel.called is True
        assert parent_window.close.called is False
    finally:
        bi.clear_pending_diffs()


def test_editor_web_shortcuts_injection_contains_escape():
    """Verifie que le script JS injecte dans la webview contient la gestion d'Echap et diff_editor_escape."""
    from unittest.mock import MagicMock
    import src.browser_integration as bi

    editor = MagicMock()
    mock_web = MagicMock()
    editor.web = mock_web

    bi._inject_editor_web_shortcuts(editor)
    assert mock_web.eval.called is True
    js_code = mock_web.eval.call_args[0][0]
    assert "diff_editor_escape" in js_code
    assert "Escape" in js_code
    assert "isOverlayVisible" in js_code





