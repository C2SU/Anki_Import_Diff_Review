"""
test_history.py — Tests unitaires pour le module src/history.py
Teste NoteHistoryDB et ProtectedFieldsDB de façon isolée (in-memory SQLite).
"""

import os
import sys
import pytest
import tempfile
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from anki_import_diff.src.history import NoteHistoryDB, ProtectedFieldsDB, VersionEntry


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def tmp_db(tmp_path):
    """DB in-memory in a temp dir for isolation."""
    db_path = str(tmp_path / "test_history.db")
    db = NoteHistoryDB(db_path=db_path)
    yield db
    # cleanup singleton if set to this instance
    NoteHistoryDB._instance = None


@pytest.fixture
def protection_db(tmp_db):
    return ProtectedFieldsDB(db=tmp_db)


# ---------------------------------------------------------------------------
# NoteHistoryDB tests
# ---------------------------------------------------------------------------

class TestNoteHistoryDB:
    def test_store_and_retrieve_snapshot(self, tmp_db):
        vid = tmp_db.store_snapshot(
            note_id=1,
            fields={"Recto": "Hello", "Verso": "World"},
            source="import_csv",
            comment="Test snapshot",
        )
        assert isinstance(vid, str) and len(vid) == 32

        history = tmp_db.get_history(1)
        assert len(history) == 1
        assert history[0].version_id == vid
        assert history[0].note_id == 1
        assert history[0].source == "import_csv"
        assert history[0].comment == "Test snapshot"
        assert history[0].fields["Recto"] == "Hello"
        assert history[0].fields["Verso"] == "World"

    def test_multiple_snapshots_ordered_newest_first(self, tmp_db):
        tmp_db.store_snapshot(1, {"F": "v1"}, comment="first")
        tmp_db.store_snapshot(1, {"F": "v2"}, comment="second")
        tmp_db.store_snapshot(1, {"F": "v3"}, comment="third")

        history = tmp_db.get_history(1)
        assert len(history) == 3
        # Most recent first
        assert history[0].comment == "third"
        assert history[2].comment == "first"

    def test_get_version_by_id(self, tmp_db):
        vid = tmp_db.store_snapshot(2, {"X": "value"}, source="manual", comment="specific")
        entry = tmp_db.get_version(vid)
        assert entry is not None
        assert entry.fields["X"] == "value"
        assert entry.source == "manual"

    def test_get_version_nonexistent_returns_none(self, tmp_db):
        result = tmp_db.get_version("nonexistent-version-id")
        assert result is None

    def test_prune_old_versions(self, tmp_db):
        for i in range(10):
            tmp_db.store_snapshot(3, {"F": f"v{i}"}, comment=f"snap{i}")

        tmp_db.prune_old_versions(3, keep=5)
        history = tmp_db.get_history(3)
        assert len(history) == 5
        # Should keep the 5 most recent
        assert history[0].comment == "snap9"

    def test_auto_prune_at_50_versions(self, tmp_db):
        for i in range(55):
            tmp_db.store_snapshot(4, {"F": f"v{i}"}, comment=f"snap{i}")

        history = tmp_db.get_history(4)
        assert len(history) == 50

    def test_delete_note_history(self, tmp_db):
        tmp_db.store_snapshot(5, {"F": "v1"})
        tmp_db.store_snapshot(5, {"F": "v2"})
        assert len(tmp_db.get_history(5)) == 2

        tmp_db.delete_note_history(5)
        assert len(tmp_db.get_history(5)) == 0

    def test_clear_all(self, tmp_db):
        tmp_db.store_snapshot(6, {"F": "x"})
        tmp_db.store_snapshot(7, {"F": "y"})
        assert tmp_db.note_count() == 2

        tmp_db.clear_all()
        assert tmp_db.note_count() == 0
        assert tmp_db.version_count() == 0

    def test_note_count_and_version_count(self, tmp_db):
        tmp_db.store_snapshot(8, {"F": "v1"})
        tmp_db.store_snapshot(8, {"F": "v2"})
        tmp_db.store_snapshot(9, {"F": "v3"})

        assert tmp_db.note_count() == 2
        assert tmp_db.version_count() == 3

    def test_empty_fields_stored_correctly(self, tmp_db):
        tmp_db.store_snapshot(10, {})
        history = tmp_db.get_history(10)
        assert len(history) == 1
        assert history[0].fields == {}

    def test_unicode_fields(self, tmp_db):
        tmp_db.store_snapshot(11, {"Recto": "Hépatite A → B ≠ C", "Verso": "日本語テスト"})
        history = tmp_db.get_history(11)
        assert history[0].fields["Recto"] == "Hépatite A → B ≠ C"
        assert history[0].fields["Verso"] == "日本語テスト"

    def test_html_fields(self, tmp_db):
        html_val = "<b>Bold</b> <span class='cloze'>{{c1::test}}</span>"
        tmp_db.store_snapshot(12, {"Recto": html_val})
        history = tmp_db.get_history(12)
        assert history[0].fields["Recto"] == html_val

    def test_isolated_by_note_id(self, tmp_db):
        tmp_db.store_snapshot(20, {"F": "note20"})
        tmp_db.store_snapshot(21, {"F": "note21"})

        h20 = tmp_db.get_history(20)
        h21 = tmp_db.get_history(21)
        assert len(h20) == 1 and h20[0].fields["F"] == "note20"
        assert len(h21) == 1 and h21[0].fields["F"] == "note21"

    def test_version_entry_timestamp_str(self, tmp_db):
        tmp_db.store_snapshot(30, {"F": "v"})
        entry = tmp_db.get_history(30)[0]
        # Format: "YYYY-MM-DD HH:MM"
        ts = entry.timestamp_str
        assert len(ts) == 16
        assert ts[4] == "-" and ts[7] == "-"

    def test_version_entry_source_icon(self, tmp_db):
        tmp_db.store_snapshot(31, {"F": "v"}, source="import_csv")
        tmp_db.store_snapshot(32, {"F": "v"}, source="ankicollab")
        tmp_db.store_snapshot(33, {"F": "v"}, source="manual")

        e31 = tmp_db.get_history(31)[0]
        e32 = tmp_db.get_history(32)[0]
        e33 = tmp_db.get_history(33)[0]
        assert e31.source_icon == "[CSV]"
        assert e32.source_icon == "[Collab]"
        assert e33.source_icon == "[Manuel]"

    def test_tags_stored_and_retrieved(self, tmp_db):
        tmp_db.store_snapshot(
            note_id=40,
            fields={"F": "val"},
            source="manual",
            tags=["tag1", "tag2"],
        )
        history = tmp_db.get_history(40)
        assert len(history) == 1
        assert history[0].fields == {"F": "val"}
        assert history[0].tags == ["tag1", "tag2"]

        # If tags are empty
        tmp_db.store_snapshot(
            note_id=41,
            fields={"F": "val"},
            source="manual",
            tags=[],
        )
        history = tmp_db.get_history(41)
        assert len(history) == 1
        assert history[0].fields == {"F": "val"}
        assert history[0].tags == []

    def test_update_snapshot_fields(self, tmp_db):
        vid = tmp_db.store_snapshot(
            note_id=42,
            fields={"F": "original"},
            source="manual",
            comment="Initial",
            tags=["tagA"]
        )
        tmp_db.update_snapshot_fields(
            version_id=vid,
            fields={"F": "updated"},
            comment="Modified"
        )
        entry = tmp_db.get_version(vid)
        assert entry is not None
        assert entry.fields == {"F": "updated"}
        assert entry.comment == "Modified"
        # Verify tags are preserved
        assert entry.tags == ["tagA"]

    def test_exact_duplicate_not_stored(self, tmp_db):
        vid1 = tmp_db.store_snapshot(50, {"F": "same"}, tags=["t1"])
        vid2 = tmp_db.store_snapshot(50, {"F": "same"}, tags=["t1"])
        history = tmp_db.get_history(50)
        assert len(history) == 1
        assert history[0].version_id == vid1

    def test_session_coalescing(self, tmp_db):
        # Initial edit in session
        vid1 = tmp_db.store_snapshot(51, {"F": "draft 1"}, source="manual", comment="Sauvegarde automatique (édition)")
        # Second rapid edit within same session
        vid2 = tmp_db.store_snapshot(51, {"F": "draft 2"}, source="manual", comment="Sauvegarde automatique (édition)")
        history = tmp_db.get_history(51)
        assert len(history) == 1
        assert history[0].fields["F"] == "draft 2"

    def test_cleanup_redundant_duplicates(self, tmp_db):
        conn = tmp_db._conn()
        conn.execute("INSERT INTO note_versions VALUES ('v1', 52, '2026-09-01T10:00:00', 'manual', 'c1', '{\"F\": \"val\"}')")
        conn.execute("INSERT INTO note_versions VALUES ('v2', 52, '2026-09-01T10:01:00', 'manual', 'c2', '{\"F\": \"val\"}')")
        conn.commit()
        assert len(tmp_db.get_history(52)) == 2
        tmp_db.cleanup_redundant_duplicates()
        history = tmp_db.get_history(52)
        assert len(history) == 1



# ---------------------------------------------------------------------------
# ProtectedFieldsDB tests
# ---------------------------------------------------------------------------

class TestProtectedFieldsDB:
    def test_protect_and_check(self, tmp_db, protection_db):
        assert not protection_db.is_protected(100, "Recto")
        protection_db.protect_field(100, "Recto", "Valeur protégée")
        assert protection_db.is_protected(100, "Recto")

    def test_get_protected_value(self, tmp_db, protection_db):
        protection_db.protect_field(101, "Verso", "Value locked")
        val = protection_db.get_protected_value(101, "Verso")
        assert val == "Value locked"

    def test_get_protected_value_nonexistent_returns_none(self, protection_db):
        assert protection_db.get_protected_value(999, "NoField") is None

    def test_unprotect_field(self, protection_db):
        protection_db.protect_field(102, "F1", "locked")
        assert protection_db.is_protected(102, "F1")
        protection_db.unprotect_field(102, "F1")
        assert not protection_db.is_protected(102, "F1")

    def test_get_all_protected(self, protection_db):
        protection_db.protect_field(103, "Recto", "val_recto")
        protection_db.protect_field(103, "Verso", "val_verso")

        all_p = protection_db.get_all_protected(103)
        assert all_p == {"Recto": "val_recto", "Verso": "val_verso"}

    def test_get_all_protected_empty(self, protection_db):
        assert protection_db.get_all_protected(999) == {}

    def test_protect_replace_existing(self, protection_db):
        protection_db.protect_field(104, "F", "old_value")
        protection_db.protect_field(104, "F", "new_value")
        assert protection_db.get_protected_value(104, "F") == "new_value"

    def test_isolated_by_note_id(self, protection_db):
        protection_db.protect_field(200, "F", "val200")
        protection_db.protect_field(201, "F", "val201")

        assert protection_db.get_protected_value(200, "F") == "val200"
        assert protection_db.get_protected_value(201, "F") == "val201"

    def test_clear_note_protections(self, protection_db):
        protection_db.protect_field(202, "F1", "v1")
        protection_db.protect_field(202, "F2", "v2")
        assert len(protection_db.get_all_protected(202)) == 2

        protection_db.clear_note_protections(202)
        assert len(protection_db.get_all_protected(202)) == 0

    def test_protection_info_contains_timestamp(self, protection_db):
        protection_db.protect_field(203, "Field", "locked_val")
        info = protection_db.get_protection_info(203, "Field")
        assert info is not None
        assert info["locked_value"] == "locked_val"
        assert isinstance(info["locked_at"], datetime)

    def test_protection_info_nonexistent_returns_none(self, protection_db):
        assert protection_db.get_protection_info(999, "NoField") is None

    def test_multiple_fields_per_note(self, protection_db):
        """Each field of a note can be independently protected."""
        fields = {"Recto": "r", "Verso": "v", "Extra": "e"}
        for name, val in fields.items():
            protection_db.protect_field(300, name, val)

        for name, val in fields.items():
            assert protection_db.is_protected(300, name)
            assert protection_db.get_protected_value(300, name) == val

        protection_db.unprotect_field(300, "Verso")
        assert not protection_db.is_protected(300, "Verso")
        assert protection_db.is_protected(300, "Recto")
        assert protection_db.is_protected(300, "Extra")


def test_api_client(qtbot):
    import sys
    import importlib
    import aqt
    from aqt.qt import QMainWindow
    
    from unittest.mock import MagicMock
    
    # Mock mw
    class MockAddonManager:
        def getConfig(self, name):
            return {}
        def writeConfig(self, name, config):
            pass
            
    class MockMW(QMainWindow):
        def __init__(self):
            super().__init__()
            self.addonManager = MockAddonManager()
            self.pm = MagicMock()
            self.taskman = MagicMock()
            
    aqt.mw = MockMW()
    
    sys.path.insert(0, r'c:\Users\Pipou\AppData\Roaming\Anki2\addons21')
    try:
        mod_auth = importlib.import_module("1957538407.auth_manager")
        mod_api = importlib.import_module("1957538407.api_client")
        auth_manager = mod_auth.auth_manager
        api_client = mod_api.api_client
        print("\n[API] Modules imported!")
        print(f"[API] Logged in: {auth_manager.is_logged_in()}")
        token = auth_manager.get_token()
        if token:
            resp = api_client.get("/GetNotificationsHistory?offset=0&limit=20", auth=True, timeout=5)
            print(f"[API] GET /GetNotificationsHistory -> Status {resp.status_code}")
            if resp.status_code == 200:
                data = resp.json()
                items = data.get("items", [])
                print(f"[API] Found {len(items)} notification items.")
                for item in items[:5]:
                    print(f"  Item: {item}")
    except Exception as e:
        print(f"\n[API] Failed: {e}")
