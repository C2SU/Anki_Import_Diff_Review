import os
import sys
import pytest
import sqlite3
import threading
import html
import time
from unittest.mock import MagicMock

# Add project root to sys.path to allow absolute imports of 'src' and 'test_import_diff'
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Force Qt offscreen platform for headless execution
os.environ["QT_QPA_PLATFORM"] = "offscreen"

# Import PyQt/PySide classes
try:
    from PyQt6 import QtCore, QtGui, QtWidgets
except ImportError:
    from PySide6 import QtCore, QtGui, QtWidgets

# Reuse mock environments and classes from test_import_diff
from test_import_diff import (
    MockNote, MockModels, MockAnkiDB, MockCollection, MockAddonManager,
    mock_env, init_qapp
)

from anki_import_diff.src.savepoint import NoteDiff, capture_import_changes
from anki_import_diff.src.diff import highlight_diff
from anki_import_diff.src.ui import VisualDiffDialog, show_diff_dialog
from anki_import_diff.src.hooks import setup_hooks, inject_checkbox, commit_changes, intercept_import, wrap_import_handler

# =========================================================================
# TIER 5: Adversarial & Stress Testing
# =========================================================================

# --- 1. EXTREME INPUTS ---

def test_t5_extreme_diff_performance_dos():
    """
    Test how the diff engine handles large strings.
    """
    large_str_1 = "A" * 2000 + "X" + "B" * 2000
    large_str_2 = "A" * 2000 + "Y" + "B" * 2000
    
    # Verify that it doesn't crash and returns formatted diff output
    res = highlight_diff(large_str_1, large_str_2)
    assert len(res) > 0
    assert "background-color: #f8d7da" in res
    assert "background-color: #d4edda" in res


def test_t5_extreme_field_count(mock_env):
    """
    Test how the change capturing and UI behave when a note has 200 fields.
    """
    field_names = [f"Field {i}" for i in range(200)]
    mock_env.col.models.set(2, "HugeModel", field_names)
    
    initial_flds = "\x1f".join(["init"] * 200)
    mock_env.col.db.execute("INSERT INTO notes (id, mid, flds, tags) VALUES (5001, 2, ?, 'tag')", initial_flds)
    
    updated_flds = "\x1f".join(["updated"] * 200)
    def run_import():
        mock_env.col.db.execute("UPDATE notes SET flds=? WHERE id=5001", updated_flds)
        
    diffs = capture_import_changes(run_import)
    assert len(diffs) == 1
    assert len(diffs[0].field_names) == 200
    assert len(diffs[0].original_fields) == 200
    assert len(diffs[0].updated_fields) == 200


def test_t5_extreme_note_ids(mock_env):
    """
    Test how the system behaves with extreme note IDs:
    - Extremely large positive ID (e.g. 2^63 - 1)
    - Negative ID
    - Zero ID
    """
    max_id = 9223372036854775807
    negative_id = -12345
    zero_id = 0
    
    mock_env.col.db.execute("INSERT INTO notes (id, mid, flds, tags) VALUES (?, 1, 'max', 'tag')", max_id)
    mock_env.col.db.execute("INSERT INTO notes (id, mid, flds, tags) VALUES (?, 1, 'neg', 'tag')", negative_id)
    mock_env.col.db.execute("INSERT INTO notes (id, mid, flds, tags) VALUES (?, 1, 'zero', 'tag')", zero_id)
    
    def run_import():
        mock_env.col.db.execute("UPDATE notes SET flds='max_up' WHERE id=?", max_id)
        mock_env.col.db.execute("UPDATE notes SET flds='neg_up' WHERE id=?", negative_id)
        mock_env.col.db.execute("UPDATE notes SET flds='zero_up' WHERE id=?", zero_id)
        
    diffs = capture_import_changes(run_import)
    assert len(diffs) == 3
    
    nids = [d.note_id for d in diffs]
    assert max_id in nids
    assert negative_id in nids
    assert zero_id in nids


def test_t5_extreme_utf8_control_chars(mock_env):
    """
    Test handling of control characters, null bytes, and unicode surrogate pairs in fields.
    """
    mock_env.col.models.set(1, "Basic", ["Field 1", "Field 2"])
    control_char_str = "line1\nline2\r\n\t\x00null_byte\x08backspace"
    # Use standard UTF-8 emoji (which would be surrogate pairs in UTF-16) instead of lone surrogates.
    # Lone surrogates cannot be encoded to UTF-8 by standard python sqlite3 driver.
    surrogate_pair_str = "surrogate:\U0001F600emoji"
    
    mock_env.col.db.execute("INSERT INTO notes (id, mid, flds, tags) VALUES (5002, 1, 'init', 'tag')")
    
    def run_import():
        # Update field with control chars and surrogates
        mock_env.col.db.execute("UPDATE notes SET flds=? WHERE id=5002", control_char_str + "\x1f" + surrogate_pair_str)
        
    diffs = capture_import_changes(run_import)
    assert len(diffs) == 1
    field_vals = list(diffs[0].updated_fields.values())
    assert control_char_str in field_vals or surrogate_pair_str in field_vals


def test_t5_extreme_field_separator_delimiter_leak(mock_env):
    """
    Test if the Anki field separator character (\x1f) inside a field value
    is safely handled without shifting subsequent fields.
    """
    mock_env.col.models.set(3, "TwoFieldsModel", ["F1", "F2"])
    mock_env.col.db.execute("INSERT INTO notes (id, mid, flds, tags) VALUES (5003, 3, 'A\x1fB', 'tag')")
    
    def run_import():
        mock_env.col.db.execute("UPDATE notes SET flds='A\x1fLeak\x1fB' WHERE id=5003")
        
    diffs = capture_import_changes(run_import)
    assert len(diffs) == 1
    note_diff = diffs[0]
    
    # Delimiter leak should NOT shift subsequent fields!
    assert note_diff.updated_fields.get("F2") == "B"
    assert note_diff.updated_fields.get("F1") == "A\x1fLeak"


# --- 2. THREAD SAFETY & CONCURRENCY ---

def test_t5_concurrent_imports_same_connection(mock_env):
    """
    Simulate two concurrent threads attempting to run imports/savepoints
    at the same time on the same connection.
    With global lock serialization, both threads should complete without errors or clashes.
    """
    # Recreate mock DB with check_same_thread=False to allow background threads to access it
    import sqlite3
    mock_env.col.db.conn.close()
    mock_env.col.db.conn = sqlite3.connect(":memory:", isolation_level=None, check_same_thread=False)
    mock_env.col.db.conn.text_factory = lambda b: b.decode('utf-8', 'surrogatepass') if isinstance(b, bytes) else b
    mock_env.col.db.conn.row_factory = sqlite3.Row
    mock_env.col.db._init_schema()

    mock_env.col.db.execute("INSERT INTO notes (id, mid, flds, tags) VALUES (6001, 1, 'A', 'tag')")
    mock_env.col.db.execute("INSERT INTO notes (id, mid, flds, tags) VALUES (6002, 1, 'B', 'tag')")
    
    results = {}
    
    def thread_1_run():
        try:
            # First enter savepoint
            def import_1():
                mock_env.col.db.execute("UPDATE notes SET flds='A_updated' WHERE id=6001")
                time.sleep(0.1)
            diffs = capture_import_changes(import_1)
            results["thread_1"] = diffs
        except Exception as e:
            results["thread_1_err"] = e

    def thread_2_run():
        try:
            # Let thread 1 start first
            time.sleep(0.02)
            def import_2():
                mock_env.col.db.execute("UPDATE notes SET flds='B_updated' WHERE id=6002")
            diffs = capture_import_changes(import_2)
            results["thread_2"] = diffs
        except Exception as e:
            results["thread_2_err"] = e

    t1 = threading.Thread(target=thread_1_run)
    t2 = threading.Thread(target=thread_2_run)
    
    t1.start()
    t2.start()
    t1.join()
    t2.join()
    
    # Verify that both concurrent imports completed successfully with no clashing errors
    assert "thread_1_err" not in results
    assert "thread_2_err" not in results
    
    t1_diffs = results.get("thread_1", [])
    nids = [d.note_id for d in t1_diffs]
    assert 6001 in nids
    
    t2_diffs = results.get("thread_2", [])
    nids_2 = [d.note_id for d in t2_diffs]
    assert 6002 in nids_2


def test_t5_concurrent_note_modification_during_dialog(mock_env):
    """
    Test what happens if a note is modified concurrently in the database
    by another thread/process while the visual diff dialog is open.
    Verify that our commit handles the collision gracefully and preserves the concurrent modification.
    """
    # Initialize a note with 2 fields
    mock_env.col.models.set(4, "TwoFields", ["F1", "F2"])
    mock_env.col.db.execute(
        "INSERT INTO notes (id, mid, flds, tags) VALUES (6003, 4, 'Init1\x1fInit2', 'tag')"
    )
    
    # User imports changes which updates F1 to 'Imported1' and F2 to 'Imported2'
    diff = NoteDiff(
        note_id=6003,
        original_fields={"F1": "Init1", "F2": "Init2"},
        updated_fields={"F1": "Imported1", "F2": "Imported2"},
        model_name="TwoFields",
        field_names=["F1", "F2"],
        action="UPDATE"
    )
    
    # Simulate user choosing to KEEP ORIGINAL for F2, but ACCEPT IMPORTED for F1
    diff.merged_fields["F1"] = "Imported1"
    diff.merged_fields["F2"] = "Init2" # keep original
    
    # Concurrently, another process updates F2 to 'SyncUpdated2' in the database
    mock_env.col.db.execute(
        "UPDATE notes SET flds='Init1\x1fSyncUpdated2' WHERE id=6003"
    )
    
    # Now we commit our diff
    commit_changes([diff])
    
    # Retrieve the final note from DB
    note = mock_env.col.get_note(6003)
    
    # F1 should be 'Imported1'
    assert note.fields[0] == "Imported1"
    # F2 should be 'SyncUpdated2' (proper collision detection/merge).
    assert note.fields[1] == "SyncUpdated2"



# --- 3. DATABASE CONSTRAINTS & SAVEPOINT ROLLBACK INTEGRITY ---

def test_t5_savepoint_failed_rollback_handling(mock_env):
    """
    Test what happens if the database raises an error when executing the ROLLBACK TO SAVEPOINT command.
    Verify if the original import exception is masked or if the DB connection is left open.
    """
    mock_env.col.db.execute("INSERT INTO notes (id, mid, flds, tags) VALUES (7001, 1, 'Initial', 'tag')")
    
    original_execute = mock_env.col.db.execute
    
    def faulty_execute(sql, *args):
        if "ROLLBACK TO SAVEPOINT" in sql:
            raise sqlite3.OperationalError("Mocked rollback failure")
        return original_execute(sql, *args)
        
    mock_env.col.db.execute = faulty_execute
    
    def import_fn():
        mock_env.col.db.execute("UPDATE notes SET flds='Changed' WHERE id=7001")
        raise RuntimeError("Original Import Error")
        
    with pytest.raises(RuntimeError) as exc_info:
        capture_import_changes(import_fn)
        
    # The original "RuntimeError: Original Import Error" is preserved and not masked because rollback failure is handled gracefully!
    assert "Original Import Error" in str(exc_info.value)
    
    # Restore original execute to clean up database state
    mock_env.col.db.execute = original_execute
    # Manual clean up of savepoint and triggers
    try:
        mock_env.col.db.execute("ROLLBACK TO SAVEPOINT import_diff")
    except Exception:
        pass
    try:
        mock_env.col.db.execute("RELEASE SAVEPOINT import_diff")
    except Exception:
        pass


def test_t5_import_fails_with_sqlite_integrity_error(mock_env):
    """
    Test how savepoint rollback behaves when import_fn fails due to a database constraint violation
    (e.g., UNIQUE constraint).
    """
    # Create note 8001
    mock_env.col.db.execute("INSERT INTO notes (id, mid, flds, tags) VALUES (8001, 1, 'Initial', 'tag')")
    
    def import_fn():
        # Attempt to insert a note with duplicate ID to trigger UNIQUE constraint violation
        mock_env.col.db.execute("INSERT INTO notes (id, mid, flds, tags) VALUES (8001, 1, 'Duplicate', 'tag')")
        
    with pytest.raises(sqlite3.IntegrityError):
        capture_import_changes(import_fn)
        
    # The savepoint rollback should successfully restore the DB state
    rows = mock_env.col.db.all("SELECT flds FROM notes WHERE id=8001")
    assert rows[0]["flds"] == "Initial"


def test_t5_existing_active_transaction(mock_env):
    """
    Test if savepoint and trigger logic is robust when there is already an active transaction
    when capture_import_changes is invoked.
    """
    mock_env.col.db.execute("INSERT INTO notes (id, mid, flds, tags) VALUES (9001, 1, 'Initial', 'tag')")
    
    # Start manual transaction
    mock_env.col.db.execute("BEGIN TRANSACTION")
    
    def import_fn():
        mock_env.col.db.execute("UPDATE notes SET flds='Changed' WHERE id=9001")
        
    diffs = capture_import_changes(import_fn)
    assert len(diffs) == 1
    
    # Roll back manual transaction
    mock_env.col.db.execute("ROLLBACK")
    
    # Verify state is initial because the outer transaction was rolled back
    rows = mock_env.col.db.all("SELECT flds FROM notes WHERE id=9001")
    assert rows[0]["flds"] == "Initial"


# --- 4. LARGE TRANSACTIONS ---

def test_t5_large_transaction_memory_stress(mock_env):
    """
    Test capture and instantiation of a large number of NoteDiff structures (e.g. 200).
    Verify performance and that memory doesn't explode.
    """
    # Insert 200 notes
    for i in range(200):
        mock_env.col.db.execute("INSERT INTO notes (id, mid, flds, tags) VALUES (?, 1, 'init', 'tag')", 10000 + i)
        
    def run_large_import():
        for i in range(200):
            mock_env.col.db.execute("UPDATE notes SET flds='updated' WHERE id=?", 10000 + i)
            
    start_time = time.perf_counter()
    diffs = capture_import_changes(run_large_import)
    duration = time.perf_counter() - start_time
    
    print(f"Captured 200 note updates in {duration:.4f} seconds")
    assert len(diffs) == 200


# --- 5. INVALID/MALICIOUS HTML IN DIFFS ---

def test_t5_malicious_html_diff_rendering(qtbot):
    """
    Test if the VisualDiffDialog safely renders field differences containing malicious HTML injection vectors.
    """
    malicious_old = "<b>Normal Text</b>"
    malicious_new = "<script>alert('XSS')</script><img src=x onerror=alert(1)>"
    
    note = NoteDiff(12345, {"Field 1": malicious_old}, {"Field 1": malicious_new}, "Basic", ["Field 1"], "UPDATE")
    dialog = VisualDiffDialog([note])
    qtbot.addWidget(dialog)
    
    html_output = dialog.diff_browser.toHtml()
    
    # Highlight diff escapes values, so script tags are escaped as &lt;script&gt; and shouldn't render as active tags.
    # Check that they do not exist as literal unescaped tags in HTML output.
    assert "<script>" not in html_output
    assert "<img" not in html_output
    # They should be safely escaped:
    assert "alert('XSS')" in html_output or "alert(1)" in html_output


# --- 6. CONFIGURATION STATE CORRUPTIONS ---

def test_t5_config_corruption_string(mock_env):
    """
    Test what happens when the add-on configuration is corrupted and stored as a string instead of a dictionary.
    Verify if it triggers a crash or is handled gracefully.
    """
    # Simulate corrupted config stored as string
    mock_env.addonManager.config = "visual_diff_enabled_but_corrupted_as_string"
    
    called = False
    def dummy_import():
        nonlocal called
        called = True
        return 100
        
    res = intercept_import(dummy_import)
    assert res is None
    assert called


def test_t5_config_corruption_list(mock_env):
    """
    Test what happens when the config is a list instead of a dictionary.
    """
    mock_env.addonManager.config = ["visual_diff_enabled"]
    
    called = False
    def dummy_import():
        nonlocal called
        called = True
        return 100
        
    res = intercept_import(dummy_import)
    assert res is None
    assert called


def test_t5_config_corruption_none(mock_env):
    """
    Test configuration robustness when config returns None.
    """
    mock_env.addonManager.config = None
    
    called = False
    def dummy_import():
        nonlocal called
        called = True
        return 100
        
    res = intercept_import(dummy_import)
    assert res is None
    assert called

def test_t5_lone_surrogate_sanitization(mock_env, monkeypatch):
    """
    Test that lone surrogate unicode strings are sanitized prior to database write to prevent UnicodeEncodeError.
    """
    from anki_import_diff.src.hooks import sanitize_surrogates
    
    # A lone surrogate character
    lone_surrogate = "\ud83d"
    
    sanitized = sanitize_surrogates(lone_surrogate)
    # The lone surrogate should be removed/filtered out by surrogatepass/ignore
    assert lone_surrogate not in sanitized
    
    # A valid surrogate pair (encoded in python standard strings as normal utf-8/utf-16 characters)
    # should be preserved.
    valid_str = "hello \U0001f600 world"
    assert sanitize_surrogates(valid_str) == valid_str


def test_t5_wrap_import_handler_path_matching(mock_env, monkeypatch):
    """
    Test that wrap_import_handler correctly matches and patches requests
    when the path contains 'importCsv' or 'importAnkiPackage' (case-insensitive).
    """
    from anki.import_export_pb2 import ImportCsvRequest
    
    # 1. Create a dummy request with dupe_resolution = DUPLICATE (2)
    req = ImportCsvRequest()
    req.path = "dummy.csv"
    req.metadata.dupe_resolution = 2  # DUPLICATE
    original_data = req.SerializeToString()
    
    # 2. Mock flask.request
    class MockFlaskRequest:
        path = "/importCsv"
        is_json = False
        headers = {}
        environ = {}
        _cached_data = original_data
        def get_data(self):
            return self._cached_data
            
    mock_request = MockFlaskRequest()
    import flask
    monkeypatch.setattr(flask, "request", mock_request)
    
    # 3. We mock the handler to assert that it runs with patched data (dupe_resolution = 0 / UPDATE)
    call_count = 0
    def dummy_original_handler(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        # Verify that flask.request WSGI input was patched
        patched_req_data = flask.request.get_data()
        patched_req = ImportCsvRequest()
        patched_req.ParseFromString(patched_req_data)
        
        if call_count == 1:
            # First call (dry-run) should have patched data (dupe_resolution = 0 / UPDATE)
            assert patched_req.metadata.dupe_resolution == 0
        else:
            # Second call (final run) should restore original data (dupe_resolution = 2 / DUPLICATE)
            assert patched_req.metadata.dupe_resolution == 2
            
        return b"success"
        
    wrapped = wrap_import_handler(dummy_original_handler)
    
    # Since diffs are empty (no DB changes), it should call dummy_original_handler and return its output
    res = wrapped()
    assert res == b"success"
    assert call_count == 2


def test_t5_custom_user_edits_resilience_under_hunk_operations(mock_env):
    """
    Adversarial test: Ensure that manual user modifications are NEVER clobbered
    by individual hunk toggles, field-level preview updates, or background syncs,
    and are only reset when the user explicitly triggers 'accept all hunks'.
    """
    from anki_import_diff.src.browser_integration import (
        DiffBrowserPanel, NoteDiff, set_pending_diffs, clear_pending_diffs
    )
    from anki_import_diff.src.editor_sync import make_field_edit_hook, sync_editor_from_hunks

    diff = NoteDiff(
        note_id=9999,
        original_fields={"F1": "orig1", "F2": "orig2", "F3": "orig3"},
        updated_fields={"F1": "imp1", "F2": "imp2", "F3": "imp3"},
        model_name="Basic",
        field_names=["F1", "F2", "F3"],
        action="UPDATE",
    )
    set_pending_diffs([diff], lambda x: None)

    class FakeBrowser(QtWidgets.QMainWindow):
        def __init__(self):
            super().__init__()
            self.table = MagicMock()
            self.editor = MagicMock()

    try:
        browser = FakeBrowser()
        panel = DiffBrowserPanel(browser)
        note_panel = panel._note_panels[9999]

        # 1. User edits F1 and F3 manually
        fake_note = MagicMock()
        fake_note.id = 9999
        fake_note.fields = ["user_f1_custom", "imp2", "user_f3_custom"]
        fake_editor = MagicMock()
        fake_editor.note = fake_note

        hook = make_field_edit_hook(lambda nid: diff if nid == 9999 else None)
        hook(True, fake_editor, 0)

        assert diff.custom_edit_fields["F1"] == "user_f1_custom"
        assert diff.custom_edit_fields["F3"] == "user_f3_custom"
        assert "F2" not in diff.custom_edit_fields

        # 2. Toggle individual hunks on F1, F2, F3 repeatedly
        fp1 = note_panel._field_panels["F1"]
        fp2 = note_panel._field_panels["F2"]
        fp3 = note_panel._field_panels["F3"]

        for _ in range(5):
            for hw in fp1._hunk_widgets:
                hw.hunk.accepted = not hw.hunk.accepted
                hw._refresh_state()
            for hw in fp2._hunk_widgets:
                hw.hunk.accepted = not hw.hunk.accepted
                hw._refresh_state()
            for hw in fp3._hunk_widgets:
                hw.hunk.accepted = not hw.hunk.accepted
                hw._refresh_state()

            note_panel._do_sync_editor()

            # F1 and F3 MUST remain untouched
            assert diff.merged_fields["F1"] == "user_f1_custom"
            assert diff.merged_fields["F3"] == "user_f3_custom"
            merged = note_panel.get_merged_fields()
            assert merged["F1"] == "user_f1_custom"
            assert merged["F3"] == "user_f3_custom"

        # 3. Reject all hunks — custom edits must STILL be preserved
        panel._reject_all_current()
        merged = note_panel.get_merged_fields()
        assert merged["F1"] == "user_f1_custom"
        assert merged["F3"] == "user_f3_custom"
        assert diff.merged_fields["F1"] == "user_f1_custom"
        assert diff.merged_fields["F3"] == "user_f3_custom"

        # 4. Accept all hunks — custom edits are cleared and import hunks apply
        panel._accept_all_current()
        merged = note_panel.get_merged_fields()
        assert merged["F1"] == "imp1"
        assert merged["F2"] == "imp2"
        assert merged["F3"] == "imp3"
        assert diff.merged_fields["F1"] == "imp1"
        assert diff.merged_fields["F3"] == "imp3"
        assert len(diff.custom_edit_fields) == 0

    finally:
        clear_pending_diffs()


