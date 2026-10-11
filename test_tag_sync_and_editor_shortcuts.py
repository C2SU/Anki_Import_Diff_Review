"""
test_tag_sync_and_editor_shortcuts.py — Tests de régression pour :
1. Actualisation des tags dans l'éditeur Anki lors de l'acceptation puis du refus d'une proposition.
2. Fonctionnement des raccourcis clavier (F2, F3, Alt+A, Alt+R) lorsqu'on clique entre des champs
   dans l'éditeur (aucun champ sélectionné, currentField is None).
"""
import sys
import os
from unittest.mock import MagicMock
import pytest
from PyQt6 import QtCore, QtGui, QtWidgets

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from src.savepoint import NoteDiff
from src.editor_sync import sync_tags_to_editor, sync_editor_from_hunks
from src.browser_integration import (
    DiffBrowserPanel,
    NoteDiffPanelWithSync,
    set_pending_diffs,
    clear_pending_diffs,
    _is_inside_field,
)
import src.browser_integration as bi


class MockEditor:
    def __init__(self, note):
        self.note = note
        self.currentField = None
        self.web = MagicMock()
        self.js_calls = []
        self.web.evalWithCallback = lambda js, cb=None: self.js_calls.append(js)
        self.tags = QtWidgets.QLineEdit()
        self.update_tags_called = False

    def updateTags(self):
        self.update_tags_called = True
        if self.note:
            self.tags.setText(" ".join(self.note.tags))


class MockNote:
    def __init__(self, nid, fields, tags):
        self.id = nid
        self.fields = list(fields)
        self.tags = list(tags)

    def string_tags(self):
        return " ".join(self.tags)


def test_sync_tags_to_editor_updates_all_targets(qtbot):
    """Vérifie que sync_tags_to_editor met à jour note.tags, editor.tags (Qt), updateTags et le JS."""
    note = MockNote(101, ["Front", "Back"], ["old_tag"])
    editor = MockEditor(note)

    new_tags = ["tag1", "tag2"]
    sync_tags_to_editor(editor, new_tags)

    # 1. Note tags
    assert note.tags == ["tag1", "tag2"]
    # 2. Qt QLineEdit widget
    assert editor.tags.text() == "tag1 tag2"
    # 3. updateTags appelé
    assert editor.update_tags_called is True
    # 4. JS setTags appelé
    assert any("setTags" in call and "tag1" in call and "tag2" in call for call in editor.js_calls)


def test_accept_then_reject_updates_editor_tags(qtbot):
    """Vérifie qu'accepter puis refuser met bien à jour le champ des tags dans l'éditeur."""
    orig_tags = ["tag_ancien"]
    upd_tags = ["tag_ancien", "tag_nouveau"]

    diff = NoteDiff(
        note_id=202,
        action="UPDATE",
        model_name="Basic",
        field_names=["Front"],
        original_fields={"Front": "OldFront"},
        updated_fields={"Front": "NewFront"},
        tags=list(upd_tags),
        original_tags=list(orig_tags),
        updated_tags=list(upd_tags),
    )
    set_pending_diffs([diff], lambda x: None)

    class FakeBrowser(QtWidgets.QMainWindow):
        def __init__(self):
            super().__init__()
            self.table = QtWidgets.QTableView(self)
            self.editor = None

    browser = FakeBrowser()
    qtbot.addWidget(browser)
    browser.show()

    try:
        panel = DiffBrowserPanel(browser)
        bi._browser_panel = panel
        note_panel = panel._note_panels[202]

        note = MockNote(202, ["OldFront"], list(orig_tags))
        editor = MockEditor(note)
        browser.editor = editor
        note_panel.set_editor(editor)

        # 1. Accepter la proposition
        note_panel.accept_all_hunks()
        assert diff.tags == upd_tags
        assert diff.tags_active_target == "PROP"
        assert editor.tags.text() == "tag_ancien tag_nouveau"
        assert note.tags == upd_tags

        # 2. Refuser la proposition (revenir à l'ancien)
        note_panel.reject_all_hunks()
        assert diff.tags == orig_tags
        assert diff.tags_active_target == "OLD"
        assert editor.tags.text() == "tag_ancien"
        assert note.tags == orig_tags

        # 3. Tester via les boutons du panneau de tags directement
        tags_panel = note_panel._tags_panel
        # Accepter tags
        tags_panel._accept_new_tags()
        assert diff.tags == upd_tags
        assert editor.tags.text() == "tag_ancien tag_nouveau"

        # Garder anciens tags
        tags_panel._keep_old_tags()
        assert diff.tags == orig_tags
        assert editor.tags.text() == "tag_ancien"

    finally:
        clear_pending_diffs()
        bi._browser_panel = None


def test_shortcuts_when_clicking_between_fields(qtbot):
    """
    Vérifie que lorsqu'on clique sur l'éditeur entre des champs (donc aucun champ sélectionné,
    editor.currentField is None), les raccourcis F2 (accepter) et F3 (rejeter) fonctionnent.
    """
    diff = NoteDiff(
        note_id=303,
        action="UPDATE",
        model_name="Basic",
        field_names=["Front"],
        original_fields={"Front": "OldFront"},
        updated_fields={"Front": "NewFront"},
        tags=["old"],
        original_tags=["old"],
        updated_tags=["new"],
    )
    set_pending_diffs([diff], lambda x: None)

    class FakeWebWidget(QtWidgets.QWidget):
        pass

    class FakeBrowser(QtWidgets.QMainWindow):
        def __init__(self):
            super().__init__()
            self.table = QtWidgets.QTableView(self)
            self.editor = MagicMock()
            self.editor.currentField = None
            self.editor.web = FakeWebWidget(self)

    browser = FakeBrowser()
    qtbot.addWidget(browser)
    browser.show()

    try:
        panel = DiffBrowserPanel(browser)
        bi._browser_panel = panel
        note_panel = panel._note_panels[303]

        # Cas 1 : Aucun champ sélectionné (clic entre les champs de l'éditeur)
        browser.editor.currentField = None
        assert _is_inside_field(panel) is False

        # Vérifier que F2 (Accepter) fonctionne même si l'événement vise le webview de l'éditeur
        ev_f2 = QtGui.QKeyEvent(QtCore.QEvent.Type.KeyPress, QtCore.Qt.Key.Key_F2, QtCore.Qt.KeyboardModifier.NoModifier)
        handled = panel._event_filter.eventFilter(browser.editor.web, ev_f2)
        assert handled is True
        assert all(h.accepted for fp in note_panel._field_panels.values() for h in fp.hunks)
        assert diff.tags == ["new"]

        # Vérifier que F3 (Rejeter / Garder ancien) fonctionne
        ev_f3 = QtGui.QKeyEvent(QtCore.QEvent.Type.KeyPress, QtCore.Qt.Key.Key_F3, QtCore.Qt.KeyboardModifier.NoModifier)
        handled = panel._event_filter.eventFilter(browser.editor.web, ev_f3)
        assert handled is True
        assert not any(h.accepted for fp in note_panel._field_panels.values() for h in fp.hunks)
        assert diff.tags == ["old"]

        # Cas 2 : Un champ est sélectionné (focus dans un champ de saisie)
        browser.editor.currentField = 0
        assert _is_inside_field(panel) is True

        # F2 et F3 doivent TOUJOURS fonctionner, peu importe la cible ou le focus de l'utilisateur
        handled = panel._event_filter.eventFilter(browser.editor.web, ev_f2)
        assert handled is True
        assert all(h.accepted for fp in note_panel._field_panels.values() for h in fp.hunks)

        handled_f3 = panel._event_filter.eventFilter(browser.editor.web, ev_f3)
        assert handled_f3 is True
        assert not any(h.accepted for fp in note_panel._field_panels.values() for h in fp.hunks)

        # En revanche, les combinaisons avec des lettres (Alt+A) sont protégées quand le focus est dans un champ
        ev_alt_a = QtGui.QKeyEvent(QtCore.QEvent.Type.KeyPress, QtCore.Qt.Key.Key_A, QtCore.Qt.KeyboardModifier.AltModifier)
        handled_alt = panel._event_filter.eventFilter(browser.editor.web, ev_alt_a)
        assert handled_alt is False

    finally:
        clear_pending_diffs()
        bi._browser_panel = None


def test_f2_f3_work_across_all_targets_in_diff_browser(qtbot):
    """
    Vérifie que F2 et F3 fonctionnent inconditionnellement peu importe la cible de l'utilisateur :
    - Table / liste des cartes (partie parcourir)
    - Champ de recherche (QLineEdit)
    - Panneau latéral des liens (dock assistant / Lier les cartes)
    - Panneau latéral de diff (dock diff)
    - Éditeur Anki
    """
    diff = NoteDiff(
        note_id=999,
        action="UPDATE",
        model_name="Basic",
        field_names=["Front"],
        original_fields={"Front": "Old"},
        updated_fields={"Front": "New"},
        tags=["t_old"],
        original_tags=["t_old"],
        updated_tags=["t_new"],
    )
    set_pending_diffs([diff], lambda x: None)

    class FakeBrowser(QtWidgets.QMainWindow):
        def __init__(self):
            super().__init__()
            self.table = QtWidgets.QTableView(self)
            self.search_edit = QtWidgets.QLineEdit(self)
            self.editor = MagicMock()
            self.editor.currentField = 1
            self.editor.web = QtWidgets.QWidget(self)

    browser = FakeBrowser()
    qtbot.addWidget(browser)

    # Simuler le dock assistant "menu latéral liens"
    assistant_dock = QtWidgets.QDockWidget("Liens", browser)
    assistant_child = QtWidgets.QWidget(assistant_dock)
    browser._edn_assistant_dock = assistant_dock

    browser.show()

    try:
        panel = DiffBrowserPanel(browser)
        bi._browser_panel = panel
        note_panel = panel._note_panels[999]

        ev_f2 = QtGui.QKeyEvent(QtCore.QEvent.Type.KeyPress, QtCore.Qt.Key.Key_F2, QtCore.Qt.KeyboardModifier.NoModifier)
        ev_f3 = QtGui.QKeyEvent(QtCore.QEvent.Type.KeyPress, QtCore.Qt.Key.Key_F3, QtCore.Qt.KeyboardModifier.NoModifier)

        targets = [
            ("table (parcourir)", browser.table),
            ("search_edit (recherche)", browser.search_edit),
            ("assistant_dock (menu latéral liens)", assistant_child),
            ("panel (menu latéral diff)", panel),
            ("editor.web (éditeur)", browser.editor.web),
        ]

        for name, target in targets:
            # Tester F2 (Accepter)
            note_panel.reject_all_hunks()
            handled_f2 = panel._event_filter.eventFilter(target, ev_f2)
            assert handled_f2 is True, f"F2 doit fonctionner sur {name}"
            assert all(h.accepted for fp in note_panel._field_panels.values() for h in fp.hunks), f"Hunks acceptés via F2 sur {name}"
            assert diff.tags == ["t_new"], f"Tags acceptés via F2 sur {name}"

            # Tester F3 (Rejeter)
            handled_f3 = panel._event_filter.eventFilter(target, ev_f3)
            assert handled_f3 is True, f"F3 doit fonctionner sur {name}"
            assert not any(h.accepted for fp in note_panel._field_panels.values() for h in fp.hunks), f"Hunks rejetés via F3 sur {name}"
            assert diff.tags == ["t_old"], f"Tags restaurés via F3 sur {name}"

    finally:
        clear_pending_diffs()
        bi._browser_panel = None


def test_handle_editor_escape_normal_mode_closes_window(qtbot):
    """Vérifie qu'en mode normal (hors diff), _handle_editor_escape ferme la fenêtre de l'éditeur sans confirmation."""
    bi.clear_pending_diffs()
    bi._browser_panel = None

    class FakeEditCurrent(QtWidgets.QMainWindow):
        def __init__(self):
            super().__init__()
            self.closed = False
            self.close_with_callback_called = False

        def closeWithCallback(self, cb):
            self.close_with_callback_called = True
            cb()

        def close(self):
            self.closed = True
            return super().close()

    win = FakeEditCurrent()
    qtbot.addWidget(win)

    class FakeEditor:
        def __init__(self, parent_win):
            self.parentWindow = parent_win

    editor = FakeEditor(win)

    # Appeler _handle_editor_escape en mode normal
    bi._handle_editor_escape(editor)

    assert win.close_with_callback_called is True
    assert win.closed is True


def test_handle_editor_escape_diff_mode_triggers_notification(qtbot):
    """Vérifie qu'en mode diff actif avec diffs en attente, _handle_editor_escape déclenche obligatoirement la demande de confirmation."""
    diff = NoteDiff(
        note_id=404,
        action="UPDATE",
        model_name="Basic",
        field_names=["Front"],
        original_fields={"Front": "A"},
        updated_fields={"Front": "B"},
        tags=["t1"],
        original_tags=["t1"],
        updated_tags=["t2"],
    )
    bi.set_pending_diffs([diff], lambda x: None)

    class FakeDiffPanel:
        def __init__(self):
            self.request_close_called = False

        def _on_request_close_panel(self):
            self.request_close_called = True

    fake_panel = FakeDiffPanel()
    bi._browser_panel = fake_panel

    class FakeEditor:
        def __init__(self):
            self.parentWindow = MagicMock()

    editor = FakeEditor()

    try:
        bi._handle_editor_escape(editor)
        # La notification / demande de confirmation de fermeture doit impérativement avoir été appelée
        assert fake_panel.request_close_called is True
        # La fenêtre ne doit PAS avoir été fermée directement
        editor.parentWindow.close.assert_not_called()
    finally:
        bi.clear_pending_diffs()
        bi._browser_panel = None


def test_ctrl_alt_c_does_not_copy_old_fields_and_triggers_nid_copy(qtbot, monkeypatch):
    """
    Verifie que Ctrl+Alt+C ne declenche PAS _copy_current_note_old_fields
    (qui injectait les marqueurs '--- Front ---' dans le presse-papier),
    mais declenche bien la copie de NID 'Lier les cartes' (_trigger_copy_nid).
    """
    diff = NoteDiff(
        note_id=505,
        action="UPDATE",
        model_name="Basic",
        field_names=["Front", "Back"],
        original_fields={"Front": "Q505", "Back": "A505"},
        updated_fields={"Front": "Q505_new", "Back": "A505_new"},
        tags=["t"],
        original_tags=["t"],
        updated_tags=["t"],
    )
    bi.set_pending_diffs([diff], lambda x: None)

    class FakeWebWidget(QtWidgets.QWidget):
        pass

    class FakeBrowser(QtWidgets.QMainWindow):
        def __init__(self):
            super().__init__()
            self.table = QtWidgets.QTableView(self)
            self.editor = MagicMock()
            self.editor.currentField = None
            self.editor.web = FakeWebWidget(self)

    browser = FakeBrowser()
    qtbot.addWidget(browser)
    browser.show()

    try:
        panel = DiffBrowserPanel(browser)
        bi._browser_panel = panel

        copy_old_fields_called = []
        copy_nid_called = []

        monkeypatch.setattr(panel, "_copy_current_note_old_fields", lambda: copy_old_fields_called.append(True))
        monkeypatch.setattr(bi, "_trigger_copy_nid", lambda p, b: copy_nid_called.append(True))

        # 1. Ctrl+Alt+C (raccourci Lier les cartes / Copier NID)
        mods_ctrl_alt = QtCore.Qt.KeyboardModifier.ControlModifier | QtCore.Qt.KeyboardModifier.AltModifier
        ev_ctrl_alt_c = QtGui.QKeyEvent(QtCore.QEvent.Type.KeyPress, QtCore.Qt.Key.Key_C, mods_ctrl_alt)
        handled = panel._event_filter.eventFilter(browser.editor.web, ev_ctrl_alt_c)

        assert handled is True
        assert len(copy_old_fields_called) == 0, "Ctrl+Alt+C ne doit PAS copier les anciens champs diff"
        assert len(copy_nid_called) == 1, "Ctrl+Alt+C doit declencher la copie de NID"

        # 2. Alt+C seul (raccourci copie champs pre-maj)
        ev_alt_c = QtGui.QKeyEvent(QtCore.QEvent.Type.KeyPress, QtCore.Qt.Key.Key_C, QtCore.Qt.KeyboardModifier.AltModifier)
        handled_alt = panel._event_filter.eventFilter(browser.editor.web, ev_alt_c)

        assert handled_alt is True
        assert len(copy_old_fields_called) == 1, "Alt+C doit copier les anciens champs diff"
        assert len(copy_nid_called) == 1

        # 3. Ctrl+Shift+C (autre raccourci copie champs pre-maj)
        mods_ctrl_shift = QtCore.Qt.KeyboardModifier.ControlModifier | QtCore.Qt.KeyboardModifier.ShiftModifier
        ev_ctrl_shift_c = QtGui.QKeyEvent(QtCore.QEvent.Type.KeyPress, QtCore.Qt.Key.Key_C, mods_ctrl_shift)
        handled_ctrl_shift = panel._event_filter.eventFilter(browser.editor.web, ev_ctrl_shift_c)

        assert handled_ctrl_shift is True
        assert len(copy_old_fields_called) == 2, "Ctrl+Shift+C doit copier les anciens champs diff"
        assert len(copy_nid_called) == 1

    finally:
        bi.clear_pending_diffs()
        bi._browser_panel = None


