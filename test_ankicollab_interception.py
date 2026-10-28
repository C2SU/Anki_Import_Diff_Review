import os
import sys
import types
from unittest.mock import MagicMock, patch
import pytest

# Ensure parent directory is in sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
os.environ["QT_QPA_PLATFORM"] = "offscreen"

from anki_import_diff.src.savepoint import NoteDiff
import anki_import_diff.src.ankicollab_hook as ack
import anki_import_diff.src.browser_integration as bi


@pytest.fixture(autouse=True)
def reset_hook_state():
    """Reset internal hook state between tests."""
    ack._is_hooked = False
    ack._is_suggestions_hooked = False
    ack._in_collab_dry_run = False
    ack._new_deck_subscriptions.clear()
    bi.clear_pending_diffs()
    yield
    ack._is_hooked = False
    ack._is_suggestions_hooked = False
    ack._in_collab_dry_run = False
    ack._new_deck_subscriptions.clear()
    bi.clear_pending_diffs()


def _create_mock_collab_import_manager():
    """Creates a mock AnkiCollab import_manager module."""
    mod = types.ModuleType("1957538407.import_manager")

    def dummy_install_update(subscription, input_hash=None, update_timestamp_after=False):
        return ("dummy_update_res", subscription, input_hash)

    def dummy_install_deck_op(deck, config, map_cache=None, note_type_data=None):
        return (42, {"success": True, "downloaded": 0, "skipped": 5, "deck_hash": "hash123", "missing_files": []})

    def dummy_on_deck_installed(install_result, deck, subscription, input_hash=None, update_timestamp_after=False):
        return "deck_installed_ok"

    mod.install_update = dummy_install_update
    mod._install_deck_op = dummy_install_deck_op
    mod._on_deck_installed = dummy_on_deck_installed

    return mod


def test_ankicollab_hook_installation():
    """Tests that setup_ankicollab_hook correctly wraps AnkiCollab functions."""
    mock_im = _create_mock_collab_import_manager()
    sys.modules["1957538407.import_manager"] = mock_im

    success = ack.setup_ankicollab_hook()
    assert success is True
    assert ack._is_hooked is True
    assert getattr(mock_im._install_deck_op, "_import_diff_hooked", False) is True
    assert getattr(mock_im._on_deck_installed, "_import_diff_hooked", False) is True
    assert getattr(mock_im.install_update, "_import_diff_hooked", False) is True


def test_ankicollab_update_intercepted_when_diffs_exist():
    """Tests that an update with modified notes intercepts and opens the browser diff."""
    mock_im = _create_mock_collab_import_manager()
    sys.modules["1957538407.import_manager"] = mock_im
    ack.setup_ankicollab_hook()

    fake_diff = NoteDiff(
        note_id=101,
        original_fields={"Recto": "Ancien texte"},
        updated_fields={"Recto": "Nouveau texte"},
        model_name="Basique",
        field_names=["Recto"],
        action="UPDATE",
    )

    deck_mock = MagicMock()
    config_mock = MagicMock()
    config_mock.deck_hash = "hash123"
    subscription = {"deck_hash": "hash123", "deck": {}}

    with patch("anki_import_diff.src.ankicollab_hook.is_ankicollab_diff_enabled", return_value=True), \
         patch("anki_import_diff.src.ankicollab_hook.capture_import_changes", return_value=[fake_diff]) as mock_capture, \
         patch("anki_import_diff.src.ankicollab_hook.open_browser_diff") as mock_open_browser:

        mock_im.install_update(subscription, input_hash=None, update_timestamp_after=True)
        res = mock_im._install_deck_op(deck_mock, config_mock)
        assert mock_capture.called
        assert isinstance(res, dict)
        assert res.get("_is_diff_run") is True
        assert res.get("diffs") == [fake_diff]

        mock_im._on_deck_installed(res, deck_mock, subscription, input_hash=None, update_timestamp_after=True)

        assert mock_open_browser.called
        called_diffs, on_commit_cb = mock_open_browser.call_args[0]
        assert called_diffs == [fake_diff]
        assert callable(on_commit_cb)


def test_ankicollab_commit_flow_finalizes_collab():
    """Tests that committing the diff triggers AnkiCollab finalization."""
    mock_im = _create_mock_collab_import_manager()
    orig_done_mock = MagicMock(return_value="finished")
    mock_im._on_deck_installed = orig_done_mock

    sys.modules["1957538407.import_manager"] = mock_im
    ack.setup_ankicollab_hook()

    fake_diff = NoteDiff(
        note_id=101,
        original_fields={"Recto": "Ancien texte"},
        updated_fields={"Recto": "Nouveau texte"},
        model_name="Basique",
        field_names=["Recto"],
        action="UPDATE",
    )

    deck_mock = MagicMock()
    config_mock = MagicMock()
    config_mock.deck_hash = "hash123"
    subscription = {"deck_hash": "hash123", "deck": {}}

    with patch("anki_import_diff.src.ankicollab_hook.is_ankicollab_diff_enabled", return_value=True), \
         patch("anki_import_diff.src.ankicollab_hook.capture_import_changes", return_value=[fake_diff]), \
         patch("anki_import_diff.src.ankicollab_hook.open_browser_diff") as mock_open_browser:

        mock_im.install_update(subscription, input_hash=None, update_timestamp_after=True)
        res = mock_im._install_deck_op(deck_mock, config_mock)
        mock_im._on_deck_installed(res, deck_mock, subscription, input_hash=None, update_timestamp_after=True)

        _, on_commit_cb = mock_open_browser.call_args[0]

        on_commit_cb([fake_diff])
        assert orig_done_mock.called


def test_ankicollab_bypassed_when_disabled():
    """Tests that updates proceed normally without diff when visual_diff is disabled."""
    mock_im = _create_mock_collab_import_manager()
    sys.modules["1957538407.import_manager"] = mock_im
    ack.setup_ankicollab_hook()

    deck_mock = MagicMock()
    config_mock = MagicMock()
    config_mock.deck_hash = "hash123"
    subscription = {"deck_hash": "hash123", "deck": {}}

    with patch("anki_import_diff.src.ankicollab_hook.is_ankicollab_diff_enabled", return_value=False), \
         patch("anki_import_diff.src.ankicollab_hook.capture_import_changes") as mock_capture:

        res = mock_im._install_deck_op(deck_mock, config_mock)
        assert not mock_capture.called
        assert isinstance(res, tuple)
        assert res[0] == 42


def test_ankicollab_bypassed_for_new_deck():
    """Tests that subscribing to a new deck (input_hash provided) bypasses the diff."""
    mock_im = _create_mock_collab_import_manager()
    sys.modules["1957538407.import_manager"] = mock_im
    ack.setup_ankicollab_hook()

    deck_mock = MagicMock()
    config_mock = MagicMock()
    config_mock.deck_hash = "new_hash"
    subscription = {"deck_hash": "new_hash", "deck": {}}

    with patch("anki_import_diff.src.ankicollab_hook.is_ankicollab_diff_enabled", return_value=True), \
         patch("anki_import_diff.src.ankicollab_hook.capture_import_changes") as mock_capture:

        # input_hash != None indicates brand new deck
        mock_im.install_update(subscription, input_hash="hash_token_123", update_timestamp_after=False)
        res = mock_im._install_deck_op(deck_mock, config_mock)

        assert not mock_capture.called
        assert isinstance(res, tuple)
        assert res[0] == 42


def test_ankicollab_no_diffs_proceeds_cleanly():
    """Tests that an update with 0 differences completes normally without showing the diff dialog."""
    mock_im = _create_mock_collab_import_manager()
    sys.modules["1957538407.import_manager"] = mock_im
    ack.setup_ankicollab_hook()

    deck_mock = MagicMock()
    config_mock = MagicMock()
    config_mock.deck_hash = "hash123"
    subscription = {"deck_hash": "hash123", "deck": {}}

    with patch("anki_import_diff.src.ankicollab_hook.is_ankicollab_diff_enabled", return_value=True), \
         patch("anki_import_diff.src.ankicollab_hook.capture_import_changes", return_value=[]), \
         patch("anki_import_diff.src.ankicollab_hook.open_browser_diff") as mock_open_browser:

        mock_im.install_update(subscription, input_hash=None, update_timestamp_after=True)
        res = mock_im._install_deck_op(deck_mock, config_mock)

        assert isinstance(res, tuple)
        assert not mock_open_browser.called


def test_setup_tools_menu_groups_diff_options():
    """Tests that setup_tools_menu creates a grouped 'Visual Diff' submenu."""
    from PyQt6.QtWidgets import QMenu, QApplication
    from PyQt6.QtGui import QAction

    app = QApplication.instance() or QApplication([])

    mock_mw = MagicMock()
    mock_tools_menu = QMenu("Outils")
    mock_mw.form.menuTools = mock_tools_menu
    mock_mw.addonManager.getConfig.return_value = {"visual_diff_enabled": True, "learned_cards_only": False}

    # Simulate an old legacy action in menuTools
    old_action = QAction("Visual Diff lors des imports & AnkiCollab", mock_tools_menu)
    mock_tools_menu.addAction(old_action)

    with patch("anki_import_diff.src.ankicollab_hook.mw", mock_mw):
        ack._menu_action_installed = False
        ack.setup_tools_menu()

    # The old action should have been removed
    assert old_action not in mock_tools_menu.actions()

    # There should be a submenu titled 'Visual Diff'
    diff_submenus = [a.menu() for a in mock_tools_menu.actions() if a.menu() and a.menu().title() == "Visual Diff"]
    assert len(diff_submenus) == 1
    diff_menu = diff_submenus[0]

    # Submenu should have 2 actions
    actions = diff_menu.actions()
    assert len(actions) == 2
    assert actions[0].text() == "Activer lors des imports et AnkiCollab"
    assert actions[0].isChecked() is True
    assert actions[1].text() == "Filtrer sur les cartes déjà apprises par défaut"
    assert actions[1].isChecked() is False


def test_open_browser_diff_deduplication_preserves_pending_diffs():
    """Vérifie qu'un double appel concurrent à open_browser_diff avec les mêmes diffs ne vide pas _pending_diffs."""
    from PyQt6.QtWidgets import QWidget
    from anki_import_diff.src.browser_integration import open_browser_diff, _pending_diffs, clear_pending_diffs
    import anki_import_diff.src.browser_integration as bi

    clear_pending_diffs()

    fake_diffs = [
        NoteDiff(note_id=100 + i, original_fields={"Texte": f"Old {i}"}, updated_fields={"Texte": f"New {i}"}, model_name="Basique", field_names=["Texte"], action="UPDATE")
        for i in range(5)
    ]

    mock_browser = MagicMock()
    mock_panel = MagicMock()
    mock_panel.isVisible.return_value = True
    mock_panel._browser = mock_browser

    with patch("aqt.dialogs.open", return_value=mock_browser), \
         patch("anki_import_diff.src.browser_integration.DiffBrowserPanel", return_value=mock_panel):
        
        # Premier appel : ouvre la session
        commit_cb1 = MagicMock()
        open_browser_diff(fake_diffs, commit_cb1)
        assert len(bi._pending_diffs) == 5
        assert set(bi._pending_diffs.keys()) == {100, 101, 102, 103, 104}

        # Simule le panel vivant
        bi._browser_panel = mock_panel

        # Second appel doublon (ex: 2e QueryOp AnkiCollab) avec les mêmes notes
        commit_cb2 = MagicMock()
        open_browser_diff(fake_diffs, commit_cb2)

        # _pending_diffs NE DOIT PAS avoir été vidé à 0 !
        assert len(bi._pending_diffs) == 5
        assert set(bi._pending_diffs.keys()) == {100, 101, 102, 103, 104}
        # Ne doit pas avoir appelé cleanup sur le panel actif
        assert not mock_panel.cleanup.called


def test_open_browser_diff_merges_multi_deck_diffs():
    """Vérifie que des diffs arrivant d'un second deck s'ajoutent à la session sans réinitialiser la première."""
    from anki_import_diff.src.browser_integration import open_browser_diff, clear_pending_diffs
    import anki_import_diff.src.browser_integration as bi

    clear_pending_diffs()

    deck1_diffs = [
        NoteDiff(note_id=101, original_fields={"Texte": "Old 1"}, updated_fields={"Texte": "New 1"}, model_name="Basique", field_names=["Texte"], action="UPDATE"),
        NoteDiff(note_id=102, original_fields={"Texte": "Old 2"}, updated_fields={"Texte": "New 2"}, model_name="Basique", field_names=["Texte"], action="UPDATE"),
    ]
    deck2_diffs = [
        NoteDiff(note_id=201, original_fields={"Texte": "Old 3"}, updated_fields={"Texte": "New 3"}, model_name="Basique", field_names=["Texte"], action="UPDATE"),
        NoteDiff(note_id=202, original_fields={"Texte": "Old 4"}, updated_fields={"Texte": "New 4"}, model_name="Basique", field_names=["Texte"], action="UPDATE"),
    ]

    mock_browser = MagicMock()
    mock_panel = MagicMock()
    mock_panel.isVisible.return_value = True
    mock_panel._browser = mock_browser

    with patch("aqt.dialogs.open", return_value=mock_browser), \
         patch("anki_import_diff.src.browser_integration.DiffBrowserPanel", return_value=mock_panel):
        
        cb1 = MagicMock()
        open_browser_diff(deck1_diffs, cb1)
        assert len(bi._pending_diffs) == 2

        bi._browser_panel = mock_panel

        cb2 = MagicMock()
        open_browser_diff(deck2_diffs, cb2)

        # Les deux decks doivent être fusionnés
        assert len(bi._pending_diffs) == 4
        assert 101 in bi._pending_diffs and 201 in bi._pending_diffs


def test_ankicollab_handle_pull_debounced():
    """Vérifie qu'un double appel rapide à handle_pull est ignoré."""
    mock_im = _create_mock_collab_import_manager()
    orig_pull_mock = MagicMock(return_value="pull_started")
    mock_im.handle_pull = orig_pull_mock

    sys.modules["1957538407.import_manager"] = mock_im
    ack.setup_ankicollab_hook()

    # Appel 1
    res1 = mock_im.handle_pull(None, silent=True)
    assert res1 == "pull_started"
    assert orig_pull_mock.call_count == 1

    # Appel 2 immédiat : doit être bloqué par le debounce
    res2 = mock_im.handle_pull(None, silent=True)
    assert res2 is None
    assert orig_pull_mock.call_count == 1


def _create_mock_collab_export_manager():
    mod = types.ModuleType("1957538407.export_manager")
    mod.suggest_notes = MagicMock(return_value="suggest_notes_ok")
    mod.suggest_subdeck = MagicMock(return_value="suggest_subdeck_ok")
    return mod


def _create_mock_collab_hooks():
    mod = types.ModuleType("1957538407.hooks")
    mod.suggest_notes = MagicMock(return_value="suggest_notes_ok")
    mod.suggest_subdeck = MagicMock(return_value="suggest_subdeck_ok")
    mod.bulk_suggest_handler = MagicMock(return_value="bulk_suggest_ok")
    return mod


def test_suggest_notes_allowed_when_no_diff_active():
    """Vérifie que la suggestion se déroule normalement quand aucune révision de diff n'est en cours."""
    mock_em = _create_mock_collab_export_manager()
    mock_hk = _create_mock_collab_hooks()
    sys.modules["1957538407.export_manager"] = mock_em
    sys.modules["1957538407.hooks"] = mock_hk

    ack.hook_ankicollab_suggestions()

    with patch("anki_import_diff.src.browser_integration.is_diff_session_active", return_value=False):
        res = mock_em.suggest_notes([101], 2)
        assert res == "suggest_notes_ok"
        assert mock_em.suggest_notes._orig_fn.called


def test_suggest_notes_blocked_when_diff_active_and_user_cancels():
    """Vérifie que la suggestion est bloquée si l'utilisateur choisit d'annuler ou de continuer sa révision."""
    mock_em = _create_mock_collab_export_manager()
    mock_hk = _create_mock_collab_hooks()
    sys.modules["1957538407.export_manager"] = mock_em
    sys.modules["1957538407.hooks"] = mock_hk

    ack.hook_ankicollab_suggestions()

    with patch("anki_import_diff.src.browser_integration.is_diff_session_active", return_value=True), \
         patch("anki_import_diff.src.ankicollab_hook.confirm_import_or_abort_suggestion", return_value=False) as mock_confirm:
        res = mock_em.suggest_notes([101], 2)
        assert res is None
        assert not mock_em.suggest_notes._orig_fn.called
        assert mock_confirm.called


def test_suggest_notes_allowed_when_user_confirms_import():
    """Vérifie que si l'utilisateur confirme l'importation, commit est appelé et la suggestion continue."""
    mock_em = _create_mock_collab_export_manager()
    mock_hk = _create_mock_collab_hooks()
    sys.modules["1957538407.export_manager"] = mock_em
    sys.modules["1957538407.hooks"] = mock_hk

    ack.hook_ankicollab_suggestions()

    mock_panel = MagicMock()

    with patch("anki_import_diff.src.browser_integration.is_diff_session_active", return_value=True), \
         patch("anki_import_diff.src.browser_integration.get_active_browser_panel", return_value=mock_panel), \
         patch("PyQt6.QtWidgets.QMessageBox") as MockMsgBox:

        mock_box = MockMsgBox.return_value
        fake_btn_confirm = MagicMock()
        mock_box.addButton.side_effect = [fake_btn_confirm, MagicMock(), MagicMock()]
        mock_box.clickedButton.return_value = fake_btn_confirm

        res = mock_em.suggest_notes([101], 2)
        assert res == "suggest_notes_ok"
        assert mock_panel.apply_commit.called
        assert mock_em.suggest_notes._orig_fn.called


def test_bulk_suggest_handler_blocked_when_diff_active():
    """Vérifie que le clic sur 'Bulk suggest' dans le menu contextuel est intercepté pendant un diff."""
    mock_em = _create_mock_collab_export_manager()
    mock_hk = _create_mock_collab_hooks()
    sys.modules["1957538407.export_manager"] = mock_em
    sys.modules["1957538407.hooks"] = mock_hk

    ack.hook_ankicollab_suggestions()

    with patch("anki_import_diff.src.browser_integration.is_diff_session_active", return_value=True), \
         patch("anki_import_diff.src.ankicollab_hook.confirm_import_or_abort_suggestion", return_value=False) as mock_confirm:
        browser_mock = MagicMock()
        res = mock_hk.bulk_suggest_handler(browser_mock, [101, 102])
        assert res is None
        assert not mock_hk.bulk_suggest_handler._orig_fn.called
        assert mock_confirm.called


def test_context_menu_tooltip_set_during_diff():
    """Vérifie que l'infobulle de sécurité est positionnée sur les actions AnkiCollab du menu contextuel pendant un diff."""
    from PyQt6.QtWidgets import QMenu, QApplication
    from PyQt6.QtGui import QAction
    import anki_import_diff.src.browser_integration as bi

    app = QApplication.instance() or QApplication([])

    browser_mock = MagicMock()
    browser_mock.selected_notes.return_value = [101]

    menu = QMenu()
    act_collab = QAction("AnkiCollab: Bulk suggest notes", menu)
    menu.addAction(act_collab)

    fake_diff = NoteDiff(note_id=101, original_fields={"F": "O"}, updated_fields={"F": "N"}, model_name="B", field_names=["F"])
    mock_panel = MagicMock()
    mock_panel._note_panels = {101: MagicMock()}
    mock_panel.isVisible.return_value = True

    with patch.object(bi, "_browser_panel", mock_panel), \
         patch.object(bi, "_pending_diffs", {101: fake_diff}):
        bi._on_browser_will_show_context_menu(browser_mock, menu)

    assert "en cours de révision" in act_collab.toolTip()

