"""
browser_integration.py — Intègre le diff dans l'explorateur et l'éditeur natifs d'Anki.

Architecture :
  - L'explorateur Anki est la fenêtre principale du workflow diff.
  - Un QDockWidget (DiffBrowserPanel) est injecté dans l'explorateur.
  - Le dock affiche les hunks de la note sélectionnée via les composants
    HunkWidget / FieldDiffPanel de ui.py (réutilisation complète).
  - Quand l'utilisateur accepte/rejette un hunk, l'éditeur natif d'Anki
    (déjà présent dans le browser) est mis à jour via JavaScript sans
    rechargement complet.
  - Les modifications manuelles de l'utilisateur dans l'éditeur sont
    capturées via editor_did_unfocus_field et propagées dans merged_fields.

Flux principal :
  Import détecté
  → capture_import_changes()  (savepoint dry-run)
  → open_browser_diff(diffs)
      → aqt.dialogs.open("Browser")
      → browser filtré sur les notes modifiées
      → DiffBrowserPanel (dock right/bottom)
          → Pour chaque note sélectionnée :
              · Affiche les champs et leurs hunks (HunkWidget)
              · Synchronise l'éditeur natif (injection JS)
          → Bouton "Commit All" → commit_changes()
"""

from __future__ import annotations

import os
import json
import html as _html
import re as _re
import threading
import time
import weakref
from datetime import datetime
from typing import Dict, List, Optional

import aqt
import aqt.gui_hooks as gui_hooks

try:
    from PyQt6 import QtCore, QtGui, QtWidgets
    _Qt = QtCore.Qt
    _AlignFlag = QtCore.Qt.AlignmentFlag
    _Signal = QtCore.pyqtSignal
except ImportError:
    from PySide6 import QtCore, QtGui, QtWidgets
    _Qt = QtCore.Qt
    _AlignFlag = QtCore.Qt.AlignmentFlag
    _Signal = QtCore.Signal

from .savepoint import NoteDiff, is_note_learned, get_learned_note_ids
from .diff import compute_hunks, apply_hunks, DiffHunk
from .ui import (
    HunkWidget, FieldDiffPanel, TagsDiffPanel, OldFieldsPanel, safe_timer_set_text,
    _get_style_btn_accept, _get_style_btn_reject, _get_style_btn_primary, _get_style_btn_secondary,
    _CLR_PANEL, _CLR_SIDEBAR, _CLR_BORDER, _CLR_HDR_BG, _CLR_HDR_FG,
    _CLR_TEXT, _CLR_SUBTEXT, _CLR_ADD_FG, _CLR_REM_FG, _CLR_ADD_BG, _CLR_REM_BG,
    _CLR_ADD_BDR, _CLR_REM_BDR, _CLR_SEL,
    _make_preview_widget, _set_preview_html, _get_model_css,
)
from .editor_sync import (
    sync_editor_from_hunks,
    sync_tags_to_editor,
    clear_field_highlights,
    highlight_field_accepted,
    highlight_field_rejected,
    make_field_edit_hook,
)


# ---------------------------------------------------------------------------
# État global : diffs en attente de validation
# ---------------------------------------------------------------------------

_pending_diffs: Dict[int, NoteDiff] = {}   # note_id → NoteDiff
_on_commit_cb = None                        # callable appelé au commit
_browser_panel: Optional["DiffBrowserPanel"] = None
_field_edit_hook = None                     # hook editor_did_unfocus_field
_committed = False
_staged_to_orig: Dict[int, int] = {}       # staged_note_id → original_note_id (pour INSERT pré-insérées)


def _get_editor_note(editor):
    """Extrait la note de l'editeur de facon securisee (Editor ou NewEditor)."""
    if not editor:
        return None
    note = getattr(editor, "note", None)
    if note is not None:
        return note
    nid = getattr(editor, "nid", None)
    if nid:
        try:
            if aqt.mw and aqt.mw.col:
                return aqt.mw.col.get_note(nid)
        except Exception:
            pass
    return None


def set_pending_diffs(diffs: List[NoteDiff], on_commit) -> None:
    """Enregistre les diffs à valider via le browser/éditeur natif."""
    global _pending_diffs, _on_commit_cb, _committed, _staged_to_orig
    _pending_diffs = {d.note_id: d for d in diffs}
    _on_commit_cb = on_commit
    _committed = False
    _staged_to_orig.clear()


def clear_pending_diffs() -> None:
    global _pending_diffs, _on_commit_cb, _browser_panel, _field_edit_hook, _staged_to_orig
    try:
        from .temp_link_interceptor import TempLinkInterceptor
        interceptor = TempLinkInterceptor.instance()
        if interceptor.is_active():
            interceptor.on_diff_session_closed(committed=_committed)
    except Exception:
        pass
    _pending_diffs.clear()
    _on_commit_cb = None
    _staged_to_orig.clear()

    # Retirer le hook d'unfocus si installé
    if _field_edit_hook is not None:
        try:
            gui_hooks.editor_did_unfocus_field.remove(_field_edit_hook)
        except (ValueError, Exception):
            pass
        _field_edit_hook = None

    _restore_editor_save()

    # Capturer la référence locale PUIS mettre le global à None EN PREMIER
    # pour éviter les appels récursifs (cleanup() rappelle clear_pending_diffs()).
    panel = _browser_panel
    _browser_panel = None
    if panel is not None:
        try:
            browser = getattr(panel, "_browser", None)
            if browser and getattr(browser, "editor", None) and getattr(browser.editor, "web", None):
                browser.editor.web.eval("window._anki_diff_active = false;")
        except Exception:
            pass
        try:
            panel.cleanup()
        except Exception:
            pass
        try:
            if panel.isVisible():
                panel.close()
        except Exception:
            pass


def get_diff_for_note(note_id: int) -> Optional[NoteDiff]:
    """Récupère le NoteDiff pour un note_id, en tenant compte des notes pré-insérées (staged)."""
    # Chercher par note_id original
    if note_id in _pending_diffs:
        return _pending_diffs[note_id]
    # Chercher par staged_note_id (notes INSERT pré-insérées)
    orig_id = _staged_to_orig.get(note_id)
    if orig_id is not None:
        return _pending_diffs.get(orig_id)
    return None


def _stage_insert_notes(diffs: List[NoteDiff]) -> List[int]:
    """
    Pré-insère les notes INSERT dans la collection Anki pour qu'elles soient
    visibles dans le browser avant le commit.

    Retourne la liste des IDs réels créés (staged_note_id).
    Ces notes sont supprimées si le panel est fermé sans commit (dans cleanup()).
    """
    global _staged_to_orig
    if not any(d.action == "INSERT" for d in diffs):
        return []
    col = getattr(aqt.mw, "col", None) if getattr(aqt, "mw", None) else None
    if not col:
        return []
    staged_nids = []

    for diff in diffs:
        if diff.action != "INSERT":
            continue
        try:
            # Trouver le modèle : par ID (fiable) puis par nom
            model = None
            if diff.model_id:
                try:
                    model = col.models.get(diff.model_id)
                except Exception:
                    pass
            if not model and hasattr(col.models, "by_name"):
                model = col.models.by_name(diff.model_name)
            if not model:
                print(f"[anki-diff] _stage_insert: model not found for note {diff.note_id}")
                continue

            # Créer la note avec les champs mis à jour
            note = col.new_note(model)
            if getattr(diff, "guid", ""):
                note.guid = diff.guid
            for idx, fname in enumerate(diff.field_names):
                val = diff.updated_fields.get(fname, "")
                if idx < len(note.fields):
                    note.fields[idx] = val
            note.tags = list(diff.tags)

            # Utiliser le deck capturé depuis le dry-run (ou deck sélectionné par défaut)
            deck_id = diff.deck_id or 1
            col.add_note(note, deck_id)

            # Enregistrer le mapping staged_id → original_id
            diff.staged_note_id = note.id
            _staged_to_orig[note.id] = diff.note_id
            staged_nids.append(note.id)
            print(f"[anki-diff] Staged INSERT: orig_id={diff.note_id} → staged_id={note.id} deck={deck_id}")

        except Exception as e:
            print(f"[anki-diff] Failed to stage INSERT note {diff.note_id}: {e}")

    if _staged_to_orig:
        try:
            from .temp_link_interceptor import start_diff_session
            start_diff_session(_staged_to_orig)
        except Exception as e_interceptor:
            print(f"[anki-diff] Error starting temp_link_interceptor session: {e_interceptor}")

    return staged_nids


def _unstage_insert_notes(diffs_dict: Dict[int, NoteDiff]) -> None:
    """Supprime les notes INSERT temporairement pré-insérées (appelé sur annulation)."""
    col = aqt.mw.col
    staged_ids = [
        diff.staged_note_id
        for diff in diffs_dict.values()
        if diff.action == "INSERT" and diff.staged_note_id
    ]
    if staged_ids:
        try:
            from .hooks import _safely_clear_editor_if_note_deleted
            _safely_clear_editor_if_note_deleted(staged_ids)
        except Exception:
            pass
        try:
            col.remove_notes(staged_ids)
            print(f"[anki-diff] Removed {len(staged_ids)} staged INSERT note(s) (cancelled)")
        except Exception as e:
            print(f"[anki-diff] Error removing staged notes: {e}")


def _get_dock_style() -> str:
    from .ui import get_colors
    c = get_colors()
    return f"""
QDockWidget, QTabWidget, QStackedWidget, QScrollArea, NoteDiffPanelWithSync {{
    background-color: {c['background']};
}}
QWidget {{
    color: {c['text_primary']};
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
    font-size: 13px;
}}
QDockWidget {{
    color: {c['text_primary']};
    font-weight: 700;
}}
QDockWidget::title {{
    background: {c['header']};
    padding: 4px 8px;
    border-bottom: 1px solid {c['border']};
}}
QLabel {{ background: transparent; }}
QFrame {{ border: none; }}
"""

# ---------------------------------------------------------------------------
# Persistance de l'état du browser (géométrie + splitters)
# ---------------------------------------------------------------------------

_BROWSER_STATE_KEY = "browser_diff_state"
_ADDON_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _get_layout_file_path() -> str:
    user_files_dir = os.path.join(_ADDON_DIR, "user_files")
    try:
        os.makedirs(user_files_dir, exist_ok=True)
    except Exception:
        pass
    return os.path.join(user_files_dir, "browser_diff_layout.json")


def _load_browser_layout_state() -> dict:
    state = {}
    try:
        if aqt.mw and getattr(aqt.mw, "addonManager", None):
            cfg = aqt.mw.addonManager.getConfig("anki_import_diff") or {}
            if "only_changed_fields" in cfg:
                state["only_changed_fields"] = cfg["only_changed_fields"]
            if "learned_cards_only" in cfg:
                state["learned_cards_only"] = cfg["learned_cards_only"]
            stored = cfg.get(_BROWSER_STATE_KEY)
            if isinstance(stored, dict) and stored:
                state.update(stored)
    except Exception:
        pass

    # Secours : charger depuis user_files/browser_diff_layout.json si vide ou incomplet
    if not state.get("main_splitter_sizes") and not state.get("editor_diff_sizes"):
        try:
            path = _get_layout_file_path()
            if os.path.exists(path):
                with open(path, "r", encoding="utf-8") as f:
                    file_state = json.load(f)
                    if isinstance(file_state, dict):
                        state.update(file_state)
        except Exception as e:
            print(f"[anki-diff] _load_browser_layout_state file error: {e}")

    return state


def _scale_sizes(saved_sizes: List[int], current_total: int) -> List[int]:
    if not saved_sizes or len(saved_sizes) != 2:
        return saved_sizes
    saved_sum = sum(saved_sizes)
    if saved_sum <= 0:
        return saved_sizes
    if current_total <= 50:
        return saved_sizes
    ratio = current_total / saved_sum
    s0 = max(50, int(saved_sizes[0] * ratio))
    s1 = max(50, current_total - s0)
    return [s0, s1]


def _save_browser_state(browser, editor_diff_splitter=None) -> None:
    """
    Sauvegarde l'état courant du browser dans la config de l'addon ET sur disque :
    - Géométrie de la fenêtre (position + taille)
    - Tailles et état du splitter principal (tableau / zone droite)
    - Tailles du sous-splitter éditeur / diff
    - Zoom de l'éditeur
    """
    try:
        from aqt.qt import sip
        if browser is None or sip.isdeleted(browser):
            return

        state = _load_browser_layout_state()

        # Géométrie de la fenêtre du browser
        try:
            geom = browser.saveGeometry()
            if geom:
                state["geometry"] = bytes(geom).hex()
        except Exception:
            pass

        # Splitter principal form.splitter (tableau / zone droite)
        try:
            form_splitter = getattr(getattr(browser, "form", None), "splitter", None)
            if form_splitter and not sip.isdeleted(form_splitter):
                splitter_state = form_splitter.saveState()
                if splitter_state:
                    state["main_splitter_state"] = bytes(splitter_state).hex()
                sizes = form_splitter.sizes()
                if sizes and len(sizes) == 2 and sum(sizes) > 0:
                    state["main_splitter_sizes"] = sizes
        except Exception:
            pass

        # Sous-splitter éditeur / diff
        if editor_diff_splitter is not None:
            try:
                if not sip.isdeleted(editor_diff_splitter):
                    ed_sizes = editor_diff_splitter.sizes()
                    if ed_sizes and len(ed_sizes) == 2 and sum(ed_sizes) > 0:
                        state["editor_diff_sizes"] = ed_sizes
            except Exception:
                pass

        # Zoom textuel de l'éditeur
        try:
            editor = getattr(browser, "editor", None)
            if editor:
                web = getattr(editor, "web", None)
                if web and hasattr(web, "zoomFactor"):
                    zf = web.zoomFactor()
                    if 0.5 <= zf <= 3.0:
                        state["editor_zoom"] = zf
        except Exception:
            pass

        # Mode de filtrage champs modifiés uniquement & cartes apprises
        global _browser_panel
        panel_to_check = _browser_panel or getattr(browser, "_diff_panel", None)
        if panel_to_check is not None and not sip.isdeleted(panel_to_check):
            if hasattr(panel_to_check, "_changed_only_cb") and panel_to_check._changed_only_cb is not None:
                try:
                    state["only_changed_fields"] = panel_to_check._changed_only_cb.isChecked()
                except Exception:
                    pass
            if hasattr(panel_to_check, "_btn_learned_only") and panel_to_check._btn_learned_only is not None:
                try:
                    state["learned_cards_only"] = panel_to_check._btn_learned_only.isChecked()
                except Exception:
                    pass

        if not state:
            return

        # 1. Sauvegarder dans addonManager
        try:
            if aqt.mw and getattr(aqt.mw, "addonManager", None):
                cfg = aqt.mw.addonManager.getConfig("anki_import_diff") or {}
                cfg[_BROWSER_STATE_KEY] = state
                aqt.mw.addonManager.writeConfig("anki_import_diff", cfg)
        except Exception as e:
            print(f"[anki-diff] addonManager.writeConfig error: {e}")

        # 2. Sauvegarder dans le fichier de configuration locale user_files
        try:
            path = _get_layout_file_path()
            with open(path, "w", encoding="utf-8") as f:
                json.dump(state, f, indent=2)
        except Exception as e:
            print(f"[anki-diff] save layout file error: {e}")

    except Exception as e:
        print(f"[anki-diff] _save_browser_state error: {e}")


def _restore_browser_state(browser, editor_diff_splitter=None, default_main_sizes=None) -> None:
    """
    Restaure l'état sauvegardé du browser (géométrie + splitters).
    Garantit des proportions cohérentes même en cas de premier lancement.
    """
    try:
        from aqt.qt import sip
        if browser is None or sip.isdeleted(browser):
            return

        state = _load_browser_layout_state()
        if not state:
            state = {
                "main_splitter_sizes": [260, 740],
                "editor_diff_sizes": [480, 260],
            }

        # 1. Géométrie de la fenêtre
        geom_hex = state.get("geometry")
        if geom_hex:
            try:
                from aqt.qt import QByteArray
                browser.restoreGeometry(QByteArray.fromHex(geom_hex.encode()))
            except Exception:
                pass

        # 2. Splitter principal form.splitter
        form_splitter = getattr(getattr(browser, "form", None), "splitter", None)
        if form_splitter and not sip.isdeleted(form_splitter):
            main_sizes = state.get("main_splitter_sizes")
            total_w = form_splitter.width()
            if main_sizes and len(main_sizes) == 2 and sum(main_sizes) > 0:
                target_sizes = _scale_sizes(main_sizes, total_w)
                form_splitter.setSizes(target_sizes)
            elif default_main_sizes and len(default_main_sizes) == 2 and sum(default_main_sizes) > 0:
                table_w = min(max(default_main_sizes[0], 240), 300)
                rest_w = max(400, (total_w - table_w) if total_w > table_w else 740)
                form_splitter.setSizes([table_w, rest_w])
            else:
                table_w = 260
                rest_w = max(400, (total_w - table_w) if total_w > table_w else 740)
                form_splitter.setSizes([table_w, rest_w])

        # 3. Sous-splitter éditeur / diff
        if editor_diff_splitter is not None and not sip.isdeleted(editor_diff_splitter):
            ed_sizes = state.get("editor_diff_sizes")
            ed_total_w = editor_diff_splitter.width()
            if ed_sizes and len(ed_sizes) == 2 and sum(ed_sizes) > 0:
                target_ed_sizes = _scale_sizes(ed_sizes, ed_total_w)
                editor_diff_splitter.setSizes(target_ed_sizes)
            else:
                w = ed_total_w if ed_total_w > 0 else 740
                editor_diff_splitter.setSizes([int(w * 0.67), max(100, w - int(w * 0.67))])

    except Exception as e:
        print(f"[anki-diff] _restore_browser_state error: {e}")



def _get_note_list_style() -> str:
    from .ui import get_colors
    c = get_colors()
    return f"""
QListWidget {{
    background-color: {c['surface']};
    border: none;
    border-right: 1px solid {c['border']};
    outline: none;
}}
QListWidget::item {{
    padding: 6px 10px;
    border-bottom: 1px solid {c['border']};
    color: {c['text_primary']};
}}
QListWidget::item:selected {{
    background-color: {c['sel']};
    color: {c['text_primary']};
}}
QListWidget::item:hover:!selected {{
    background-color: {c['surface_hover']};
}}
"""

def _get_style_btn_danger() -> str:
    from .ui import get_colors
    c = get_colors()
    return f"""
QPushButton {{
    background-color: {c['danger']};
    color: #ffffff;
    border: none;
    border-radius: 4px;
    padding: 6px 20px;
    font-size: 13px;
    font-weight: 600;
}}
QPushButton:hover {{
    background-color: {c['danger_hover']};
}}
"""


def _get_learned_btn_style(checked: bool) -> str:
    from .ui import get_colors
    c = get_colors()
    if checked:
        return f"""
QPushButton {{
    background-color: rgba(99, 102, 241, 0.15);
    color: #818cf8;
    border: 1px solid #6366f1;
    border-radius: 4px;
    padding: 3px 8px;
    font-size: 11px;
    font-weight: 600;
}}
QPushButton:hover {{
    background-color: rgba(99, 102, 241, 0.25);
    color: #a5b4fc;
}}
"""
    return f"""
QPushButton {{
    background-color: transparent;
    color: {c['text_secondary']};
    border: 1px solid {c['border']};
    border-radius: 4px;
    padding: 3px 8px;
    font-size: 11px;
    font-weight: 500;
}}
QPushButton:hover {{
    background-color: {c['surface_hover']};
    color: {c['text_primary']};
    border-color: {c['border']};
}}
"""


def _is_inside_field(panel: Optional["DiffBrowserPanel"], obj: Optional[QtCore.QObject] = None) -> bool:
    """
    Détermine si l'utilisateur est actuellement à l'intérieur d'un champ d'édition de note.
    Retourne False dès que l'événement cible le panneau de diff (menu diff), la table des cartes ou la barre latérale.
    Retourne True si l'événement cible un champ éditable de l'éditeur Anki ou un widget de saisie texte (QLineEdit/QTextEdit non-lecture seule).
    """
    if panel is None:
        return False

    browser = getattr(panel, "_browser", None)
    if browser is None:
        return False

    editor = getattr(browser, "editor", None)

    # Helper pour tester si un widget est ou appartient a la table des cartes
    def _is_table_widget(w: Optional[QtCore.QObject]) -> bool:
        if w is None or not isinstance(w, QtWidgets.QWidget):
            return False
        table = getattr(browser, "table", None)
        table_view = getattr(table, "_view", None) if table is not None else getattr(getattr(browser, "form", None), "tableView", None)
        if table_view is None and isinstance(table, QtWidgets.QAbstractItemView):
            table_view = table
        if table is not None and (w is table or (isinstance(table, QtWidgets.QWidget) and table.isAncestorOf(w))):
            return True
        if table_view is not None and (w is table_view or (isinstance(table_view, QtWidgets.QWidget) and table_view.isAncestorOf(w))):
            return True
        return False

    def _is_sidebar_widget(w: Optional[QtCore.QObject]) -> bool:
        if w is None or not isinstance(w, QtWidgets.QWidget):
            return False
        sidebar = getattr(browser, "sidebar", None)
        if sidebar is not None and (w is sidebar or (isinstance(sidebar, QtWidgets.QWidget) and sidebar.isAncestorOf(w))):
            return True
        return False

    # 1. Inspecter l'objet recepteur de l'evenement si fourni
    if obj is not None and isinstance(obj, QtWidgets.QWidget):
        try:
            # Si l'evenement vise le panneau diff ou l'un de ses widgets -> PAS dans un champ editable (False absolu)
            if obj is panel or panel.isAncestorOf(obj):
                return False

            # Si l'evenement vise la table des cartes ou la barre laterale -> False
            if _is_table_widget(obj) or _is_sidebar_widget(obj):
                return False

            # Si l'evenement vise un widget Qt standard de saisie (ex: recherche, tags)
            if isinstance(obj, (QtWidgets.QLineEdit, QtWidgets.QTextEdit, QtWidgets.QPlainTextEdit)):
                if not getattr(obj, "isReadOnly", lambda: False)():
                    return True

            # Si l'evenement vise le webview de l'editeur Anki
            web = getattr(editor, "web", None) if editor else None
            if web is not None and (obj is web or web.isAncestorOf(obj)):
                cf = getattr(editor, "currentField", None)
                return isinstance(cf, int)
        except Exception:
            pass

    # 2. Si aucun objet recepteur n'est fourni ou s'il n'a pas permis de trancher
    app = QtWidgets.QApplication.instance()
    focus_w = app.focusWidget() if app else None

    if focus_w is not None and isinstance(focus_w, QtWidgets.QWidget):
        try:
            if focus_w is panel or panel.isAncestorOf(focus_w):
                return False
            if _is_table_widget(focus_w) or _is_sidebar_widget(focus_w):
                return False
            if isinstance(focus_w, (QtWidgets.QLineEdit, QtWidgets.QTextEdit, QtWidgets.QPlainTextEdit)):
                if not getattr(focus_w, "isReadOnly", lambda: False)():
                    return True
            web = getattr(editor, "web", None) if editor else None
            if web is not None and (focus_w is web or web.isAncestorOf(focus_w)):
                cf = getattr(editor, "currentField", None)
                return isinstance(cf, int)
        except Exception:
            pass

    # 3. Vérifier l'état de l'éditeur Anki (fallback quand appelé directement sans obj)
    if editor is not None:
        cf = getattr(editor, "currentField", None)
        if isinstance(cf, int):
            return True

    return False


def _trigger_copy_nid(panel: Any, browser: Any) -> None:
    """Declenche la copie de NID 'Lier les cartes' (ou fallback) de maniere fiable."""
    # 1. Tenter via Anki_EDN_Lier_les_cartes
    try:
        from Anki_EDN_Lier_les_cartes.linked_cards import _copy_nid_smart, copy_nid_from_editor
        if browser:
            _copy_nid_smart(browser)
            return
        editor = getattr(panel, "_editor", None)
        if editor:
            copy_nid_from_editor(editor)
            return
    except Exception:
        pass

    # 2. Fallback direct si Lier les cartes n'est pas disponible
    try:
        from aqt.qt import QApplication, QMimeData
        editor = getattr(panel, "_editor", None) or (getattr(browser, "editor", None) if browser else None)
        note = _get_editor_note(editor) if editor else None
        nid = None
        if note and getattr(note, "id", None):
            nid = str(note.id)
        elif panel and getattr(panel, "_current_nid", None):
            nid = str(panel._current_nid)
        elif browser and hasattr(browser, "selected_cards") and browser.selected_cards():
            c = browser.col.get_card(browser.selected_cards()[0])
            nid = str(c.nid)
        if nid:
            try:
                from .temp_link_interceptor import TempLinkInterceptor
                TempLinkInterceptor.instance().register_copied_nid(nid)
            except Exception:
                pass
            mime = QMimeData()
            mime.setText(nid)
            app = QApplication.instance()
            if app and app.clipboard():
                app.clipboard().setMimeData(mime)
    except Exception:
        pass


class DiffGlobalEventFilter(QtCore.QObject):
    """
    Event filter global installe sur QApplication pour capturer F2, F3, Alt+A, Alt+R, Alt+V
    des ShortcutOverride et KeyPress, meme lorsque le focus est sur la tableView
    (qui intercepte F2 pour editer la cellule), ou la barre de menu
    (qui intercepte Alt+A/Alt+R pour les accelerateurs Windows).
    """
    def __init__(self, panel: "DiffBrowserPanel"):
        super().__init__()
        self._panel_ref = weakref.ref(panel)
        self._last_override_key = None
        self._last_override_time = 0.0

    def _should_handle_escape(self, panel, browser, obj) -> bool:
        """
        Vérifie si la touche Échap risque réellement de fermer l'explorateur / la revue de diff.
        Ne doit JAMAIS intercepter Échap si :
        - Un dialogue enfant (modal ou non modal, ex: LinkedCardsDialog) est actif
        - Un popup / menu / completer / preview est affiché
        - Le focus se trouve dans un widget secondaire ayant son propre cycle de fermeture
        """
        app = QtWidgets.QApplication.instance()
        if not app:
            return False

        # 1. Popups Qt actifs (menus déroulants, QCompleter, tooltips interactifs)
        if app.activePopupWidget() is not None:
            return False

        active_win = app.activeWindow()
        # 2. Si la fenêtre active est une QDialog (ex: dialogue de Lier les cartes, styles, etc.)
        #    même non-modale (comme LinkedCardsDialog), laisser le dialogue se fermer normalement
        if active_win is not None and active_win is not browser and active_win is not panel.window():
            if isinstance(active_win, QtWidgets.QDialog) or hasattr(active_win, "reject"):
                return False
            cls_name = active_win.__class__.__name__
            if "Dialog" in cls_name or cls_name in (
                "LinkedCardsDialog", "MirrorSelectionDialog", "InterLinkSelectionDialog",
                "CrossLinksSelectionDialog", "CardStylesDialog", "EDNSettingsDialog", "ShortcutsDialog"
            ):
                return False

        # 3. Vérifier également l'objet recevant l'événement
        if isinstance(obj, QtWidgets.QWidget):
            w = obj.window()
            if w is not None and w is not browser and w is not panel.window():
                if isinstance(w, QtWidgets.QDialog) or hasattr(w, "reject"):
                    return False
                cls_name = w.__class__.__name__
                if "Dialog" in cls_name:
                    return False

            # Popup d'aperçu d'assistant de Lier les cartes (EdnAssistantPreviewPopup)
            if obj.objectName() == "EdnAssistantPreviewPopup" or "PreviewPopup" in obj.metaObject().className():
                return False
            if hasattr(obj, "_edn_parent_widget") or getattr(obj, "_is_edn_preview", False):
                return False

        return True

    def eventFilter(self, obj: QtCore.QObject, event: QtCore.QEvent) -> bool:
        ev_type = event.type()
        if ev_type not in (QtCore.QEvent.Type.ShortcutOverride, QtCore.QEvent.Type.KeyPress):
            return False

        panel = self._panel_ref()
        if panel is None:
            return False

        global _pending_diffs
        if not _pending_diffs:
            return False

        browser = getattr(panel, "_browser", None)
        if browser is None:
            return False

        try:
            from aqt.qt import sip
            if sip.isdeleted(panel) or sip.isdeleted(browser):
                return False
        except Exception:
            return False

        app = QtWidgets.QApplication.instance()
        if app is None:
            return False

        # Verifier si l'evenement appartient a l'explorateur (ou a ses fenetres/widgets enfants)
        is_in_browser = False
        if isinstance(obj, QtWidgets.QWidget):
            try:
                w = obj.window()
                if w is browser or w is panel.window() or browser.isAncestorOf(obj) or panel.isAncestorOf(obj):
                    is_in_browser = True
            except Exception:
                pass

        if not is_in_browser and isinstance(obj, QtGui.QWindow):
            try:
                bw = browser.windowHandle()
                if bw is not None and (obj is bw or bw.isAncestorOf(obj)):
                    is_in_browser = True
            except Exception:
                pass

        if not is_in_browser:
            active_win = app.activeWindow()
            if active_win is not None:
                if (active_win is browser or
                        browser.isAncestorOf(active_win) or
                        active_win is panel.window() or
                        panel.isAncestorOf(active_win)):
                    is_in_browser = True

        if not is_in_browser:
            try:
                if getattr(browser, "isActiveWindow", lambda: False)():
                    is_in_browser = True
            except Exception:
                pass

        # Vérifier explicitement le panneau latéral des liens et autres docks
        if not is_in_browser:
            try:
                assistant_dock = getattr(browser, "_edn_assistant_dock", None)
                if assistant_dock is not None:
                    if isinstance(obj, QtWidgets.QWidget) and (obj is assistant_dock or assistant_dock.isAncestorOf(obj) or obj.window() is assistant_dock):
                        is_in_browser = True
            except Exception:
                pass

        if not is_in_browser:
            try:
                cursor_pos = QtGui.QCursor.pos()
                widget_under_mouse = app.widgetAt(cursor_pos)
                if widget_under_mouse is not None and (
                    browser is widget_under_mouse or browser.isAncestorOf(widget_under_mouse) or
                    panel is widget_under_mouse or panel.isAncestorOf(widget_under_mouse)
                ):
                    is_in_browser = True
            except Exception:
                pass

        if not is_in_browser:
            return False

        key = event.key()
        mods = event.modifiers()
        has_alt = bool(mods & QtCore.Qt.KeyboardModifier.AltModifier)
        has_ctrl = bool(mods & QtCore.Qt.KeyboardModifier.ControlModifier)
        has_shift = bool(mods & QtCore.Qt.KeyboardModifier.ShiftModifier)

        # Touches F2 et F3 directes (sans modificateurs)
        is_f2 = (key == QtCore.Qt.Key.Key_F2 and not (has_alt or has_ctrl or has_shift))
        is_f3 = (key == QtCore.Qt.Key.Key_F3 and not (has_alt or has_ctrl or has_shift))
        is_f4 = (key == QtCore.Qt.Key.Key_F4 and not (has_alt or has_ctrl or has_shift))

        # Raccourci Echap : fermer la revue de diff avec confirmation
        # STRICTEMENT si la page d'explorateur risque d'être réellement fermée,
        # et JAMAIS si un dialogue (modal ou non modal, ex: LinkedCardsDialog),
        # un menu contextuel, un popup d'aperçu ou une sous-fenêtre est actif.
        is_escape = False
        if key == QtCore.Qt.Key.Key_Escape and not (has_alt or has_ctrl or has_shift):
            is_escape = self._should_handle_escape(panel, browser, obj)

        # Flèches de déplacement vers le haut / le bas (sans modificateurs)
        is_up = (key == QtCore.Qt.Key.Key_Up and not (has_alt or has_ctrl or has_shift))
        is_down = (key == QtCore.Qt.Key.Key_Down and not (has_alt or has_ctrl or has_shift))

        # Raccourcis Accepter : F2, Alt+A, Alt+V, Alt+Y, Ctrl+Alt+A, Ctrl+Shift+A
        is_accept = (
            is_f2
            or (has_alt and not has_ctrl and not has_shift and key in (QtCore.Qt.Key.Key_A, QtCore.Qt.Key.Key_V, QtCore.Qt.Key.Key_Y))
            or (has_ctrl and has_alt and not has_shift and key == QtCore.Qt.Key.Key_A)
            or (has_ctrl and has_shift and not has_alt and key == QtCore.Qt.Key.Key_A)
        )

        # Raccourcis Rejeter : F3, Alt+R, Alt+N, Ctrl+Alt+R, Ctrl+Shift+R
        is_reject = (
            is_f3
            or (has_alt and not has_ctrl and not has_shift and key in (QtCore.Qt.Key.Key_R, QtCore.Qt.Key.Key_N))
            or (has_ctrl and has_alt and not has_shift and key == QtCore.Qt.Key.Key_R)
            or (has_ctrl and has_shift and not has_alt and key == QtCore.Qt.Key.Key_R)
        )

        # Raccourcis Accepter uniquement les tags : F4, Alt+Shift+T, Ctrl+Alt+Shift+T
        is_accept_tags = (
            is_f4
            or (has_alt and has_shift and not has_ctrl and key == QtCore.Qt.Key.Key_T)
            or (has_ctrl and has_alt and has_shift and key == QtCore.Qt.Key.Key_T)
        )

        # Raccourcis Historique : Alt+H, Ctrl+Shift+H
        is_history = (
            (has_alt and not has_ctrl and not has_shift and key == QtCore.Qt.Key.Key_H)
            or (has_ctrl and has_shift and not has_alt and key == QtCore.Qt.Key.Key_H)
        )

        # Raccourcis Copier anciens champs : Alt+C, Ctrl+Shift+C
        is_copy = (
            (has_alt and not has_ctrl and not has_shift and key == QtCore.Qt.Key.Key_C)
            or (has_ctrl and has_shift and not has_alt and key == QtCore.Qt.Key.Key_C)
        )

        # Raccourci Copier NID (Lier les cartes) : Ctrl+Alt+C
        is_copy_nid = (
            has_ctrl and has_alt and not has_shift and key == QtCore.Qt.Key.Key_C
        )

        if not (is_accept or is_reject or is_accept_tags or is_history or is_copy or is_copy_nid or is_escape or is_up or is_down):
            return False

        # F2, F3, F4 et Escape sont des touches fonctionnelles dédiées : elles doivent TOUJOURS fonctionner
        # peu importe la cible ou le focus de l'utilisateur (parcourir, éditeur, champ texte, menu latéral liens, menu latéral diff...).
        # Seuls les raccourcis textuels basés sur des lettres (Alt+A, Alt+R, Alt+C, etc.) ainsi que les flèches haut/bas
        # sont ignorés quand l'utilisateur est dans un champ de saisie texte (pour permettre l'édition et la navigation dans le texte).
        if not (is_f2 or is_f3 or is_f4 or is_escape):
            if _is_inside_field(panel, obj):
                return False

        # Si le focus ou la cible de l'evenement est directement sur la table des cartes (tableView),
        # laisser le comportement natif de la table pour les touches flechees (eviter tout conflit ou double saut)
        if is_up or is_down:
            table = getattr(browser, "table", None)
            table_view = getattr(table, "_view", None) if table is not None else None
            if table_view is None and hasattr(browser, "form") and hasattr(browser.form, "tableView"):
                table_view = browser.form.tableView
            if table_view is None and isinstance(table, QtWidgets.QAbstractItemView):
                table_view = table

            focus_w = app.focusWidget()
            is_table_event = False
            for w in (obj, focus_w):
                if w is not None and isinstance(w, QtWidgets.QWidget):
                    if table is not None and (w is table or (isinstance(table, QtWidgets.QWidget) and table.isAncestorOf(w))):
                        is_table_event = True
                        break
                    if table_view is not None and (w is table_view or (isinstance(table_view, QtWidgets.QWidget) and table_view.isAncestorOf(w))):
                        is_table_event = True
                        break
            if is_table_event:
                return False

        now = time.monotonic()
        if ev_type == QtCore.QEvent.Type.ShortcutOverride:
            self._last_override_key = key
            self._last_override_time = now
            if is_accept:
                panel._accept_all_current()
            elif is_reject:
                panel._reject_all_current()
            elif is_accept_tags:
                panel._accept_tags_only_current()
            elif is_history:
                panel._toggle_history_tab()
            elif is_copy:
                panel._copy_current_note_old_fields()
            elif is_copy_nid:
                _trigger_copy_nid(panel, browser)
            elif is_escape:
                # IMPORTANT : Ne JAMAIS ouvrir de dialogue modal (prompt_diff_close_decision)
                # lors de ShortcutOverride ! Cela bloque la boucle Qt et provoque un double déclenchement
                # sur l'événement KeyPress consécutif. On accepte le ShortcutOverride pour empêcher
                # la fermeture brutale par Anki natif, et l'action s'exécute uniquement sur KeyPress.
                event.accept()
                return True
            elif is_up:
                panel._select_previous_card()
            elif is_down:
                panel._select_next_card()
            event.accept()
            return True
        else:
            # KeyPress
            if self._last_override_key == key and not is_escape and (now - self._last_override_time) < 0.15:
                self._last_override_key = None
                event.accept()
                return True
            self._last_override_key = None
            if is_accept:
                panel._accept_all_current()
            elif is_reject:
                panel._reject_all_current()
            elif is_accept_tags:
                panel._accept_tags_only_current()
            elif is_history:
                panel._toggle_history_tab()
            elif is_copy:
                panel._copy_current_note_old_fields()
            elif is_copy_nid:
                _trigger_copy_nid(panel, browser)
            elif is_escape:
                panel._on_request_close_panel()
            elif is_up:
                panel._select_previous_card()
            elif is_down:
                panel._select_next_card()
            event.accept()
            return True


# ---------------------------------------------------------------------------
# DiffBrowserPanel — Panneau injecte dans le splitter de l'explorateur
# ---------------------------------------------------------------------------

class _LazyNotePanelsDict(dict):
    """Dictionnaire à instanciation paresseuse pour les panels NoteDiffPanelWithSync."""
    def __init__(self, panel_container):
        super().__init__()
        self._panel_container = panel_container

    def __missing__(self, nid):
        res = self._panel_container._get_or_create_panel(nid)
        if res is not None:
            return res
        raise KeyError(nid)

    def __getitem__(self, nid):
        if nid in self:
            return super().__getitem__(nid)
        return self.__missing__(nid)

    def get(self, nid, default=None):
        if nid in self:
            return super().get(nid)
        try:
            res = self._panel_container._get_or_create_panel(nid)
            if res is not None:
                return res
        except Exception:
            pass
        return default

class DiffBrowserPanel(QtWidgets.QWidget):
    """
    Panel de diff docké dans l'explorateur Anki.
    """

    def __init__(self, browser):
        super().__init__(browser)
        self.setObjectName("diffBrowserPanel")
        self._browser = browser
        if browser is not None:
            browser._diff_panel = self
        self._current_nid: Optional[int] = None
        self._last_diff_nid: Optional[int] = None
        self._current_editor = None  # reference a l'editeur actif dans le browser
        self._note_panels: Dict[int, "NoteDiffPanelWithSync"] = _LazyNotePanelsDict(self)  # note_id -> panel
        self._editor_diff_splitter: Optional[QtWidgets.QSplitter] = None  # splitter injecte
        self._original_splitter_widget: Optional[QtWidgets.QWidget] = None  # fieldsArea original
        self._pre_diff_main_splitter_sizes: Optional[List[int]] = None
        self._pre_diff_main_splitter_state: Optional[bytes] = None
        self._save_timer: Optional[QtCore.QTimer] = None
        self._event_filter: Optional[DiffGlobalEventFilter] = None
        layout_state = _load_browser_layout_state()
        self._only_changed_fields: bool = layout_state.get("only_changed_fields", True)
        self._changed_only_cb: Optional[QtWidgets.QCheckBox] = None
        self._learned_cards_only: bool = layout_state.get("learned_cards_only", False)
        self._btn_learned_only: Optional[QtWidgets.QPushButton] = None

        self.setSizePolicy(
            QtWidgets.QSizePolicy.Policy.Ignored,
            QtWidgets.QSizePolicy.Policy.Preferred,
        )
        self.setStyleSheet(_get_dock_style())
        self._build()
        self._shortcuts: List[QtGui.QShortcut] = []
        self._setup_shortcuts()
        self._inject_into_browser_splitter()

        # Hooks
        gui_hooks.browser_did_change_row.append(self._on_browser_row_changed)
        self._install_field_edit_hook()
        _patch_editor_save_for_diff()
        if self._learned_cards_only:
            self._apply_learned_cards_filter(True)

    def _inject_into_browser_splitter(self) -> None:
        """
        Injecte le panel diff dans form.splitter[1] du browser en créant
        un QSplitter horizontal :
          form.splitter[1] = QSplitter(H) [ fieldsArea | DiffBrowserPanel ]
        """
        try:
            form_splitter = self._browser.form.splitter
            # Enregistrer l'état et tailles du splitter AVANT injection
            self._pre_diff_main_splitter_sizes = form_splitter.sizes()
            pre_state = form_splitter.saveState()
            self._pre_diff_main_splitter_state = bytes(pre_state) if pre_state else None

            # Récupérer le widget actuel à l'index 1 (fieldsArea)
            original_widget = form_splitter.widget(1)
            if original_widget is None:
                return
            self._original_splitter_widget = original_widget

            # Créer le sous-splitter horizontal
            editor_diff_splitter = QtWidgets.QSplitter(_Qt.Orientation.Horizontal)
            editor_diff_splitter.setObjectName("editorDiffSplitter")
            editor_diff_splitter.setChildrenCollapsible(False)
            editor_diff_splitter.setHandleWidth(4)
            editor_diff_splitter.setStyleSheet(
                "QSplitter::handle { background: %s; }" % _CLR_BORDER
            )

            # addWidget() sur le sous-splitter re-parent automatiquement original_widget,
            # ce qui le retire de form_splitter (form_splitter passe de count=2 à count=1).
            editor_diff_splitter.addWidget(original_widget)  # index 0 dans sous-splitter
            editor_diff_splitter.addWidget(self)             # index 1 dans sous-splitter

            # Insérer notre sous-splitter à index 1
            form_splitter.insertWidget(1, editor_diff_splitter)
            self._editor_diff_splitter = editor_diff_splitter

            # Connecter splitterMoved sur les deux splitters pour enregistrer en temps réel
            # les ajustements de l'utilisateur
            try:
                form_splitter.splitterMoved.connect(self._on_splitter_moved)
                editor_diff_splitter.splitterMoved.connect(self._on_splitter_moved)
            except Exception:
                pass

            # Application de la disposition préférée (Screen 1)
            def _apply_current_state():
                try:
                    from aqt.qt import sip
                    if not sip.isdeleted(self) and self._browser and not sip.isdeleted(self._browser):
                        _restore_browser_state(
                            self._browser,
                            editor_diff_splitter,
                            default_main_sizes=self._pre_diff_main_splitter_sizes
                        )
                except Exception:
                    pass

            # Application immédiate et après chaque passe de rendu Qt
            _apply_current_state()
            QtCore.QTimer.singleShot(0, _apply_current_state)
            QtCore.QTimer.singleShot(50, _apply_current_state)
            QtCore.QTimer.singleShot(150, _apply_current_state)

        except Exception as e:
            import traceback
            print(f"[anki-diff] _inject_into_browser_splitter failed: {e}")
            traceback.print_exc()
            # En cas d'échec, afficher comme QWidget flottant
            self.setWindowTitle("Diff Review")
            self.setWindowFlags(_Qt.WindowType.Window)
            self.resize(420, 700)
            self.show()

    def _on_splitter_moved(self, pos: int = 0, index: int = 0) -> None:
        """Enregistre automatiquement les nouvelles proportions quand l'utilisateur déplace un volet."""
        if self._save_timer is None:
            self._save_timer = QtCore.QTimer(self)
            self._save_timer.setSingleShot(True)
            self._save_timer.setInterval(300)
            self._save_timer.timeout.connect(self._save_current_layout)
        self._save_timer.start()

    def _on_changed_only_toggled(self, checked: bool) -> None:
        self._only_changed_fields = checked
        for p in list(self._note_panels.values()):
            if p is not None and hasattr(p, "set_only_changed_filter"):
                p.set_only_changed_filter(checked)
        self._save_current_layout()

    def _update_learned_btn_style(self) -> None:
        if hasattr(self, "_btn_learned_only") and self._btn_learned_only is not None:
            self._btn_learned_only.setStyleSheet(_get_learned_btn_style(self._btn_learned_only.isChecked()))

    def _on_learned_only_toggled(self, checked: bool) -> None:
        self._learned_cards_only = checked
        self._update_learned_btn_style()
        self._apply_learned_cards_filter(checked)
        self._save_current_layout()

    def get_learned_diff_nids(self) -> List[int]:
        """Retourne la liste ordonnée des IDs de notes du diff possédant au moins une carte apprise."""
        col = getattr(self._browser, "col", None) or getattr(aqt.mw, "col", None)
        update_nids = [d.note_id for d in _pending_diffs.values() if d.action != "INSERT"]
        if not update_nids:
            return []
        learned_set = get_learned_note_ids(update_nids, col)
        return [nid for nid in update_nids if nid in learned_set]

    def _apply_learned_cards_filter(self, checked: bool) -> None:
        if not _pending_diffs:
            return

        if checked:
            learned_nids = self.get_learned_diff_nids()
            if learned_nids:
                search_str = "nid:" + ",".join(str(nid) for nid in learned_nids)
                if self._browser and hasattr(self._browser, "search_for"):
                    self._browser.search_for(search_str)

                target_sel = self._current_nid if self._current_nid in learned_nids else learned_nids[0]
                self.select_note(target_sel)

                col = getattr(self._browser, "col", None) or getattr(aqt.mw, "col", None)
                if col and hasattr(self._browser, "table") and self._browser.table is not None:
                    card_ids = []
                    try:
                        if hasattr(col, "card_ids_of_note"):
                            card_ids = col.card_ids_of_note(target_sel)
                        else:
                            n = col.get_note(target_sel)
                            if n:
                                card_ids = n.card_ids()
                    except Exception:
                        pass
                    if card_ids and hasattr(self._browser.table, "select_single_card"):
                        self._browser.table.select_single_card(card_ids[0])
            else:
                if self._browser and hasattr(self._browser, "search_for"):
                    self._browser.search_for("nid:0")
                if hasattr(self, "_no_learned_widget") and self._no_learned_widget is not None:
                    self._diff_stack.setCurrentWidget(self._no_learned_widget)
        else:
            self._back_to_diff_notes()

        self._update_status()

    def _create_no_learned_widget(self) -> QtWidgets.QWidget:
        w = QtWidgets.QWidget()
        w.setStyleSheet(f"background-color:{_CLR_PANEL};")
        layout = QtWidgets.QVBoxLayout(w)
        layout.setContentsMargins(20, 24, 20, 20)
        layout.setSpacing(10)

        title = QtWidgets.QLabel("Aucune carte apprise modifiée", w)
        title.setStyleSheet(f"color:{_CLR_TEXT}; font-size:13px; font-weight:700;")
        title.setWordWrap(True)
        layout.addWidget(title)

        badge = QtWidgets.QLabel("0 carte apprise parmi les modifications", w)
        badge.setStyleSheet(
            f"color:{_CLR_SUBTEXT}; font-size:11px; font-weight:600; background:{_CLR_HDR_BG}; "
            f"border:1px solid {_CLR_BORDER}; border-radius:10px; padding:3px 10px;"
        )
        layout.addWidget(badge)

        desc = QtWidgets.QLabel(
            "Toutes les modifications en attente concernent de nouvelles cartes ou des cartes non encore révisées.",
            w,
        )
        desc.setStyleSheet(f"color:{_CLR_SUBTEXT}; font-size:12px; line-height:1.45;")
        desc.setWordWrap(True)
        layout.addWidget(desc)

        layout.addSpacing(8)

        btn_all = QtWidgets.QPushButton("Afficher toutes les modifications", w)
        btn_all.setCursor(QtGui.QCursor(_Qt.CursorShape.PointingHandCursor))
        btn_all.setStyleSheet(_get_style_btn_primary())
        btn_all.setMinimumHeight(28)
        btn_all.clicked.connect(lambda: self._btn_learned_only.setChecked(False) if getattr(self, "_btn_learned_only", None) else None)
        layout.addWidget(btn_all)

        layout.addStretch()
        return w

    def _save_current_layout(self) -> None:
        try:
            from aqt.qt import sip
            if not sip.isdeleted(self) and self._browser and not sip.isdeleted(self._browser):
                _save_browser_state(self._browser, self._editor_diff_splitter)
        except Exception:
            pass


    def _setup_shortcuts(self) -> None:
        # Installer l'event filter global pour capturer les touches dès ShortcutOverride et KeyPress
        # même si le focus est dans la tableView (F2) ou sur la barre de menu (Alt+A, Alt+R)
        if not hasattr(self, "_event_filter") or self._event_filter is None:
            self._event_filter = DiffGlobalEventFilter(self)
            app = QtWidgets.QApplication.instance()
            if app:
                app.installEventFilter(self._event_filter)

        # Attacher les QShortcut uniquement à self (panel) avec WidgetWithChildrenShortcut
        # pour éviter les doublons ambigus sur le browser qui bloqueraient les raccourcis
        # Accepter tous les champs : F2, Alt+A, Alt+V, Ctrl+Alt+A, Alt+Y, Ctrl+Shift+A
        for key in ("F2", "Alt+A", "Alt+V", "Ctrl+Alt+A", "Alt+Y", "Ctrl+Shift+A"):
            try:
                sc = QtGui.QShortcut(QtGui.QKeySequence(key), self)
                sc.setContext(_Qt.ShortcutContext.WidgetWithChildrenShortcut)
                sc.activated.connect(self._accept_all_current)
                self._shortcuts.append(sc)
            except Exception:
                pass

        # Rejeter tous les champs : F3, Alt+R, Ctrl+Alt+R, Alt+N, Ctrl+Shift+R
        for key in ("F3", "Alt+R", "Ctrl+Alt+R", "Alt+N", "Ctrl+Shift+R"):
            try:
                sc = QtGui.QShortcut(QtGui.QKeySequence(key), self)
                sc.setContext(_Qt.ShortcutContext.WidgetWithChildrenShortcut)
                sc.activated.connect(self._reject_all_current)
                self._shortcuts.append(sc)
            except Exception:
                pass

        # Accepter uniquement les tags : F4, Alt+Shift+T, Ctrl+Alt+Shift+T
        for key in ("F4", "Alt+Shift+T", "Ctrl+Alt+Shift+T"):
            try:
                sc = QtGui.QShortcut(QtGui.QKeySequence(key), self)
                sc.setContext(_Qt.ShortcutContext.WidgetWithChildrenShortcut)
                sc.activated.connect(self._accept_tags_only_current)
                self._shortcuts.append(sc)
            except Exception:
                pass

        # Basculer vers l'historique : Alt+H et Ctrl+Shift+H
        for key in ("Alt+H", "Ctrl+Shift+H"):
            try:
                sc = QtGui.QShortcut(QtGui.QKeySequence(key), self)
                sc.setContext(_Qt.ShortcutContext.WidgetWithChildrenShortcut)
                sc.activated.connect(self._toggle_history_tab)
                self._shortcuts.append(sc)
            except Exception:
                pass

        # Copier tous les anciens champs de la note en cours : Alt+C et Ctrl+Shift+C
        for key in ("Alt+C", "Ctrl+Shift+C"):
            try:
                sc = QtGui.QShortcut(QtGui.QKeySequence(key), self)
                sc.setContext(_Qt.ShortcutContext.WidgetWithChildrenShortcut)
                sc.activated.connect(self._copy_current_note_old_fields)
                self._shortcuts.append(sc)
            except Exception:
                pass

        # Fermer la revue de diff avec confirmation via Échap
        try:
            sc = QtGui.QShortcut(QtGui.QKeySequence(QtCore.Qt.Key.Key_Escape), self)
            sc.setContext(_Qt.ShortcutContext.WidgetWithChildrenShortcut)
            sc.activated.connect(self._on_request_close_panel)
            self._shortcuts.append(sc)
        except Exception:
            pass

        # Navigation de carte via les fleches haut / bas
        for key, callback in (
            (QtCore.Qt.Key.Key_Up, self._select_previous_card),
            (QtCore.Qt.Key.Key_Down, self._select_next_card),
        ):
            try:
                sc = QtGui.QShortcut(QtGui.QKeySequence(key), self)
                sc.setContext(_Qt.ShortcutContext.WidgetWithChildrenShortcut)
                sc.activated.connect(callback)
                self._shortcuts.append(sc)
            except Exception:
                pass

        # Si l'editeur du navigateur existe deja, injecter les raccourcis dans sa webview
        editor = getattr(self._browser, "editor", None)
        if editor:
            _inject_editor_web_shortcuts(editor)

    def _select_previous_card(self) -> None:
        """Sélectionne la carte précédente (au-dessus) dans l'explorateur Anki."""
        self._navigate_card_relative(-1)

    def _select_next_card(self) -> None:
        """Sélectionne la carte suivante (en-dessous) dans l'explorateur Anki."""
        self._navigate_card_relative(1)

    def keyPressEvent(self, event: QtGui.QKeyEvent) -> None:
        key = event.key()
        mods = event.modifiers()
        if not (mods & (_Qt.KeyboardModifier.ControlModifier | _Qt.KeyboardModifier.AltModifier | _Qt.KeyboardModifier.ShiftModifier)):
            if key == _Qt.Key.Key_Up:
                self._select_previous_card()
                event.accept()
                return
            elif key == _Qt.Key.Key_Down:
                self._select_next_card()
                event.accept()
                return
        super().keyPressEvent(event)

    def _navigate_card_relative(self, delta: int) -> None:
        """
        Déplace la sélection d'une ligne vers le haut (delta = -1) ou vers le bas (delta = 1)
        dans la table de l'explorateur Anki et met à jour l'affichage du diff.
        """
        now = time.monotonic()
        if hasattr(self, "_last_nav_time") and (now - self._last_nav_time) < 0.08:
            return
        self._last_nav_time = now

        browser = getattr(self, "_browser", None)
        if browser is None:
            return

        try:
            from aqt.qt import sip
            if sip.isdeleted(browser):
                return
        except Exception:
            return

        table = getattr(browser, "table", None)
        table_view = getattr(table, "_view", None) if table is not None else None
        if table_view is None and hasattr(browser, "form") and hasattr(browser.form, "tableView"):
            table_view = browser.form.tableView

        # 1. Utiliser en priorité l'API officielle table.to_previous_row / table.to_next_row
        if table is not None:
            try:
                has_curr = False
                if hasattr(table, "has_current"):
                    has_curr = table.has_current()
                elif table_view is not None and table_view.selectionModel() is not None:
                    has_curr = table_view.selectionModel().currentIndex().isValid()

                if not has_curr:
                    if table_view is not None:
                        sm = table_view.selectionModel()
                        m = table_view.model()
                        if sm is not None and m is not None and m.rowCount() > 0:
                            target_row = 0 if delta > 0 else m.rowCount() - 1
                            new_idx = m.index(target_row, 0)
                            table_view.setCurrentIndex(new_idx)
                            sm.select(
                                new_idx,
                                QtCore.QItemSelectionModel.SelectionFlag.ClearAndSelect
                                | QtCore.QItemSelectionModel.SelectionFlag.Rows,
                            )
                            try:
                                table_view.scrollTo(new_idx, QtWidgets.QAbstractItemView.ScrollHint.EnsureVisible)
                            except Exception:
                                pass
                            if hasattr(self, "_on_browser_row_changed"):
                                self._on_browser_row_changed(browser)
                    elif delta > 0 and hasattr(table, "to_first_row"):
                        table.to_first_row()
                    elif delta < 0 and hasattr(table, "to_last_row"):
                        table.to_last_row()
                    return

                if delta < 0:
                    if hasattr(table, "has_previous") and table.has_previous():
                        table.to_previous_row()
                        if table_view is not None:
                            try:
                                table_view.scrollTo(table_view.currentIndex(), QtWidgets.QAbstractItemView.ScrollHint.EnsureVisible)
                            except Exception:
                                pass
                        if hasattr(self, "_on_browser_row_changed"):
                            self._on_browser_row_changed(browser)
                        return
                    elif not hasattr(table, "has_previous") and hasattr(table, "to_previous_row"):
                        table.to_previous_row()
                        if table_view is not None:
                            try:
                                table_view.scrollTo(table_view.currentIndex(), QtWidgets.QAbstractItemView.ScrollHint.EnsureVisible)
                            except Exception:
                                pass
                        if hasattr(self, "_on_browser_row_changed"):
                            self._on_browser_row_changed(browser)
                        return
                elif delta > 0:
                    if hasattr(table, "has_next") and table.has_next():
                        table.to_next_row()
                        if table_view is not None:
                            try:
                                table_view.scrollTo(table_view.currentIndex(), QtWidgets.QAbstractItemView.ScrollHint.EnsureVisible)
                            except Exception:
                                pass
                        if hasattr(self, "_on_browser_row_changed"):
                            self._on_browser_row_changed(browser)
                        return
                    elif not hasattr(table, "has_next") and hasattr(table, "to_next_row"):
                        table.to_next_row()
                        if table_view is not None:
                            try:
                                table_view.scrollTo(table_view.currentIndex(), QtWidgets.QAbstractItemView.ScrollHint.EnsureVisible)
                            except Exception:
                                pass
                        if hasattr(self, "_on_browser_row_changed"):
                            self._on_browser_row_changed(browser)
                        return
            except Exception as e:
                print(f"[anki-diff] _navigate_card_relative table error: {e}")

        # 2. Fallback direct via table_view (QTableView / QItemSelectionModel)
        if table_view is not None:
            try:
                sm = table_view.selectionModel()
                m = table_view.model()
                if sm is not None and m is not None:
                    cur = sm.currentIndex()
                    row_count = m.rowCount()
                    if row_count > 0:
                        cur_row = cur.row() if cur.isValid() else (-1 if delta > 0 else row_count)
                        target_row = max(0, min(row_count - 1, cur_row + delta))
                        if target_row != cur_row:
                            col_idx = cur.column() if cur.isValid() and cur.column() >= 0 else 0
                            new_idx = m.index(target_row, col_idx)
                            table_view.setCurrentIndex(new_idx)
                            sm.select(
                                new_idx,
                                QtCore.QItemSelectionModel.SelectionFlag.ClearAndSelect
                                | QtCore.QItemSelectionModel.SelectionFlag.Rows,
                            )
                            try:
                                table_view.scrollTo(new_idx, QtWidgets.QAbstractItemView.ScrollHint.EnsureVisible)
                            except Exception:
                                pass
                            if hasattr(self, "_on_browser_row_changed"):
                                self._on_browser_row_changed(browser)
            except Exception as e:
                print(f"[anki-diff] _navigate_card_relative view error: {e}")

    def _toggle_history_tab(self) -> None:
        """Bascule entre l'onglet Diff (0) et l'onglet Historique (1)."""
        if hasattr(self, "_tabs") and self._tabs is not None:
            if self._tabs.currentIndex() == 1:
                self._tabs.setCurrentIndex(0)
            else:
                self._tabs.setCurrentIndex(1)

    def _toggle_old_fields_tab(self) -> None:
        """Alias de compatibilité pour basculer vers l'onglet Historique."""
        self._toggle_history_tab()

    def _copy_current_note_old_fields(self) -> None:
        """Copie tous les champs d'origine (pré-maj) de la note courante."""
        if self._current_nid is None:
            return
        diff = _pending_diffs.get(self._current_nid)
        if not diff:
            return
        parts = []
        for fn in diff.field_names:
            v = diff.original_fields.get(fn, "")
            parts.append(f"--- {fn} ---\n{v}")
        app = QtWidgets.QApplication.instance()
        if app and app.clipboard():
            app.clipboard().setText("\n\n".join(parts))

    def closeEvent(self, event):
        try:
            from aqt.qt import sip
            if sip.isdeleted(self):
                return
        except Exception:
            pass

        try:
            self._restore_browser_splitter()
        except Exception:
            pass

        try:
            gui_hooks.browser_did_change_row.remove(self._on_browser_row_changed)
        except (ValueError, RuntimeError, KeyError):
            pass

        try:
            self.cleanup()
        except Exception:
            pass

        try:
            from aqt.qt import sip
            if not sip.isdeleted(self):
                super().closeEvent(event)
        except RuntimeError:
            pass

    def _restore_browser_splitter(self) -> None:
        """
        Restaure form.splitter[1] à son widget d'origine (fieldsArea),
        en supprimant le sous-splitter injecté.
        Sauvegarde l'état courant (géométrie + tailles) avant démontage.
        Restaure le splitter principal à son état pré-diff pour préserver l'explorateur normal.
        """
        try:
            from aqt.qt import sip
            if sip.isdeleted(self):
                return
            if self._editor_diff_splitter is None or self._original_splitter_widget is None:
                return

            # Sauvegarder l'état courant avant de démonter le splitter
            try:
                if self._browser and not sip.isdeleted(self._browser):
                    _save_browser_state(self._browser, self._editor_diff_splitter)
            except Exception as e:
                print(f"[anki-diff] _save_browser_state in restore: {e}")

            eds = self._editor_diff_splitter
            orig = self._original_splitter_widget
            self._editor_diff_splitter = None
            self._original_splitter_widget = None

            if not sip.isdeleted(orig):
                form_splitter = getattr(getattr(self._browser, "form", None), "splitter", None)
                if form_splitter and not sip.isdeleted(form_splitter):
                    try:
                        # Déconnecter le handler splitterMoved
                        try:
                            form_splitter.splitterMoved.disconnect(self._on_splitter_moved)
                        except Exception:
                            pass

                        form_splitter.replaceWidget(1, orig)

                        # Restaurer l'état pré-diff d'Anki pour ne pas corrompre le profil normal
                        if self._pre_diff_main_splitter_state:
                            try:
                                from aqt.qt import QByteArray
                                form_splitter.restoreState(QByteArray(self._pre_diff_main_splitter_state))
                            except Exception:
                                pass
                        if self._pre_diff_main_splitter_sizes and len(self._pre_diff_main_splitter_sizes) == 2:
                            try:
                                form_splitter.setSizes(self._pre_diff_main_splitter_sizes)
                            except Exception:
                                pass
                    except Exception as e:
                        print(f"[anki-diff] error replacing orig widget: {e}")

            # Détacher self pour éviter toute suppression en cascade
            if not sip.isdeleted(self):
                self.setParent(None)

            if eds is not None and not sip.isdeleted(eds):
                try:
                    eds.splitterMoved.disconnect(self._on_splitter_moved)
                except Exception:
                    pass
                eds.setParent(None)
                eds.deleteLater()
        except Exception as e:
            print(f"[anki-diff] _restore_browser_splitter failed: {e}")


    def cleanup(self) -> None:
        """Disconnect signals and explicitly delete buttons to avoid PyQt6 wildcard disconnect warnings."""
        try:
            self._restore_browser_splitter()
        except Exception:
            pass

        try:
            gui_hooks.browser_did_change_row.remove(self._on_browser_row_changed)
        except (ValueError, RuntimeError, KeyError):
            pass

        _restore_editor_save()

        # 1. Supprimer les notes INSERT pré-insérées (staged)
        # 2. Restaurer les notes UPDATE à leur état d'origine
        global _committed, _pending_diffs
        if not _committed and _pending_diffs:
            # Supprimer les notes INSERT temporairement insérées
            try:
                _unstage_insert_notes(_pending_diffs)
            except Exception as e:
                print(f"[anki-diff] Error unstaging INSERT notes: {e}")

            # Nettoyer les liens morts pointant vers les notes annulées
            try:
                from .temp_link_interceptor import TempLinkInterceptor
                TempLinkInterceptor.instance().on_diff_session_closed(
                    col=aqt.mw.col if (hasattr(aqt, "mw") and aqt.mw) else None,
                    browser=self._browser,
                    committed=False
                )
            except Exception as e_interceptor:
                print(f"[anki-diff] Error closing temp_link_interceptor on rollback: {e_interceptor}")

            restored_count = 0
            try:
                col = aqt.mw.col
                # IMPORTANT: ne pas utiliser col.db.execute("begin"/"commit") — conflit
                # avec les transactions internes du backend Rust (update_note gère les siennes).
                for nid, diff in list(_pending_diffs.items()):
                    if diff.action == "UPDATE":
                        try:
                            note = col.get_note(nid)
                            model = col.models.get(note.mid)
                            if not model:
                                continue
                            field_indices = {f["name"]: idx for idx, f in enumerate(model.get("flds", []))}

                            # Restaurer chaque champ et les tags d'origine
                            modified = False
                            for name, orig_val in diff.original_fields.items():
                                idx = field_indices.get(name)
                                if idx is not None and idx < len(note.fields):
                                    if note.fields[idx] != orig_val:
                                        note.fields[idx] = orig_val
                                        modified = True
                            orig_tags = list(diff.original_tags) if hasattr(diff, "original_tags") and diff.original_tags is not None else []
                            if list(getattr(note, "tags", [])) != orig_tags:
                                note.tags = orig_tags
                                modified = True
                            if modified:
                                col.update_note(note)  # gère sa propre transaction Rust
                                restored_count += 1
                        except Exception:
                            pass
                if restored_count > 0:
                    aqt.utils.tooltip(f"Review annulée. {restored_count} note(s) restaurée(s) à leur état initial.")
                elif _pending_diffs:
                    rejected_updates = sum(1 for d in _pending_diffs.values() if d.action == "UPDATE")
                    rejected_inserts = sum(1 for d in _pending_diffs.values() if d.action == "INSERT")
                    parts = []
                    if rejected_updates:
                        parts.append(f"{rejected_updates} modification(s) rejetée(s)")
                    if rejected_inserts:
                        parts.append(f"{rejected_inserts} nouvelle(s) carte(s) annulée(s)")
                    aqt.utils.tooltip(f"Importation rejetée : {', '.join(parts)}.")
                # Essayer de rafraîchir le navigateur
                try:
                    self._browser.loadNote()
                    self._browser.on_notes_changed()
                except Exception:
                    pass
            except Exception as e:
                print("[anki-diff] Error during rollback of uncommitted changes:", e)

        # Clear pending diffs
        clear_pending_diffs()

        # Retirer le hook d'unfocus si installé
        global _field_edit_hook
        if _field_edit_hook is not None:
            try:
                gui_hooks.editor_did_unfocus_field.remove(_field_edit_hook)
            except Exception:
                pass
            _field_edit_hook = None

        from aqt.qt import sip
        for panel in list(self._note_panels.values()):
            if sip.isdeleted(panel):
                continue
            for fp in list(panel.get_field_panels().values()):
                if sip.isdeleted(fp):
                    continue
                if hasattr(fp, "_btn_accept_all") and fp._btn_accept_all is not None:
                    try:
                        btn = fp._btn_accept_all
                        fp._btn_accept_all = None
                        if not sip.isdeleted(btn):
                            btn.deleteLater()
                    except Exception:
                        pass
                if hasattr(fp, "_btn_reject_all") and fp._btn_reject_all is not None:
                    try:
                        btn = fp._btn_reject_all
                        fp._btn_reject_all = None
                        if not sip.isdeleted(btn):
                            btn.deleteLater()
                    except Exception:
                        pass
        if hasattr(self, "_event_filter") and self._event_filter is not None:
            app = QtWidgets.QApplication.instance()
            if app:
                try:
                    app.removeEventFilter(self._event_filter)
                except Exception:
                    pass
            self._event_filter = None

        if hasattr(self, "_shortcuts"):
            for sc in self._shortcuts:
                try:
                    sc.setEnabled(False)
                    sc.deleteLater()
                except Exception:
                    pass
            self._shortcuts.clear()

        for btn_attr in ("_acc_all_btn", "_rej_all_btn", "_commit_btn", "_reject_import_btn", "_close_panel_btn", "_changed_only_cb", "_btn_learned_only"):
            if hasattr(self, btn_attr) and getattr(self, btn_attr) is not None:
                try:
                    btn = getattr(self, btn_attr)
                    setattr(self, btn_attr, None)
                    if not sip.isdeleted(btn):
                        btn.deleteLater()
                except Exception:
                    pass
        if hasattr(self, "_tabs") and self._tabs is not None:
            try:
                if not sip.isdeleted(self._tabs):
                    self._tabs.currentChanged.disconnect(self._on_tab_changed)
            except Exception:
                pass

        # Restaurer closeEvent et keyPressEvent d'origine sur le navigateur
        browser = getattr(self, "_browser", None)
        if browser is not None:
            try:
                if hasattr(browser, "_orig_diff_close_event") and browser._orig_diff_close_event is not None:
                    browser.closeEvent = browser._orig_diff_close_event
                    browser._orig_diff_close_event = None
                if hasattr(browser, "_orig_diff_key_press_event") and browser._orig_diff_key_press_event is not None:
                    browser.keyPressEvent = browser._orig_diff_key_press_event
                    browser._orig_diff_key_press_event = None
            except Exception:
                pass

    # ── Construction ────────────────────────────────────────────────────────

    def _build(self):
        root_layout = QtWidgets.QVBoxLayout(self)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)

        # ── Barre d'outils (header) ───────────────────────────────────────
        toolbar_frame = QtWidgets.QFrame()
        toolbar_frame.setStyleSheet(
            f"background-color:{_CLR_HDR_BG}; border-bottom:1px solid {_CLR_BORDER};"
        )
        toolbar = QtWidgets.QVBoxLayout(toolbar_frame)
        toolbar.setContentsMargins(12, 12, 12, 12)
        toolbar.setSpacing(8)

        # Row 1: Logo & Close Button
        row1 = QtWidgets.QHBoxLayout()
        row1.setContentsMargins(0, 0, 0, 0)
        
        logo = QtWidgets.QLabel("Diff Review")
        logo.setStyleSheet(f"color:{_CLR_HDR_FG}; font-size:13px; font-weight:700;")
        row1.addWidget(logo)
        
        row1.addStretch()

        self._close_panel_btn = QtWidgets.QPushButton("Fermer")
        self._close_panel_btn.setObjectName("dock_close_btn")
        self._close_panel_btn.setStyleSheet(f"""
            QPushButton {{
                background-color: transparent;
                color: {_CLR_SUBTEXT};
                border: 1px solid {_CLR_BORDER};
                border-radius: 4px;
                padding: 2px 8px;
                font-size: 11px;
                font-weight: 500;
            }}
            QPushButton:hover {{
                background-color: {_CLR_BORDER};
                color: {_CLR_HDR_FG};
            }}
        """)
        self._close_panel_btn.setCursor(QtGui.QCursor(_Qt.CursorShape.PointingHandCursor))
        self._close_panel_btn.setToolTip("Fermer le panneau de diff")
        self._close_panel_btn.clicked.connect(self._on_request_close_panel)
        row1.addWidget(self._close_panel_btn)
        
        toolbar.addLayout(row1)

        # Row 1b: Status Label on its own line (supports wrapping and narrow panel width)
        self._status_lbl = QtWidgets.QLabel("")
        self._status_lbl.setStyleSheet(f"color:{_CLR_SUBTEXT}; font-size:11px;")
        self._status_lbl.setWordWrap(True)
        toolbar.addWidget(self._status_lbl)

        # Row 1c: Option Champs modifiés uniquement (pré-coché par défaut)
        row_opts = QtWidgets.QHBoxLayout()
        row_opts.setContentsMargins(0, 0, 0, 0)
        row_opts.setSpacing(6)

        self._changed_only_cb = QtWidgets.QCheckBox("Champs modifiés uniquement")
        self._changed_only_cb.setObjectName("dock_changed_only_cb")
        self._changed_only_cb.setChecked(self._only_changed_fields)
        self._changed_only_cb.setStyleSheet(f"""
            QCheckBox {{
                color: {_CLR_TEXT};
                font-size: 11px;
                font-weight: 500;
                spacing: 6px;
            }}
            QCheckBox:hover {{
                color: {_CLR_HDR_FG};
            }}
        """)
        self._changed_only_cb.setCursor(QtGui.QCursor(_Qt.CursorShape.PointingHandCursor))
        self._changed_only_cb.setToolTip("Afficher uniquement les champs ayant des modifications dans cette note")
        self._changed_only_cb.toggled.connect(self._on_changed_only_toggled)
        row_opts.addWidget(self._changed_only_cb)
        row_opts.addStretch()
        toolbar.addLayout(row_opts)

        # Row 1d: Bouton filtre Cartes déjà apprises uniquement (nativement désactivé)
        row_learned = QtWidgets.QHBoxLayout()
        row_learned.setContentsMargins(0, 0, 0, 0)
        row_learned.setSpacing(6)

        self._btn_learned_only = QtWidgets.QPushButton("Cartes déjà apprises")
        self._btn_learned_only.setObjectName("dock_learned_only_btn")
        self._btn_learned_only.setCheckable(True)
        self._btn_learned_only.setChecked(self._learned_cards_only)
        self._btn_learned_only.setStyleSheet(_get_learned_btn_style(self._learned_cards_only))
        self._btn_learned_only.setCursor(QtGui.QCursor(_Qt.CursorShape.PointingHandCursor))
        self._btn_learned_only.setToolTip("Afficher uniquement les modifications des cartes déjà apprises (hors nouvelles cartes)")
        self._btn_learned_only.toggled.connect(self._on_learned_only_toggled)
        row_learned.addWidget(self._btn_learned_only)
        toolbar.addLayout(row_learned)

        # Row 2: All / Clear Hunks for the current note
        row2 = QtWidgets.QHBoxLayout()
        row2.setContentsMargins(0, 0, 0, 0)
        row2.setSpacing(8)

        self._acc_all_btn = QtWidgets.QPushButton("Accepter tous les hunks")
        self._acc_all_btn.setObjectName("dock_accept_all_btn")
        self._acc_all_btn.setStyleSheet(_get_style_btn_accept())
        self._acc_all_btn.setMinimumHeight(26)
        self._acc_all_btn.setToolTip("Accepter tous les champs de la note en cours (F2, Alt+A ou Alt+V)")
        self._acc_all_btn.setCursor(QtGui.QCursor(_Qt.CursorShape.PointingHandCursor))
        self._acc_all_btn.setFocusPolicy(_Qt.FocusPolicy.NoFocus)
        self._acc_all_btn.clicked.connect(self._accept_all_current)

        self._rej_all_btn = QtWidgets.QPushButton("Rejeter tous les hunks")
        self._rej_all_btn.setObjectName("dock_reject_all_btn")
        self._rej_all_btn.setStyleSheet(_get_style_btn_reject())
        self._rej_all_btn.setMinimumHeight(26)
        self._rej_all_btn.setToolTip("Rejeter tous les champs de la note en cours (F3 ou Alt+R)")
        self._rej_all_btn.setCursor(QtGui.QCursor(_Qt.CursorShape.PointingHandCursor))
        self._rej_all_btn.setFocusPolicy(_Qt.FocusPolicy.NoFocus)
        self._rej_all_btn.clicked.connect(self._reject_all_current)
        
        row2.addWidget(self._acc_all_btn, stretch=1)
        row2.addWidget(self._rej_all_btn, stretch=1)
        toolbar.addLayout(row2)

        # Row 3 & 4: Global buttons (Commit update, Reject import)
        self._commit_btn = QtWidgets.QPushButton("Valider la mise à jour")
        self._commit_btn.setObjectName("dock_commit_btn")
        self._commit_btn.setStyleSheet(_get_style_btn_primary())
        self._commit_btn.setMinimumHeight(30)
        self._commit_btn.setCursor(QtGui.QCursor(_Qt.CursorShape.PointingHandCursor))
        self._commit_btn.clicked.connect(self._on_commit_all)
        self._commit_btn.setEnabled(bool(_pending_diffs))

        self._reject_import_btn = QtWidgets.QPushButton("Rejeter l'importation")
        self._reject_import_btn.setObjectName("dock_reject_import_btn")
        self._reject_import_btn.setStyleSheet(_get_style_btn_danger())
        self._reject_import_btn.setMinimumHeight(30)
        self._reject_import_btn.setCursor(QtGui.QCursor(_Qt.CursorShape.PointingHandCursor))
        self._reject_import_btn.clicked.connect(self._on_reject_import)
        self._reject_import_btn.setEnabled(bool(_pending_diffs))

        toolbar.addWidget(self._commit_btn)
        toolbar.addWidget(self._reject_import_btn)
        root_layout.addWidget(toolbar_frame)

        # ── Progress bar ─────────────────────────────────────────────────
        self._progress_bar = QtWidgets.QProgressBar()
        self._progress_bar.setStyleSheet(
            f"QProgressBar {{ background: {_CLR_BORDER}; border: none; border-radius: 0; height: 3px; }}"
            f"QProgressBar::chunk {{ background: #6366f1; }}"
        )
        self._progress_bar.setFixedHeight(3)
        self._progress_bar.setTextVisible(False)
        self._progress_bar.setMinimum(0)
        self._progress_bar.setMaximum(max(len(_pending_diffs), 1))
        self._progress_bar.setValue(0)
        root_layout.addWidget(self._progress_bar)

        # ── Corps : tabs Diff | Historique ──────────────────────────────
        self._tabs = QtWidgets.QTabWidget()
        self._tabs.setStyleSheet(
            f"QTabWidget::pane {{ border: none; background: {_CLR_PANEL}; }}"
            f"QTabBar::tab {{ background: {_CLR_HDR_BG}; color: {_CLR_SUBTEXT}; "
            f"border: 1px solid {_CLR_BORDER}; border-bottom: none; padding: 4px 12px; font-size: 12px; }}"
            f"QTabBar::tab:selected {{ background: {_CLR_PANEL}; color: {_CLR_TEXT}; "
            f"border-bottom: 2px solid #6366f1; }}"
            f"QTabBar::tab:hover:!selected {{ color: {_CLR_TEXT}; }}"
        )

        # Tab 1 : Diff (stack des panels)
        diff_tab = QtWidgets.QWidget()
        diff_tab.setStyleSheet(f"background: {_CLR_PANEL};")
        diff_tab_layout = QtWidgets.QVBoxLayout(diff_tab)
        diff_tab_layout.setContentsMargins(0, 0, 0, 0)

        self._diff_stack = QtWidgets.QStackedWidget()
        self._diff_stack.setSizePolicy(
            QtWidgets.QSizePolicy.Policy.Expanding,
            QtWidgets.QSizePolicy.Policy.Expanding
        )
        self._diff_stack.setStyleSheet(f"background:{_CLR_PANEL};")
        self._build_note_panels()
        diff_tab_layout.addWidget(self._diff_stack)
        self._tabs.addTab(diff_tab, "Diff")

        # Tab 1 : Historique & AnkiCollab
        self._history_container = QtWidgets.QWidget()
        self._history_container.setStyleSheet(f"background: {_CLR_PANEL};")
        self._history_layout = QtWidgets.QVBoxLayout(self._history_container)
        self._history_layout.setContentsMargins(0, 0, 0, 0)
        self._history_layout.setSpacing(0)

        self._history_placeholder = QtWidgets.QLabel(
            f"<center><span style='color:{_CLR_SUBTEXT};font-size:12px;'>"
            f"Sélectionnez une note pour voir son historique</span></center>"
        )
        self._history_placeholder.setTextFormat(_Qt.TextFormat.RichText)
        self._history_layout.addStretch()
        self._history_layout.addWidget(self._history_placeholder)
        self._history_layout.addStretch()

        self._history_tab_widget = None  # AnkiCollabHistoryPanel
        self._tabs.addTab(self._history_container, "Historique")

        root_layout.addWidget(self._tabs, stretch=1)
        self._tabs.currentChanged.connect(self._on_tab_changed)
        self._update_status()

    def _build_note_panels(self):
        self._note_panels.clear()
        self._not_in_diff_widget = self._create_not_in_diff_widget()
        self._diff_stack.addWidget(self._not_in_diff_widget)
        self._no_learned_widget = self._create_no_learned_widget()
        self._diff_stack.addWidget(self._no_learned_widget)

        if _pending_diffs and self._last_diff_nid is None:
            first_diff = next(iter(_pending_diffs.values()), None)
            if first_diff:
                self._last_diff_nid = first_diff.staged_note_id if (first_diff.action == "INSERT" and first_diff.staged_note_id) else first_diff.note_id

    def _get_or_create_panel(self, nid: int) -> Optional[NoteDiffPanelWithSync]:
        if nid in self._note_panels:
            return self._note_panels[nid]
        orig_id = _staged_to_orig.get(nid, nid)
        if orig_id in self._note_panels:
            return self._note_panels[orig_id]
        diff = _pending_diffs.get(orig_id) or _pending_diffs.get(nid)
        if not diff:
            return None
        panel = NoteDiffPanelWithSync(diff, self, self._diff_stack)
        self._note_panels[diff.note_id] = panel
        if diff.staged_note_id:
            self._note_panels[diff.staged_note_id] = panel
        self._diff_stack.addWidget(panel)
        return panel

    def _create_not_in_diff_widget(self) -> QtWidgets.QWidget:
        w = QtWidgets.QWidget()
        w.setStyleSheet(f"background-color:{_CLR_PANEL};")
        layout = QtWidgets.QVBoxLayout(w)
        layout.setContentsMargins(20, 24, 20, 20)
        layout.setSpacing(10)

        self._not_in_diff_title = QtWidgets.QLabel(w)
        self._not_in_diff_title.setStyleSheet(
            f"color:{_CLR_TEXT}; font-size:13px; font-weight:700;"
        )
        self._not_in_diff_title.setWordWrap(True)
        layout.addWidget(self._not_in_diff_title)

        badge = QtWidgets.QLabel("Note non modifiée par cette importation", w)
        badge.setStyleSheet(
            f"color:{_CLR_SUBTEXT}; font-size:11px; font-weight:600; background:{_CLR_HDR_BG}; "
            f"border:1px solid {_CLR_BORDER}; border-radius:10px; padding:3px 10px;"
        )
        layout.addWidget(badge)

        self._not_in_diff_desc = QtWidgets.QLabel(
            "Cette carte fait partie de votre collection mais ne comporte aucune modification "
            "en attente dans cette session de revue.",
            w,
        )
        self._not_in_diff_desc.setStyleSheet(f"color:{_CLR_SUBTEXT}; font-size:12px; line-height:1.45;")
        self._not_in_diff_desc.setWordWrap(True)
        layout.addWidget(self._not_in_diff_desc)

        layout.addSpacing(8)

        btn_hist = QtWidgets.QPushButton("Afficher l'historique de cette carte", w)
        btn_hist.setCursor(QtGui.QCursor(_Qt.CursorShape.PointingHandCursor))
        btn_hist.setStyleSheet(_get_style_btn_secondary())
        btn_hist.setMinimumHeight(28)
        btn_hist.clicked.connect(lambda: self._tabs.setCurrentIndex(1))
        layout.addWidget(btn_hist)

        self._btn_back_to_diff = QtWidgets.QPushButton("Revenir aux modifications en attente", w)
        self._btn_back_to_diff.setCursor(QtGui.QCursor(_Qt.CursorShape.PointingHandCursor))
        self._btn_back_to_diff.setStyleSheet(_get_style_btn_primary())
        self._btn_back_to_diff.setMinimumHeight(28)

        self._btn_back_to_diff.clicked.connect(self._back_to_diff_notes)
        layout.addWidget(self._btn_back_to_diff)

        layout.addStretch()
        return w

    def _show_history_for_note(self, nid: int) -> None:
        """Crée ou met à jour l'onglet Historique pour la note sélectionnée."""
        if nid is None:
            return
        real_nid = _staged_to_orig.get(nid, nid)
        diff = _pending_diffs.get(real_nid) or _pending_diffs.get(nid)

        field_names = []
        current_fields = {}
        if diff is not None:
            field_names = list(diff.field_names)
            current_fields = {fn: diff.original_fields.get(fn, "") for fn in diff.field_names}
            current_fields["__tags__"] = " ".join(diff.tags)
        else:
            try:
                col = getattr(self._browser, "col", None) or getattr(aqt.mw, "col", None)
                if col:
                    note = col.get_note(real_nid)
                    if note:
                        field_names = [f[0] for f in note.items()]
                        current_fields = dict(note.items())
                        current_fields["__tags__"] = " ".join(note.tags)
            except Exception:
                pass

        if not field_names:
            return

        try:
            from .ankicollab_panel import AnkiCollabHistoryPanel
            if self._history_tab_widget is None:
                # Vider le placeholder du container
                while self._history_layout.count():
                    item = self._history_layout.takeAt(0)
                    w = item.widget()
                    if w:
                        w.deleteLater()

                panel = AnkiCollabHistoryPanel(
                    note_id=real_nid,
                    field_names=field_names,
                    current_fields=current_fields,
                    parent=self._history_container,
                )
                panel.version_retrieved.connect(
                    lambda fields: self._on_history_version_retrieved(
                        self._history_tab_widget._note_id if self._history_tab_widget is not None else real_nid, fields
                    )
                )
                panel.fields_protected.connect(
                    lambda fields: self._on_history_fields_protected(
                        self._history_tab_widget._note_id if self._history_tab_widget is not None else real_nid, fields
                    )
                )
                panel.protection_tags_applied.connect(
                    lambda tags: self._on_history_protection_tags_applied(
                        self._history_tab_widget._note_id if self._history_tab_widget is not None else real_nid, tags
                    )
                )
                self._history_tab_widget = panel
                self._history_layout.addWidget(panel)
            else:
                self._history_tab_widget.set_note(real_nid, field_names, current_fields)
        except Exception as e:
            print(f"[anki-diff] Erreur affichage historique pour la note {nid}: {e}")
            import traceback
            traceback.print_exc()

    def _on_history_version_retrieved(self, note_id: int, fields: dict) -> None:
        """Applique une version récupérée depuis l'historique (dans le diff ou directement sur la note)."""
        panel = self._note_panels.get(note_id)
        diff = _pending_diffs.get(note_id) if panel is not None else None

        if panel is None or diff is None:
            # Note dans la collection (pas de diff d'import en attente)
            try:
                col = getattr(self._browser, "col", None) or aqt.mw.col
                if col:
                    target_note = col.get_note(note_id)
                    if target_note:
                        for fname, new_val in fields.items():
                            if fname == "__tags__":
                                target_note.tags = [t for t in new_val.strip().split(" ") if t]
                            elif fname in target_note:
                                target_note[fname] = new_val
                        col.update_note(target_note)
                        from .history import NoteHistoryDB
                        NoteHistoryDB.instance().store_snapshot_async(
                            note_id=note_id,
                            fields=dict(target_note.items()),
                            source="restore",
                            comment="Restauration d'une version précédente" if len(fields) > 1 else f"Restauration du champ {next(iter(fields.keys()))}",
                            tags=target_note.tags,
                        )
                        if hasattr(self._browser, "editor") and self._browser.editor:
                            if hasattr(self._browser.editor, "reload_note"):
                                self._browser.editor.reload_note()
                            elif hasattr(self._browser.editor, "loadNote"):
                                self._browser.editor.loadNote()
                        if hasattr(self._browser, "loadNote"):
                            self._browser.loadNote()
                        if hasattr(self._browser, "on_notes_changed"):
                            self._browser.on_notes_changed()
                        label = "Version restaurée avec succès" if len(fields) > 1 else f"Champ restauré avec succès"
                        aqt.utils.tooltip(label)
            except Exception as e:
                print(f"[anki-diff] Erreur lors de la restauration directe de la note {note_id}: {e}")
            return
        for fname, new_val in fields.items():
            if fname == "__tags__":
                diff.tags = [t for t in new_val.strip().split(" ") if t]
                if self._history_tab_widget is not None:
                    self._history_tab_widget._current_fields["__tags__"] = new_val
                    self._history_tab_widget._render_comparison_view()
                continue
            fp = panel._field_panels.get(fname)
            if fp is not None:
                from .diff import compute_hunks
                fp.new_val = new_val
                fp.prop_mod = new_val
                fp.active_target = "PROP"
                if diff is not None:
                    diff.prop_mod_fields[fname] = new_val
                    diff.field_active_target[fname] = "PROP"
                    diff.merged_fields[fname] = new_val
                fp.hunks = compute_hunks(fp.old_val, new_val)
                for h in fp.hunks:
                    h.accepted = True
                fp._refresh_display()
                fp.previewChanged.emit()
        panel._sync_all_merged_then_editor()

    def _on_history_fields_protected(self, note_id: int, fields: dict) -> None:
        panel = self._note_panels.get(note_id)
        if panel is None:
            return
        panel._protected_fields.update(fields)
        for field_name in fields:
            if field_name == "__tags__":
                continue
            fp = panel._field_panels.get(field_name)
            if fp is not None:
                fp._protected = True
                for hw in fp._hunk_widgets:
                    hw.hunk.accepted = False
                    hw._protected = True
                    hw._refresh_state()
                fp._refresh_display()
                fp.previewChanged.emit()

    def _on_history_protection_tags_applied(self, note_id: int, tags: List[str]) -> None:
        diff = _pending_diffs.get(note_id)
        if diff is None:
            return
        diff.tags = [t for t in diff.tags if not t.lower().startswith("ankicollab_protect::")]
        diff.tags.extend(tags)

    # ── Synchronisation avec le navigateur ──────────────────────────────────

    def select_note(self, nid: Optional[int]) -> None:
        """Sélectionne et affiche la note donnée dans le panneau de diff et l'historique."""
        if nid is None:
            return

        panel = self._get_or_create_panel(nid)
        panel_nid = nid
        if panel is not None:
            panel_nid = panel._diff.note_id
            self._current_nid = panel_nid
            self._last_diff_nid = panel_nid
            if self._diff_stack.currentWidget() != panel:
                self._diff_stack.setCurrentWidget(panel)

            nids = [d.note_id for d in _pending_diffs.values()]
            try:
                idx = nids.index(panel_nid)
                self._progress_bar.setValue(idx + 1)
            except (ValueError, AttributeError):
                pass
        else:
            self._current_nid = panel_nid
            if hasattr(self, "_not_in_diff_widget") and self._not_in_diff_widget is not None:
                title = f"Note {nid}"
                try:
                    col = getattr(self._browser, "col", None) or getattr(aqt.mw, "col", None)
                    if col:
                        note = col.get_note(nid)
                        if note:
                            model = col.models.get(note.mid)
                            model_name = model.get("name", "") if model else ""
                            first_fld = note.fields[0] if note.fields else ""
                            first_fld_clean = _re.sub(r"<[^>]+>", "", first_fld).strip()[:40]
                            if first_fld_clean:
                                title = f"Note {nid} — {first_fld_clean}"
                            elif model_name:
                                title = f"Note {nid} — {model_name}"
                except Exception:
                    pass
                if hasattr(self, "_not_in_diff_title") and self._not_in_diff_title is not None:
                    self._not_in_diff_title.setText(title)
                self._diff_stack.setCurrentWidget(self._not_in_diff_widget)

        # Actualiser l'historique uniquement si l'onglet Historique est actuellement visible
        if hasattr(self, "_tabs") and self._tabs.currentIndex() == 1:
            try:
                self._show_history_for_note(nid)
            except Exception:
                pass

    def get_diff_search_nids(self) -> List[int]:
        """Retourne la liste des IDs de notes de la collection correspondant aux diffs en attente."""
        if getattr(self, "_learned_cards_only", False):
            learned = self.get_learned_diff_nids()
            return learned
        search_nids = []
        for d in _pending_diffs.values():
            if d.action == "INSERT" and d.staged_note_id:
                search_nids.append(d.staged_note_id)
            elif d.action != "INSERT":
                search_nids.append(d.note_id)
        if not search_nids:
            search_nids = [d.note_id for d in _pending_diffs.values()]
        return search_nids

    def get_diff_search_str(self) -> str:
        """Construit la requête de recherche Anki pour afficher toutes les cartes en cours de révision."""
        nids = self.get_diff_search_nids()
        if not nids:
            if getattr(self, "_learned_cards_only", False):
                return "nid:0"
            return ""
        return "nid:" + ",".join(str(nid) for nid in nids)

    def _back_to_diff_notes(self) -> None:
        """Restaure la recherche du browser sur l'ensemble des notes du diff et resélectionne la note active."""
        if not _pending_diffs:
            return

        search_nids = self.get_diff_search_nids()
        search_str = self.get_diff_search_str()

        if getattr(self, "_learned_cards_only", False) and not search_nids:
            if self._browser and hasattr(self._browser, "search_for"):
                self._browser.search_for("nid:0")
            if hasattr(self, "_no_learned_widget") and self._no_learned_widget is not None:
                self._diff_stack.setCurrentWidget(self._no_learned_widget)
            return

        if not search_str or not search_nids:
            return

        # Déterminer la note cible à afficher : la dernière vue dans le diff ou la première
        target_nid = None
        if self._last_diff_nid is not None and self._last_diff_nid in search_nids:
            target_nid = self._last_diff_nid
        elif self._last_diff_nid is not None:
            real_orig = _staged_to_orig.get(self._last_diff_nid, self._last_diff_nid)
            if real_orig in search_nids:
                target_nid = real_orig

        if target_nid is None:
            target_nid = search_nids[0]

        # Déterminer le note_id dans la collection Anki pour la sélection de carte
        col_nid = target_nid
        diff_obj = _pending_diffs.get(target_nid)
        if diff_obj and diff_obj.action == "INSERT" and diff_obj.staged_note_id:
            col_nid = diff_obj.staged_note_id

        # 1. Rétablir la recherche Anki pour afficher TOUTES les cartes du diff
        if self._browser:
            try:
                if hasattr(self._browser, "search_for"):
                    self._browser.search_for(search_str)
                col = getattr(self._browser, "col", None) or getattr(aqt.mw, "col", None)
                if col and hasattr(self._browser, "table") and self._browser.table is not None:
                    card_ids = []
                    try:
                        if hasattr(col, "card_ids_of_note"):
                            card_ids = col.card_ids_of_note(col_nid)
                        else:
                            n = col.get_note(col_nid)
                            if n:
                                card_ids = n.card_ids()
                    except Exception:
                        pass
                    if card_ids and hasattr(self._browser.table, "select_single_card"):
                        self._browser.table.select_single_card(card_ids[0])
            except Exception as e:
                print(f"[anki-diff] _back_to_diff_notes error: {e}")

        # 2. Resélectionner la note dans notre panneau
        self.select_note(target_nid)

    def _on_browser_row_changed(self, browser) -> None:
        """Appelé par Anki quand la ligne sélectionnée dans le browser change."""
        try:
            from aqt.qt import sip
            if sip.isdeleted(self):
                return
        except Exception:
            pass

        try:
            if browser is not self._browser:
                if hasattr(self._browser, "form") and getattr(self._browser, "form", None) is None:
                    self._browser = browser
                else:
                    return

            nid = None

            # 1. Inspecter browser.table (méthodes officielles Anki 2.1+)
            if hasattr(browser, "table") and browser.table is not None:
                try:
                    if hasattr(browser.table, "get_current_note"):
                        n = browser.table.get_current_note()
                        if n and hasattr(n, "id") and isinstance(n.id, int) and n.id:
                            nid = n.id
                except Exception:
                    pass

                if nid is None and hasattr(browser.table, "current_note"):
                    try:
                        n = browser.table.current_note()
                        if n and hasattr(n, "id") and isinstance(n.id, int) and n.id:
                            nid = n.id
                    except Exception:
                        pass

                if nid is None:
                    try:
                        if hasattr(browser.table, "get_current_card"):
                            c = browser.table.get_current_card()
                            if c and hasattr(c, "nid") and isinstance(c.nid, int) and c.nid:
                                nid = c.nid
                    except Exception:
                        pass

                if nid is None:
                    try:
                        if hasattr(browser.table, "get_single_selected_card"):
                            c = browser.table.get_single_selected_card()
                            if c and hasattr(c, "nid") and isinstance(c.nid, int) and c.nid:
                                nid = c.nid
                    except Exception:
                        pass

                if nid is None:
                    try:
                        if hasattr(browser.table, "get_selected_note_ids"):
                            sn = browser.table.get_selected_note_ids()
                            if sn:
                                nid = sn[0]
                    except Exception:
                        pass

            # 2. Inspecter browser.editor.note si disponible
            if nid is None and hasattr(browser, "editor") and browser.editor is not None:
                ed_note = getattr(browser.editor, "note", None)
                if ed_note and hasattr(ed_note, "id") and isinstance(ed_note.id, int) and ed_note.id:
                    nid = ed_note.id

            # 3. Inspecter browser.selected_notes()
            if nid is None and hasattr(browser, "selected_notes"):
                try:
                    selected = browser.selected_notes()
                    if selected:
                        nid = selected[0]
                except Exception:
                    pass

            if nid is not None and nid != self._current_nid:
                self.select_note(nid)
        except Exception as e:
            print(f"[anki-diff] _on_browser_row_changed error: {e}")

    def _on_tab_changed(self, index: int) -> None:
        if index == 1 and self._current_nid is not None:
            try:
                self._show_history_for_note(self._current_nid)
            except Exception:
                pass

    def _install_field_edit_hook(self):
        global _field_edit_hook
        if _field_edit_hook is not None:
            return
        def _on_field_edited(diff, field_name, new_val):
            panel = self._note_panels.get(diff.note_id)
            if panel:
                panel._on_field_edited_by_user(field_name, new_val)
        hook = make_field_edit_hook(get_diff_for_note, _on_field_edited)
        _field_edit_hook = hook
        gui_hooks.editor_did_unfocus_field.append(hook)

    # ── Mettre à jour l'éditeur courant ─────────────────────────────────────

    def set_current_editor(self, editor) -> None:
        """Enregistrer l'editeur actif."""
        self._current_editor = editor
        if editor:
            _inject_editor_web_shortcuts(editor)
        note = _get_editor_note(editor)
        if note and getattr(note, "id", None):
            diff = get_diff_for_note(note.id)
            if diff:
                panel = self._note_panels.get(diff.note_id)
                if panel:
                    panel.set_editor(editor)
                    sync_editor_from_hunks(editor, diff)

    def get_editor_for_note(self, note_id: int):
        """Retourne l'éditeur courant si sa note correspond à note_id."""
        real_target_nid = _staged_to_orig.get(note_id, note_id)
        # Priorité 1 : éditeur enregistré via hook
        if self._current_editor:
            note = _get_editor_note(self._current_editor)
            if note and getattr(note, "id", None):
                real_ed_nid = _staged_to_orig.get(note.id, note.id)
                if real_ed_nid == real_target_nid or note.id == note_id:
                    return self._current_editor
            nid = getattr(self._current_editor, "nid", None)
            if nid and (_staged_to_orig.get(nid, nid) == real_target_nid or nid == note_id):
                return self._current_editor
        # Priorité 2 : accès direct à browser.editor (contourne le problème de timing)
        return self._get_browser_editor_for_note(note_id)

    def _get_browser_editor_for_note(self, note_id: int):
        """Accès direct à browser.editor — fiable même si le hook n'a pas encore tiré."""
        try:
            ed = getattr(self._browser, "editor", None)
            if ed:
                real_target_nid = _staged_to_orig.get(note_id, note_id)
                note = _get_editor_note(ed)
                if note and getattr(note, "id", None):
                    real_ed_nid = _staged_to_orig.get(note.id, note.id)
                    if real_ed_nid == real_target_nid or note.id == note_id:
                        return ed
                nid = getattr(ed, "nid", None)
                if nid and (_staged_to_orig.get(nid, nid) == real_target_nid or nid == note_id):
                    return ed
                if getattr(self, "_current_nid", None) in (note_id, real_target_nid) and note:
                    return ed
        except Exception:
            pass
        return None

    # ── Actions globales ────────────────────────────────────────────────────

    def _get_target_panels(self) -> List["NoteDiffPanelWithSync"]:
        """
        Retourne la liste des panels ciblés par une action accept/reject :
        - Si plusieurs notes sont sélectionnées dans le browser, toutes les notes correspondantes dans le diff.
        - Si une seule note est sélectionnée, le panel de cette note.
        - Sinon, le panel actuellement sélectionné/affiché.
        """
        targets: List["NoteDiffPanelWithSync"] = []
        try:
            if hasattr(self._browser, "selected_notes"):
                selected = self._browser.selected_notes()
                if len(selected) > 1:
                    for nid in selected:
                        real_nid = _staged_to_orig.get(nid, nid)
                        p = self._get_or_create_panel(real_nid)
                        if p and p not in targets:
                            targets.append(p)
                    if targets:
                        return targets
                elif len(selected) == 1:
                    real_nid = _staged_to_orig.get(selected[0], selected[0])
                    p = self._get_or_create_panel(real_nid)
                    if p:
                        return [p]
        except Exception:
            pass

        # Si pas de sélection browser valide, utiliser la note courante
        current = self._get_current_panel()
        if current:
            targets.append(current)
        return targets

    def _get_current_panel(self) -> Optional["NoteDiffPanelWithSync"]:
        """Retourne le panel de la note actuellement affichée dans le stack."""
        if self._current_nid is not None:
            p = self._note_panels.get(self._current_nid)
            if p is not None:
                return p
        curr_widget = self._diff_stack.currentWidget()
        if isinstance(curr_widget, NoteDiffPanelWithSync):
            return curr_widget
        if self._last_diff_nid is not None:
            p = self._note_panels.get(self._last_diff_nid)
            if p is not None:
                return p
        if _pending_diffs:
            first_diff = next(iter(_pending_diffs.values()), None)
            if first_diff:
                return self._note_panels.get(first_diff.note_id)
        return None

    def _accept_all_current(self):
        """Accepter tous les champs de la note en cours ou de la multi-selection."""
        panels = self._get_target_panels()
        for panel in panels:
            panel.accept_all_hunks()
        self._update_status()
        try:
            if hasattr(self._browser, "_previewer") and self._browser._previewer:
                self._browser._previewer._last_state = None
                self._browser._renderPreview()
        except Exception:
            pass

    def _reject_all_current(self):
        """Rejeter tous les champs de la note en cours ou de la multi-sélection."""
        panels = self._get_target_panels()
        for panel in panels:
            if getattr(panel._diff, "action", "") == "INSERT":
                continue
            panel.reject_all_hunks()
        self._update_status()
        try:
            if hasattr(self._browser, "_previewer") and self._browser._previewer:
                self._browser._previewer._last_state = None
                self._browser._renderPreview()
        except Exception:
            pass

    def _accept_tags_only_current(self):
        """Accepter uniquement les tags pour la note en cours ou la multi-sélection (sans impacter les champs)."""
        panels = self._get_target_panels()
        for panel in panels:
            panel.accept_tags_only()
        self._update_status()
        try:
            if hasattr(self._browser, "_previewer") and self._browser._previewer:
                self._browser._previewer._last_state = None
                self._browser._renderPreview()
        except Exception:
            pass

    def _accept_all_global(self):
        """Accepter tous les champs de toutes les notes (non exposé dans l'UI, gardé pour compatibilité)."""
        for nid, panel in self._note_panels.items():
            panel.accept_all_hunks()
        self._update_status()

    def _reject_all_global(self):
        """Rejeter tous les champs de toutes les notes (non exposé dans l'UI, gardé pour compatibilité)."""
        for nid, panel in self._note_panels.items():
            if getattr(panel._diff, "action", "") == "INSERT":
                continue
            panel.reject_all_hunks()
        self._update_status()



    def _on_commit_all(self):
        # Flush l'éditeur actif pour s'assurer que toute frappe en cours est capturée dans note.fields
        editor = getattr(self._browser, "editor", None)
        if editor and hasattr(editor, "saveNow"):
            try:
                editor.saveNow(lambda: self._continue_commit_all())
                return
            except Exception:
                pass
        self._continue_commit_all()

    def apply_commit(self, close_browser: bool = False) -> None:
        """Applique les modifications acceptées dans la collection et nettoie le panneau."""
        # 1. Flush de l'éditeur actif dans la note
        editor = getattr(self._browser, "editor", None)
        note = _get_editor_note(editor)
        if editor and note:
            diff = get_diff_for_note(note.id)
            if diff:
                for idx, field_name in enumerate(diff.field_names):
                    if idx < len(note.fields):
                        if field_name in diff.custom_edit_fields:
                            val = note.fields[idx]
                            diff.custom_edit_fields[field_name] = val
                            diff.merged_fields[field_name] = val
                        else:
                            # Le champ n'a pas d'edition manuelle active :
                            # la version acceptee du diff est reinjectee dans la note
                            note.fields[idx] = diff.merged_fields.get(field_name, "")

        # 2. Flush des valeurs mergées de chaque panel
        for nid, panel in self._note_panels.items():
            diff = _pending_diffs.get(nid)
            if diff:
                diff.merged_fields = panel.get_merged_fields()

        accepted = list(_pending_diffs.values())

        # Mettre à jour les NIDs temporaires dans les merged_fields avant commit
        try:
            from .temp_link_interceptor import TempLinkInterceptor
            TempLinkInterceptor.instance().update_pending_diffs(accepted)
        except Exception:
            pass

        global _committed
        _committed = True

        from .hooks import commit_changes as _commit_changes
        try:
            _commit_changes(accepted)
            try:
                self._browser.search_for("added:1")
            except Exception:
                pass
            try:
                from aqt.gui_hooks import state_did_reset
                state_did_reset()
            except Exception:
                pass
        except Exception as ex:
            print("[anki_import_diff] native commit error:", ex)
            import traceback
            traceback.print_exc()

        # Sauvegarder le callback on_commit avant que cleanup() ne le reinitialise
        global _on_commit_cb
        cb = _on_commit_cb
        _on_commit_cb = None

        self._restore_browser_splitter()
        self.cleanup()

        if cb is not None:
            try:
                cb(accepted)
            except Exception as e_cb:
                print(f'[anki_import_diff] _on_commit_cb error: {e_cb}')

        try:
            aqt.utils.tooltip("Modifications enregistrées dans la collection.")
        except Exception:
            pass

        if close_browser:
            try:
                self._browser.close()
            except Exception:
                pass

    def apply_rollback(self, close_browser: bool = False) -> None:
        """Rejette les modifications, restaure l'état initial des notes et nettoie le panneau."""
        global _committed
        _committed = False

        self._restore_browser_splitter()
        self.cleanup()

        if close_browser:
            try:
                self._browser.close()
            except Exception:
                pass

    def _on_request_close_panel(self) -> None:
        """Appelé lors du clic sur le bouton 'Fermer' du panneau diff."""
        global _committed, _pending_diffs
        if not _committed and bool(_pending_diffs):
            decision = prompt_diff_close_decision(self._browser)
            if decision == "cancel":
                return
            elif decision == "commit":
                self.apply_commit(close_browser=False)
            elif decision == "revert":
                self.apply_rollback(close_browser=False)
        else:
            self._restore_browser_splitter()
            self.cleanup()

    def _continue_commit_all(self):
        total = len(_pending_diffs)
        included_notes = sum(1 for d in _pending_diffs.values() if not d.excluded)

        if included_notes == 0:
            aqt.utils.showInfo("Aucune note n'est sélectionnée pour l'import.")
            return

        reply = QtWidgets.QMessageBox.question(
            self,
            "Confirmer les modifications",
            f"Voulez-vous vraiment appliquer les modifications sur {included_notes} note(s) dans votre collection et fermer le navigateur ?",
            QtWidgets.QMessageBox.StandardButton.Yes | QtWidgets.QMessageBox.StandardButton.No,
            QtWidgets.QMessageBox.StandardButton.No
        )
        if reply != QtWidgets.QMessageBox.StandardButton.Yes:
            return

        self.apply_commit(close_browser=True)

    def _on_reject_import(self):
        reply = QtWidgets.QMessageBox.question(
            self,
            "Rejeter l'importation",
            "Voulez-vous vraiment rejeter toutes les modifications de l'importation, annuler les nouvelles cartes et fermer le navigateur ?",
            QtWidgets.QMessageBox.StandardButton.Yes | QtWidgets.QMessageBox.StandardButton.No,
            QtWidgets.QMessageBox.StandardButton.No
        )
        if reply != QtWidgets.QMessageBox.StandardButton.Yes:
            return

        self.apply_rollback(close_browser=True)

    def _update_status(self):
        total = len(_pending_diffs)
        included_notes = sum(1 for d in _pending_diffs.values() if not d.excluded)
        total_hunks = 0
        accepted_hunks = 0
        for panel in self._note_panels.values():
            if panel._diff.excluded:
                continue
            for fp in panel.get_field_panels().values():
                total_hunks += len(fp.hunks)
                accepted_hunks += sum(1 for h in fp.hunks if h.accepted)

        if hasattr(self, "_btn_learned_only") and self._btn_learned_only is not None:
            learned_count = len(self.get_learned_diff_nids())
            self._btn_learned_only.setText(f"Cartes déjà apprises ({learned_count})")

        filter_suffix = "  ·  Filtre : cartes apprises" if getattr(self, "_learned_cards_only", False) else ""
        self._status_lbl.setText(
            f"{included_notes}/{total} note(s) incluse(s)  ·  {accepted_hunks}/{total_hunks} hunks acceptés{filter_suffix}"
        )
        self._commit_btn.setEnabled(included_notes > 0)


# ---------------------------------------------------------------------------
# NoteDiffPanelWithSync — FieldDiffPanel + synchronisation éditeur
# ---------------------------------------------------------------------------

class NoteDiffPanelWithSync(QtWidgets.QScrollArea):
    """
    Scrollable panel affichant tous les FieldDiffPanel d'une note.

    Étend le comportement de ui.NoteDiffPanel pour synchroniser
    l'éditeur natif quand les hunks changent.
    """

    def __init__(self, diff: NoteDiff, browser_panel: DiffBrowserPanel, parent=None):
        super().__init__(parent)
        self._diff = diff
        self._browser_panel = browser_panel
        self._editor = None
        self._field_panels: Dict[str, FieldDiffPanel] = {}
        self._sync_pending = False   # debounce : un sync est déjà programmé
        self._sync_timer = QtCore.QTimer(self)
        self._sync_timer.setSingleShot(True)
        self._sync_timer.setInterval(30)  # 30 ms : regroupe les rafales de signaux
        self._sync_timer.timeout.connect(self._do_sync_editor)
        self._only_changed: bool = getattr(browser_panel, "_only_changed_fields", True)
        self._unchanged_expanded: bool = False
        self._protected_fields: Dict[str, str] = {}
        try:
            from .history import ProtectedFieldsDB
            pdb = ProtectedFieldsDB.instance() if hasattr(ProtectedFieldsDB, "instance") else ProtectedFieldsDB()
            self._protected_fields = pdb.get_all_protected(diff.note_id)
        except Exception:
            pass

        self.setWidgetResizable(True)
        self.setSizePolicy(
            QtWidgets.QSizePolicy.Policy.Expanding,
            QtWidgets.QSizePolicy.Policy.Expanding
        )
        self.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
        self.setStyleSheet(f"background-color:{_CLR_PANEL}; border:none;")
        self.viewport().setStyleSheet(f"background-color:{_CLR_PANEL};")

        container = QtWidgets.QWidget()
        container.setSizePolicy(
            QtWidgets.QSizePolicy.Policy.Ignored,
            QtWidgets.QSizePolicy.Policy.Preferred,
        )
        container.setStyleSheet(f"background-color:{_CLR_PANEL};")
        layout = QtWidgets.QVBoxLayout(container)
        layout.setContentsMargins(16, 12, 16, 16)
        layout.setSpacing(0)

        # Header vertical widget for Title + Include checkbox (Row 1) & actions (Row 2)
        header_widget = QtWidgets.QWidget()
        header_layout = QtWidgets.QVBoxLayout(header_widget)
        header_layout.setContentsMargins(0, 0, 0, 10)
        header_layout.setSpacing(6)

        # Row 1: Title (Note ID + model + action badge) + Include checkbox
        row1 = QtWidgets.QHBoxLayout()
        row1.setContentsMargins(0, 0, 0, 0)
        row1.setSpacing(8)

        action_color = "#3b82f6" if diff.action == "UPDATE" else "#10b981"
        title_lbl = QtWidgets.QLabel(
            f"<b style='color:{_CLR_TEXT};font-size:13px;'>Note {diff.note_id}</b>"
            f"<span style='color:{_CLR_SUBTEXT};font-size:11px;margin-left:6px;'>"
            f" · {_html.escape(diff.model_name)}</span>"
            f"<span style='color:{action_color};font-size:10px;font-weight:700;margin-left:6px;'>"
            f"[{diff.action}]</span>"
        )
        title_lbl.setTextFormat(_Qt.TextFormat.RichText)
        title_lbl.setWordWrap(True)
        row1.addWidget(title_lbl, stretch=1)

        self._include_cb = QtWidgets.QCheckBox("Inclure")
        self._include_cb.setStyleSheet(f"color: {_CLR_TEXT}; font-weight: bold; font-size: 11px;")
        self._include_cb.setChecked(not diff.excluded)
        self._include_cb.toggled.connect(self._on_include_toggled)
        row1.addWidget(self._include_cb)
        header_layout.addLayout(row1)

        # Row 2: Note-level actions (Copy, Keep old, Accept new)
        row2 = QtWidgets.QHBoxLayout()
        row2.setContentsMargins(0, 0, 0, 0)
        row2.setSpacing(6)

        btn_copy_all = QtWidgets.QPushButton("Tout copier (pré-maj)")
        btn_copy_all.setCursor(QtGui.QCursor(_Qt.CursorShape.PointingHandCursor))
        btn_copy_all.setToolTip("Copier tous les champs de la version pré-maj dans le presse-papier")
        btn_copy_all.setStyleSheet(
            f"QPushButton {{ background-color:transparent; color:{_CLR_SUBTEXT}; "
            f"border:1px solid {_CLR_BORDER}; border-radius:4px; padding:3px 6px; font-size:11px; font-weight:500; }}"
            f"QPushButton:hover {{ color:{_CLR_TEXT}; background-color:{_CLR_SEL}; border-color:{_CLR_BORDER}; }}"
        )
        def _copy_all():
            parts = []
            for fn in diff.field_names:
                v = diff.original_fields.get(fn, "")
                parts.append(f"--- {fn} ---\n{v}")
            app = QtWidgets.QApplication.instance()
            if app and app.clipboard():
                app.clipboard().setText("\n\n".join(parts))
                btn_copy_all.setText("Copié !")
                safe_timer_set_text(btn_copy_all, "Tout copier (pré-maj)", 1500)
        btn_copy_all.clicked.connect(_copy_all)
        row2.addWidget(btn_copy_all, stretch=1)

        btn_rej_all_note = QtWidgets.QPushButton("Tout garder")
        btn_rej_all_note.setCursor(QtGui.QCursor(_Qt.CursorShape.PointingHandCursor))
        btn_rej_all_note.setStyleSheet(_get_style_btn_reject())
        btn_rej_all_note.setMinimumHeight(24)
        if diff.action == "INSERT":
            btn_rej_all_note.setEnabled(False)
            btn_rej_all_note.setToolTip("Non applicable aux nouvelles cartes (aucun état antérieur)")
        else:
            btn_rej_all_note.setToolTip("Conserver la version pré-maj pour tous les champs de cette note")
            btn_rej_all_note.clicked.connect(self.reject_all_hunks)
        row2.addWidget(btn_rej_all_note, stretch=1)

        btn_acc_all_note = QtWidgets.QPushButton("Tout accepter")
        btn_acc_all_note.setCursor(QtGui.QCursor(_Qt.CursorShape.PointingHandCursor))
        btn_acc_all_note.setStyleSheet(_get_style_btn_accept())
        btn_acc_all_note.setMinimumHeight(24)
        btn_acc_all_note.setToolTip("Accepter les modifications d'import pour tous les champs de cette note")
        btn_acc_all_note.clicked.connect(self.accept_all_hunks)
        row2.addWidget(btn_acc_all_note, stretch=1)

        header_layout.addLayout(row2)
        layout.addWidget(header_widget)

        # Section Tags
        self._tags_panel = TagsDiffPanel(diff, parent=container)
        self._tags_panel.tagsChanged.connect(self._on_tags_changed)
        layout.addWidget(self._tags_panel)

        sep = QtWidgets.QFrame()
        sep.setFrameShape(QtWidgets.QFrame.Shape.HLine)
        sep.setStyleSheet(f"color:{_CLR_BORDER};")
        layout.addWidget(sep)
        layout.addSpacing(8)

        # Panneau de hunks pour chaque champ
        model_css = _get_model_css(diff.model_name)
        for field_name in diff.field_names:
            old_val = diff.original_fields.get(field_name, "")
            new_val = diff.updated_fields.get(field_name, "")
            is_protected = field_name in self._protected_fields

            fp = FieldDiffPanel(field_name, old_val, new_val,
                                model_css=model_css, protected=is_protected, diff=diff, parent=container)
            # Connecter les changements pour synchroniser l'éditeur
            fp.previewChanged.connect(
                lambda fn=field_name, fp_=fp: self._on_field_hunk_changed(fn, fp_)
            )
            self._field_panels[field_name] = fp
            fp.setEnabled(not diff.excluded)
            layout.addWidget(fp)

        # Message informatif si aucun champ texte n'a été modifié
        self._no_fields_changed_lbl = QtWidgets.QLabel("Aucun champ modifié à cette note.", container)
        self._no_fields_changed_lbl.setStyleSheet(
            f"color:{_CLR_SUBTEXT}; font-size:11px; font-style:italic; padding:12px; background:transparent;"
        )
        self._no_fields_changed_lbl.setAlignment(_Qt.AlignmentFlag.AlignCenter)
        self._no_fields_changed_lbl.setVisible(False)
        layout.addWidget(self._no_fields_changed_lbl)

        # Bouton tiroir pour afficher / masquer les champs inchangés
        self._unchanged_toggle_btn = QtWidgets.QPushButton(container)
        self._unchanged_toggle_btn.setObjectName("toggle_unchanged_fields_btn")
        self._unchanged_toggle_btn.setCursor(QtGui.QCursor(_Qt.CursorShape.PointingHandCursor))
        self._unchanged_toggle_btn.setStyleSheet(f"""
            QPushButton {{
                background-color: transparent;
                color: {_CLR_SUBTEXT};
                border: 1px dashed {_CLR_BORDER};
                border-radius: 4px;
                padding: 6px 12px;
                font-size: 11px;
                font-weight: 500;
                margin-top: 6px;
                margin-bottom: 6px;
            }}
            QPushButton:hover {{
                background-color: {_CLR_SEL};
                color: {_CLR_TEXT};
                border-style: solid;
            }}
        """)
        self._unchanged_toggle_btn.setVisible(False)
        self._unchanged_toggle_btn.clicked.connect(self._on_toggle_unchanged_clicked)
        layout.addWidget(self._unchanged_toggle_btn)

        layout.addStretch()
        self.setWidget(container)

        self._update_fields_visibility()

    def _on_toggle_unchanged_clicked(self) -> None:
        self._unchanged_expanded = not self._unchanged_expanded
        self._update_fields_visibility()

    def set_only_changed_filter(self, enabled: bool) -> None:
        self._only_changed = enabled
        self._unchanged_expanded = False
        self._update_fields_visibility()

    def _update_fields_visibility(self) -> None:
        changed_count = 0
        unchanged_count = 0
        for fp in self._field_panels.values():
            if fp.is_changed():
                changed_count += 1
                fp.setVisible(True)
            else:
                unchanged_count += 1
                if not self._only_changed or self._unchanged_expanded:
                    fp.setVisible(True)
                else:
                    fp.setVisible(False)

        # Message informatif si aucun champ n'a été modifié
        if self._only_changed and changed_count == 0:
            self._no_fields_changed_lbl.setVisible(True)
        else:
            self._no_fields_changed_lbl.setVisible(False)

        # Bouton tiroir pour les champs inchangés
        if self._only_changed and unchanged_count > 0:
            self._unchanged_toggle_btn.setVisible(True)
            if self._unchanged_expanded:
                txt = "Masquer le champ inchangé" if unchanged_count == 1 else f"Masquer les {unchanged_count} champs inchangés"
            else:
                txt = "+ Afficher le champ inchangé" if unchanged_count == 1 else f"+ Afficher les {unchanged_count} champs inchangés"
            self._unchanged_toggle_btn.setText(txt)
        else:
            self._unchanged_toggle_btn.setVisible(False)

    def _on_tags_changed(self) -> None:
        editor = self._browser_panel.get_editor_for_note(self._diff.note_id)
        if editor:
            sync_tags_to_editor(editor, self._diff.tags)
        self._browser_panel._update_status()

    def _on_field_edited_by_user(self, field_name: str, new_val: str) -> None:
        fp = self._field_panels.get(field_name)
        if fp:
            fp.on_editor_modified(new_val)
        self._update_fields_visibility()
        self._browser_panel._update_status()

    def set_editor(self, editor) -> None:
        """Enregistrer l'éditeur natif pour synchronisation."""
        if editor:
            nid = getattr(editor, "nid", None)
            note = _get_editor_note(editor)
            if nid == self._diff.note_id or (note and getattr(note, "id", None) == self._diff.note_id):
                self._editor = editor
                return
        self._editor = None

    def _sync_all_merged_then_editor(self) -> None:
        """Programme une sync débouncée vers l'éditeur (30 ms max de délai).
        Plusieurs appels rapprochés seront fusionnés en un seul."""
        if not self._sync_timer.isActive():
            self._sync_timer.start()

    def _do_sync_editor(self) -> None:
        """Exécute la synchronisation effective (appelé par le timer)."""
        for fname, fp in self._field_panels.items():
            if fname in self._diff.custom_edit_fields:
                self._diff.merged_fields[fname] = self._diff.custom_edit_fields[fname]
            else:
                self._diff.merged_fields[fname] = fp.get_merged_value()
        editor = self._browser_panel.get_editor_for_note(self._diff.note_id)
        if editor:
            self._editor = editor
            sync_editor_from_hunks(editor, self._diff)

    def _on_field_hunk_changed(self, field_name: str, fp: FieldDiffPanel) -> None:
        """Appele quand un hunk est accepte/rejete dans un champ."""
        if all(h.accepted for h in fp.hunks):
            self._diff.custom_edit_fields.pop(field_name, None)
        self._sync_all_merged_then_editor()
        self._update_fields_visibility()
        self._browser_panel._update_status()

    def _on_include_toggled(self, checked: bool) -> None:
        self._diff.excluded = not checked
        for fp in self._field_panels.values():
            fp.setEnabled(checked)
        
        editor = self._browser_panel.get_editor_for_note(self._diff.note_id)
        if editor:
            self._editor = editor
            sync_editor_from_hunks(editor, self._diff)
            
        self._browser_panel._update_status()

    def accept_all_hunks(self) -> None:
        """Accepte tous les hunks de tous les champs et synce l'editeur une seule fois."""
        self._include_cb.setChecked(True)
        self._diff.custom_edit_fields.clear()
        self._diff.excluded = False
        for fp in self._field_panels.values():
            fp.active_target = "PROP"
            self._diff.field_active_target[fp.field_name] = "PROP"
            fp.prop_mod = fp.prop_orig
            for h in fp.hunks:
                h.accepted = True
            for hw in fp._hunk_widgets:
                hw.hunk.accepted = True
                hw._refresh_state()
            if hasattr(fp, "_refresh_display"):
                fp._refresh_display()
            elif hasattr(fp, "_update_badge_and_buttons"):
                fp._update_badge_and_buttons()
        for fname in self._diff.field_names:
            fp = self._field_panels.get(fname)
            if fp is not None:
                self._diff.merged_fields[fname] = fp.get_merged_value()
            else:
                self._diff.merged_fields[fname] = self._diff.updated_fields.get(fname, "")

        self._diff.tags = list(self._diff.updated_tags)
        self._diff.tags_active_target = "PROP"
        if hasattr(self, "_tags_panel") and self._tags_panel:
            self._tags_panel.refresh()

        self._do_sync_editor()
        self._browser_panel._update_status()

    def accept_tags_only(self) -> None:
        """Accepte uniquement les modifications de tags sans impacter les choix faits dans les champs."""
        if self._diff.excluded:
            self._include_cb.setChecked(True)
            self._diff.excluded = False
        self._diff.tags = list(self._diff.updated_tags)
        self._diff.tags_active_target = "PROP"
        if hasattr(self, "_tags_panel") and self._tags_panel:
            self._tags_panel.refresh()

        editor = self._browser_panel.get_editor_for_note(self._diff.note_id)
        if editor:
            sync_tags_to_editor(editor, self._diff.tags)
        self._browser_panel._update_status()

    def reject_all_hunks(self) -> None:
        """Rejette tous les hunks de tous les champs et synce l'éditeur une seule fois."""
        # Sécurité : une carte neuve (INSERT) n'a pas d'état antérieur ("OLD"). Rejeter ne doit avoir aucun effet.
        if getattr(self._diff, "action", "") == "INSERT":
            return
        self._include_cb.setChecked(True)
        for fp in self._field_panels.values():
            fp.active_target = "OLD"
            self._diff.field_active_target[fp.field_name] = "OLD"
            fp.old_mod = fp.old_orig
            for h in fp.hunks:
                h.accepted = False
            for hw in fp._hunk_widgets:
                hw.hunk.accepted = False
                hw._refresh_state()
            if hasattr(fp, "_refresh_display"):
                fp._refresh_display()
            elif hasattr(fp, "_update_badge_and_buttons"):
                fp._update_badge_and_buttons()
        for fname in self._diff.field_names:
            if fname in self._diff.custom_edit_fields:
                self._diff.merged_fields[fname] = self._diff.custom_edit_fields[fname]
            else:
                fp = self._field_panels.get(fname)
                if fp is not None:
                    self._diff.merged_fields[fname] = fp.get_merged_value()
                else:
                    self._diff.merged_fields[fname] = self._diff.original_fields.get(fname, "")

        self._diff.tags = list(self._diff.original_tags)
        self._diff.tags_active_target = "OLD"
        if hasattr(self, "_tags_panel") and self._tags_panel:
            self._tags_panel.refresh()

        self._do_sync_editor()
        self._browser_panel._update_status()


    def get_merged_fields(self) -> Dict[str, str]:
        result = {}
        for name, fp in self._field_panels.items():
            if name in self._diff.custom_edit_fields:
                result[name] = self._diff.custom_edit_fields[name]
            else:
                result[name] = fp.get_merged_value()
        return result

    def get_field_panels(self) -> Dict[str, FieldDiffPanel]:
        return self._field_panels


_editor_note_cache: Dict[int, dict] = {}
_editor_last_snapshot: Dict[int, tuple[float, str]] = {}  # note_id -> (timestamp, version_id)

_orig_editor_save_current_note = None


def _diff_save_current_note(editor_self) -> None:
    """
    Remplacement de Editor._save_current_note pendant la session de diff.
    Effectue une mise à jour synchrone directe en base SQLite (0.1ms) uniquement
    pour les notes appartenant à la session de diff active.
    Délègue toutes les autres notes (AddCards, EditCurrent, notes hors diff) à la
    méthode d'origine d'Anki.
    """
    if not editor_self.note:
        return
    note_id = getattr(editor_self.note, "id", 0)
    if _browser_panel is not None and _pending_diffs and get_diff_for_note(note_id) is not None:
        try:
            col = getattr(editor_self, "mw", None) and getattr(editor_self.mw, "col", None)
            if col:
                col.update_note(editor_self.note)
            parent_win = getattr(editor_self, "parentWindow", None)
            browser = parent_win if hasattr(parent_win, "_previewer") else None
            if browser and hasattr(browser, "_previewer") and browser._previewer:
                browser._previewer._last_state = None
                browser._renderPreview()
            return
        except Exception:
            pass
    if _orig_editor_save_current_note:
        _orig_editor_save_current_note(editor_self)


def _patch_editor_save_for_diff() -> None:
    global _orig_editor_save_current_note
    try:
        from aqt.editor import Editor
        if hasattr(Editor, "_save_current_note") and _orig_editor_save_current_note is None:
            _orig_editor_save_current_note = Editor._save_current_note
            Editor._save_current_note = _diff_save_current_note
    except Exception:
        pass


def _restore_editor_save() -> None:
    global _orig_editor_save_current_note
    try:
        from aqt.editor import Editor
        if _orig_editor_save_current_note is not None:
            Editor._save_current_note = _orig_editor_save_current_note
            _orig_editor_save_current_note = None
    except Exception:
        pass





def _on_editor_did_load_note(editor) -> None:
    """
    Appelé par Anki quand une note est chargée dans l'éditeur.
    1. Met en cache l'état initial des champs en mémoire
    2. Notifie le browser panel de sélectionner et afficher cette note immédiatement.
    """
    global _browser_panel, _editor_note_cache
    note = getattr(editor, "note", None)
    if note is None:
        return

    # Cache note state for unfocus tracking (in-memory only, no disk I/O)
    try:
        _editor_note_cache[note.id] = {
            'fields': {name: val for name, val in note.items()},
            'tags': list(note.tags) if hasattr(note, "tags") and note.tags else [],
            'mod': getattr(note, "mod", None),
            'timestamp': time.time()
        }
    except Exception:
        pass

    # Synchroniser immédiatement le panneau latéral avec la note active dans l'éditeur si diff actif
    if _browser_panel is not None:
        try:
            from aqt.qt import sip
            if not sip.isdeleted(_browser_panel):
                _browser_panel.set_current_editor(editor)
                if getattr(_browser_panel, "_current_nid", None) != note.id:
                    _browser_panel.select_note(note.id)
        except Exception:
            pass

    if _browser_panel is not None and _pending_diffs:
        if editor:
            _inject_editor_web_shortcuts(editor)


def _on_editor_did_unfocus_field(changed: bool, note_or_editor, current_field_idx: int) -> bool:
    """
    Appelé par Anki quand l'utilisateur quitte un champ de saisie dans l'éditeur.
    Compare l'état actuel des champs avec le cache mémoire pour détecter les changements
    et enregistre un snapshot d'historique de manière 100% asynchrone (0ms freeze UI).
    """
    note = _get_editor_note(note_or_editor)
    if note is None:
        note = note_or_editor

    if note is None or not hasattr(note, "id") or not note.id:
        return changed

    # Ne pas enregistrer de snapshot temporaire polluant si la note est en cours de revue de diff non committée
    global _pending_diffs, _committed
    if not _committed and _pending_diffs and (note.id in _pending_diffs or note.id in _staged_to_orig):
        return changed

    try:
        global _editor_note_cache, _editor_last_snapshot
        cached = _editor_note_cache.get(note.id)
        current_fields = {name: val for name, val in note.items()}

        if not cached:
            _editor_note_cache[note.id] = {
                'fields': current_fields,
                'timestamp': time.time()
            }
            return changed

        old_fields = cached['fields']
        has_changes = any(old_fields.get(k) != v for k, v in current_fields.items()) or any(k not in old_fields for k in current_fields)

        if has_changes:
            from .history import NoteHistoryDB
            db = NoteHistoryDB.instance()
            now_ts = time.time()
            tags = list(note.tags) if hasattr(note, "tags") and note.tags else []

            # 1. Garantir que l'état initial (avant modification dans cette session)
            # est persisté comme baseline immuable dans l'historique
            try:
                db.ensure_baseline_snapshot(
                    note_id=note.id,
                    fields=old_fields,
                    tags=cached.get('tags', tags),
                    mod_time=cached.get('mod'),
                )
            except Exception as e_base:
                print(f"[anki-diff] Error ensuring baseline: {e_base}")

            # Mettre à jour le cache mémoire
            _editor_note_cache[note.id] = {
                'fields': current_fields,
                'tags': tags,
                'mod': cached.get('mod'),
                'timestamp': now_ts,
            }

            dedup_window = 900.0  # 15 minutes (durée d'une séance d'édition continue)
            last_info = _editor_last_snapshot.get(note.id)

            if last_info and (now_ts - last_info[0]) < dedup_window:
                last_time, last_vid = last_info
                # Mise à jour en place du snapshot de la séance active (zéro version intermédiaire)
                db.update_snapshot_fields_async(
                    version_id=last_vid,
                    fields=current_fields,
                    comment="Sauvegarde automatique (édition)",
                    tags=tags,
                )
                _editor_last_snapshot[note.id] = (now_ts, last_vid)
            else:
                import uuid
                new_vid = uuid.uuid4().hex
                _editor_last_snapshot[note.id] = (now_ts, new_vid)
                db.store_snapshot_async(
                    note_id=note.id,
                    fields=current_fields,
                    source="manual",
                    comment="Sauvegarde automatique (édition)",
                    tags=tags,
                    version_id=new_vid,
                )

    except Exception as e:
        print(f"[anki-diff] Error in editor did unfocus field history tracker: {e}")

    return changed


def _handle_editor_escape(editor: Any) -> None:
    """
    Gere la touche Echap pressee dans l'editeur (y compris quand un champ a le focus actif).
    - En mode diff : demande confirmation via le dialogue dedie pour eviter toute perte de donnees.
    - En edition normale : ferme proprement la fenetre parente de l'editeur (EditCurrent, AddCards, etc.).
    """
    global _browser_panel, _pending_diffs
    # 1. Mode diff actif avec diffs en attente : notification et confirmation OBLIGATOIRES
    if _browser_panel is not None and bool(_pending_diffs):
        try:
            from aqt.qt import sip
            try:
                is_del = sip.isdeleted(_browser_panel)
            except (TypeError, ValueError):
                is_del = False
            if not is_del:
                _browser_panel._on_request_close_panel()
                return
        except Exception as e:
            print(f"[anki-diff] Error in diff escape handling: {e}")
            return

    # 2. Mode normal : fermer proprement la fenetre de l'editeur
    try:
        from aqt import mw
        from aqt.qt import QDialog, QMainWindow, QDialogButtonBox

        parent_win = getattr(editor, "parentWindow", None)
        if not parent_win:
            web = getattr(editor, "web", None)
            if web:
                try:
                    parent_win = web.window()
                except Exception:
                    pass
        if not parent_win and hasattr(editor, "widget"):
            try:
                parent_win = editor.widget.window()
            except Exception:
                pass

        if not parent_win or (mw is not None and parent_win is mw):
            if hasattr(editor, "web") and editor.web:
                editor.web.onEsc()
            return

        # Dans le Browser, Escape doit seulement retirer le focus de l'editeur (pas fermer le Browser entier)
        win_class_name = parent_win.__class__.__name__
        if win_class_name == "Browser":
            if hasattr(editor, "saveNow"):
                editor.saveNow(lambda: None)
            if hasattr(parent_win, "table") and hasattr(parent_win.table, "setFocus"):
                parent_win.table.setFocus()
            elif hasattr(editor, "web") and editor.web:
                editor.web.onEsc()
            return

        # Fenetre EditCurrent ("Modifier la carte en cours")
        if win_class_name in ("EditCurrent", "NewEditCurrent"):
            # Si le bouton Close du buttonBox existe, le declencher
            if hasattr(parent_win, "form") and hasattr(parent_win.form, "buttonBox"):
                close_btn = parent_win.form.buttonBox.button(QDialogButtonBox.StandardButton.Close)
                if close_btn:
                    close_btn.click()
                    return
            parent_win.close()
            return

        # Fenetre AddCards ("Ajouter") ou autre fenetre avec closeWithCallback
        if hasattr(parent_win, "closeWithCallback") and callable(parent_win.closeWithCallback):
            parent_win.closeWithCallback(parent_win.close)
            if hasattr(parent_win.close, "called") and not parent_win.close.called:
                parent_win.close()
            return

        # QDialog standard
        if isinstance(parent_win, QDialog):
            parent_win.reject()
            return

        # QMainWindow ou autre widget fenetre
        parent_win.close()
    except Exception as e:
        print(f"[anki-diff] Error closing editor window on Escape: {e}")



def _on_editor_did_init(editor) -> None:
    """Patch la commande bridge pour intercepter les commandes diff_."""
    original_bridge = editor.onBridgeCmd

    def patched_bridge(cmd: str):
        # Only intercept specific import diff commands. Other commands like diff_history_cmd
        # must fall back to the original bridge handler.
        if cmd in ("diff_accept_note", "diff_reject_note", "diff_commit_all", "diff_previous_card", "diff_next_card", "diff_accept_tags_only"):
            _handle_editor_bridge(editor, cmd)
            return
        elif cmd == "diff_editor_escape":
            _handle_editor_escape(editor)
            return
        return original_bridge(cmd)

    editor.onBridgeCmd = patched_bridge
    try:
        editor.web.set_bridge_command(patched_bridge, editor)
    except Exception:
        pass
    _inject_editor_web_shortcuts(editor)


def _inject_editor_web_shortcuts(editor: Any) -> None:
    """Injecte un ecouteur JavaScript keydown dans le webview de l'editeur pour capturer F2, F3, Alt+A, Alt+R et Echap."""
    global _pending_diffs, _browser_panel
    if not editor:
        return
    web = getattr(editor, "web", None)
    if not web:
        return

    is_diff_active = "true" if bool(_pending_diffs and _browser_panel is not None) else "false"

    js = f"""
    (function() {{
        window._anki_diff_active = {is_diff_active};
        if (window._anki_diff_shortcuts_installed_v2) return;
        window._anki_diff_shortcuts_installed_v2 = true;

        function isFieldFocused() {{
            var el = document.activeElement;
            if (!el) return false;
            if (el.isContentEditable) return true;
            if (el.tagName === "INPUT" || el.tagName === "TEXTAREA") return true;
            if (typeof el.closest === "function" && el.closest("[contenteditable='true'], anki-editable")) return true;
            if (el.shadowRoot && el.shadowRoot.activeElement) {{
                var sEl = el.shadowRoot.activeElement;
                if (sEl.isContentEditable || sEl.tagName === "INPUT" || sEl.tagName === "TEXTAREA") return true;
            }}
            return false;
        }}

        function isOverlayVisible() {{
            var elements = document.querySelectorAll(".mathjax-overlay, [role='menu'], [role='listbox'], .popover, .floating");
            for (var i = 0; i < elements.length; i++) {{
                var el = elements[i];
                if (el.offsetParent !== null || (window.getComputedStyle && window.getComputedStyle(el).display !== "none")) {{
                    var rect = el.getBoundingClientRect();
                    if (rect.width > 0 && rect.height > 0) {{
                        return true;
                    }}
                }}
            }}
            return false;
        }}

        function sendBridge(cmd) {{
            if (typeof window.bridgeCommand === "function") {{
                window.bridgeCommand(cmd);
            }} else if (typeof bridgeCommand === "function") {{
                bridgeCommand(cmd);
            }} else if (typeof pycmd === "function") {{
                pycmd(cmd);
            }}
        }}

        function onKey(e) {{
            var key = e.key;
            var hasAlt = e.altKey;
            var hasCtrl = e.ctrlKey || e.metaKey;
            var hasShift = e.shiftKey;

            // Touche Echap : fermer l'editeur (ou demander confirmation en mode diff)
            // S'applique meme si un champ est encadre en bleu (focus actif)
            if (key === "Escape" && !hasAlt && !hasCtrl && !hasShift) {{
                if (isOverlayVisible()) {{
                    return;
                }}
                e.preventDefault();
                e.stopPropagation();
                sendBridge("diff_editor_escape");
                return;
            }}

            // Raccourcis exclusifs à la revue de diff : inactifs en mode explorateur / édition normale
            if (!window._anki_diff_active) {{
                return;
            }}

            // Touches F2, F3 et F4 : TOUJOURS actives en mode diff, peu importe la cible ou le focus
            if (key === "F2" && !hasAlt && !hasCtrl && !hasShift) {{
                e.preventDefault();
                e.stopPropagation();
                sendBridge("diff_accept_note");
                return;
            }}
            if (key === "F3" && !hasAlt && !hasCtrl && !hasShift) {{
                e.preventDefault();
                e.stopPropagation();
                sendBridge("diff_reject_note");
                return;
            }}
            if (key === "F4" && !hasAlt && !hasCtrl && !hasShift) {{
                e.preventDefault();
                e.stopPropagation();
                sendBridge("diff_accept_tags_only");
                return;
            }}

            // Ne jamais intercepter les autres raccourcis (Alt+A, Alt+R, flèches) si le focus se trouve dans un champ editable
            if (isFieldFocused()) {{
                return;
            }}

            // Flèches haut / bas pour naviguer entre les cartes du diff quand aucun champ n'est en cours d'édition
            if (key === "ArrowUp" && !hasAlt && !hasCtrl && !hasShift) {{
                e.preventDefault();
                e.stopPropagation();
                sendBridge("diff_previous_card");
                return;
            }}
            if (key === "ArrowDown" && !hasAlt && !hasCtrl && !hasShift) {{
                e.preventDefault();
                e.stopPropagation();
                sendBridge("diff_next_card");
                return;
            }}

            var isAccept = (
                (hasAlt && !hasCtrl && !hasShift && (key === "a" || key === "A" || key === "v" || key === "V" || key === "y" || key === "Y")) ||
                (hasCtrl && hasAlt && (key === "a" || key === "A")) ||
                (hasCtrl && hasShift && (key === "a" || key === "A"))
            );

            var isReject = (
                (hasAlt && !hasCtrl && !hasShift && (key === "r" || key === "R" || key === "n" || key === "N")) ||
                (hasCtrl && hasAlt && (key === "r" || key === "R")) ||
                (hasCtrl && hasShift && (key === "r" || key === "R"))
            );

            var isAcceptTags = (
                (hasAlt && hasShift && !hasCtrl && (key === "t" || key === "T")) ||
                (hasCtrl && hasAlt && hasShift && (key === "t" || key === "T"))
            );

            if (isAccept) {{
                e.preventDefault();
                e.stopPropagation();
                sendBridge("diff_accept_note");
            }} else if (isReject) {{
                e.preventDefault();
                e.stopPropagation();
                sendBridge("diff_reject_note");
            }} else if (isAcceptTags) {{
                e.preventDefault();
                e.stopPropagation();
                sendBridge("diff_accept_tags_only");
            }}
        }}

        window.addEventListener("keydown", onKey, true);
        document.addEventListener("keydown", onKey, true);
    }})();
    """
    try:
        web.eval(js)
    except Exception:
        pass


def _on_editor_did_init_shortcuts(cuts: List[Any], editor: Any) -> None:
    """
    Enregistre les raccourcis diff directement dans l'editeur.
    Le 3eme element du tuple (True) indique a Anki d'activer le raccourci meme si aucun champ n'est selectionne.
    NOTE CRITIQUE : Ne JAMAIS enregistrer les touches Up/Down dans 'cuts' de l'éditeur car QShortcut
    capture les touches au niveau de toute la fenêtre (WindowShortcut) et bloque la navigation par
    flèches dans l'explorateur normal et sa liste de cartes.
    """
    def _accept_action(force: bool = False):
        global _browser_panel, _pending_diffs
        if _browser_panel and _pending_diffs:
            if force or not _is_inside_field(_browser_panel):
                _browser_panel._accept_all_current()

    def _reject_action(force: bool = False):
        global _browser_panel, _pending_diffs
        if _browser_panel and _pending_diffs:
            if force or not _is_inside_field(_browser_panel):
                _browser_panel._reject_all_current()

    def _accept_tags_action(force: bool = False):
        global _browser_panel, _pending_diffs
        if _browser_panel and _pending_diffs:
            if force or not _is_inside_field(_browser_panel):
                _browser_panel._accept_tags_only_current()

    def _copy_nid_action():
        """Copie le NID de la carte en cours d'edition dans le presse-papiers.
        Delegue a Lier les cartes si disponible, sinon copie le NID brut directement."""
        browser = getattr(editor, "parentWindow", None) or getattr(_browser_panel, "_browser", None)
        _trigger_copy_nid(_browser_panel, browser)

    # F2 et F3 : TOUJOURS actifs peu importe le focus dans l'editeur
    cuts.append(("F2", lambda: _accept_action(force=True), True))
    cuts.append(("F3", lambda: _reject_action(force=True), True))
    cuts.append(("F4", lambda: _accept_tags_action(force=True), True))

    # Accepter (lettres/Alt) : Alt+A, Alt+V, Ctrl+Alt+A, Alt+Y, Ctrl+Shift+A
    for key in ("Alt+A", "Alt+V", "Ctrl+Alt+A", "Alt+Y", "Ctrl+Shift+A"):
        cuts.append((key, lambda: _accept_action(force=False), True))

    # Rejeter / Garder l'ancien (lettres/Alt) : Alt+R, Ctrl+Alt+R, Alt+N, Ctrl+Shift+R
    for key in ("Alt+R", "Ctrl+Alt+R", "Alt+N", "Ctrl+Shift+R"):
        cuts.append((key, lambda: _reject_action(force=False), True))

    # Accepter uniquement les tags : Alt+Shift+T, Ctrl+Alt+Shift+T
    for key in ("Alt+Shift+T", "Ctrl+Alt+Shift+T"):
        cuts.append((key, lambda: _accept_tags_action(force=False), True))

    # Copier NID : uniquement si Lier les cartes n'est pas déjà présent pour éviter le doublon ambigu
    has_lier_cartes = False
    try:
        import sys
        if any("Anki_EDN_Lier_les_cartes" in m or "linked_cards" in m for m in sys.modules):
            has_lier_cartes = True
    except Exception:
        pass
    if not has_lier_cartes:
        cuts.append(("Ctrl+Alt+C", _copy_nid_action, True))


def _handle_editor_bridge(editor, cmd: str) -> None:
    """Gere les commandes diff_ depuis le JS de l'editeur."""
    note = getattr(editor, "note", None)
    nid = note.id if note else None

    if cmd == "diff_previous_card":
        if _browser_panel:
            _browser_panel._select_previous_card()

    elif cmd == "diff_next_card":
        if _browser_panel:
            _browser_panel._select_next_card()

    elif cmd == "diff_accept_note":
        if _browser_panel:
            _browser_panel._accept_all_current()
        elif nid:
            real_nid = _staged_to_orig.get(nid, nid)
            diff = _pending_diffs.get(real_nid)
            if diff:
                diff.excluded = False
                diff.tags = list(diff.updated_tags)
                diff.tags_active_target = "PROP"
                for fn in diff.field_names:
                    diff.merged_fields[fn] = diff.updated_fields.get(fn, "")

    elif cmd == "diff_reject_note":
        if _browser_panel:
            _browser_panel._reject_all_current()
        elif nid:
            real_nid = _staged_to_orig.get(nid, nid)
            diff = _pending_diffs.get(real_nid)
            if diff and getattr(diff, "action", "") != "INSERT":
                diff.excluded = True
                diff.tags = list(diff.original_tags)
                diff.tags_active_target = "OLD"

    elif cmd == "diff_accept_tags_only":
        if _browser_panel:
            _browser_panel._accept_tags_only_current()
        elif nid:
            real_nid = _staged_to_orig.get(nid, nid)
            diff = _pending_diffs.get(real_nid)
            if diff:
                if diff.excluded:
                    diff.excluded = False
                diff.tags = list(diff.updated_tags)
                diff.tags_active_target = "PROP"
                sync_tags_to_editor(editor, diff.tags)

    elif cmd == "diff_commit_all":
        if _browser_panel:
            _browser_panel._on_commit_all()
        elif _on_commit_cb:
            accepted = [d for d in _pending_diffs.values() if not d.excluded]
            _on_commit_cb(accepted)
            clear_pending_diffs()


def _on_editor_will_show_context_menu(editor_webview, menu) -> None:
    """Ajoute les actions diff au menu contextuel de l'éditeur."""
    global _browser_panel, _pending_diffs
    if _browser_panel is None:
        return
    try:
        editor = getattr(editor_webview, "editor", None)
        note = _get_editor_note(editor)
        if not note or note.id not in _pending_diffs:
            return
    except Exception:
        return

    diff = _pending_diffs[note.id]
    menu.addSeparator()

    accept_act = menu.addAction("Accepter tous les changements de cette note")
    accept_act.triggered.connect(
        lambda: _handle_editor_bridge(editor, "diff_accept_note")
    )
    reject_act = menu.addAction("Rejeter tous les changements de cette note")
    reject_act.triggered.connect(
        lambda: _handle_editor_bridge(editor, "diff_reject_note")
    )
    accept_tags_act = menu.addAction("Accepter uniquement les tags de cette note")
    accept_tags_act.triggered.connect(
        lambda: _handle_editor_bridge(editor, "diff_accept_tags_only")
    )
    commit_act = menu.addAction("Committer tous les changements en attente")
    commit_act.triggered.connect(
        lambda: _handle_editor_bridge(editor, "diff_commit_all")
    )


# ---------------------------------------------------------------------------
# Point d'entrée public
# ---------------------------------------------------------------------------

def _is_panel_alive(panel) -> bool:
    """Vérifie si le panel de diff existe encore côté C++ et est visible."""
    if panel is None:
        return False
    try:
        from aqt.qt import sip
        if hasattr(sip, "isdeleted"):
            try:
                if sip.isdeleted(panel):
                    return False
            except TypeError:
                pass
        if hasattr(panel, "isVisible"):
            return bool(panel.isVisible())
        return True
    except Exception:
        return False


def is_diff_session_active() -> bool:
    """Vérifie si une session de diff est active et en attente de révision."""
    global _browser_panel, _pending_diffs, _committed
    return bool(_browser_panel is not None and _is_panel_alive(_browser_panel) and _pending_diffs and not _committed)


def get_active_browser_panel() -> Optional[DiffBrowserPanel]:
    """Retourne l'instance active du panneau de diff dans le browser si vivante."""
    global _browser_panel
    if _browser_panel is not None and _is_panel_alive(_browser_panel):
        return _browser_panel
    return None


def open_browser_diff(diffs: List[NoteDiff], on_commit) -> None:
    """
    Ouvre l'explorateur Anki filtré sur les notes modifiées et attache
    le DiffBrowserPanel. C'est le point d'entrée principal du workflow diff.

    Les notes INSERT sont pré-insérées temporairement dans la collection pour
    être visibles dans le browser. Elles sont supprimées si l'utilisateur annule.
    """
    global _browser_panel, _pending_diffs, _on_commit_cb

    if not diffs:
        return

    incoming_nids = set(d.note_id for d in diffs)

    # Vérifier si le panel existe encore côté C++ (peut être détruit si le browser a été fermé)
    _panel_alive = _is_panel_alive(_browser_panel)
    if not _panel_alive:
        _browser_panel = None

    # 1. Garde contre les appels concurrents / doublons :
    # Si une session de diff est déjà active et visible dans l'explorateur
    if _panel_alive and _pending_diffs:
        current_nids = set(_pending_diffs.keys())
        if incoming_nids and incoming_nids.issubset(current_nids):
            print("[anki-diff] open_browser_diff: appel doublon ignoré, conservation de la session active.")
            try:
                browser = getattr(_browser_panel, "_browser", None) or aqt.dialogs.open("Browser", aqt.mw)
                browser.activateWindow()
                browser.raise_()
            except Exception:
                pass
            return

        # S'il s'agit de nouvelles notes complémentaires (ex. pull AnkiCollab multi-paquets) :
        # Fusionner dans la session active sans détruire l'état de revue de l'utilisateur
        new_diffs = [d for d in diffs if d.note_id not in _pending_diffs]
        if new_diffs:
            print(f"[anki-diff] open_browser_diff: fusion de {len(new_diffs)} nouvelles notes dans la session active.")
            for d in new_diffs:
                _pending_diffs[d.note_id] = d
            _stage_insert_notes(new_diffs)

            # Chaînage des callbacks de commit
            old_cb = _on_commit_cb
            new_cb = on_commit

            def chained_on_commit(all_accepted):
                if old_cb:
                    try:
                        old_cb([d for d in all_accepted if d.note_id in current_nids])
                    except Exception as e_old:
                        print(f"[anki-diff] Erreur callback commit précédent: {e_old}")
                if new_cb:
                    try:
                        new_cb([d for d in all_accepted if d.note_id in incoming_nids])
                    except Exception as e_new:
                        print(f"[anki-diff] Erreur callback commit nouveau: {e_new}")

            _on_commit_cb = chained_on_commit

            search_nids = []
            for d in _pending_diffs.values():
                if d.action == "INSERT" and d.staged_note_id:
                    search_nids.append(d.staged_note_id)
                elif d.action != "INSERT":
                    search_nids.append(d.note_id)
            if not search_nids:
                search_nids = list(_pending_diffs.keys())
            search_str = "nid:" + ",".join(str(nid) for nid in search_nids)

            browser = getattr(_browser_panel, "_browser", None) or aqt.dialogs.open("Browser", aqt.mw)
            browser.search_for(search_str)
            _browser_panel._update_status()
            browser.activateWindow()
            browser.raise_()
            return

    # 2. Si un ancien panel était vivant mais doit être réinitialisé :
    # Nettoyer l'ancienne session AVANT d'enregistrer les nouveaux diffs
    if _panel_alive and _browser_panel is not None:
        try:
            _browser_panel.cleanup()
            _browser_panel.close()
        except Exception:
            pass
        _browser_panel = None

    # 3. Enregistrer les nouveaux diffs et pré-insérer les notes INSERT
    set_pending_diffs(diffs, on_commit)
    _stage_insert_notes(diffs)

    # Construire le filtre : utiliser les staged_note_id pour les INSERT, note_id pour le reste
    search_nids = []
    for d in diffs:
        if d.action == "INSERT" and d.staged_note_id:
            search_nids.append(d.staged_note_id)
        elif d.action != "INSERT":
            search_nids.append(d.note_id)
    if not search_nids:
        search_nids = [d.note_id for d in diffs]  # fallback

    search_str = "nid:" + ",".join(str(nid) for nid in search_nids)

    browser = aqt.dialogs.open("Browser", aqt.mw)
    browser.search_for(search_str)

    _browser_panel = DiffBrowserPanel(browser)

    editor = getattr(browser, "editor", None)
    if editor:
        _inject_editor_web_shortcuts(editor)

    # Intercepter la fermeture de la fenetre du navigateur (bouton X en haut)
    if getattr(browser, "_orig_diff_close_event", None) is None:
        browser._orig_diff_close_event = browser.closeEvent
    _orig_browser_close = browser._orig_diff_close_event

    def _diff_browser_close(event):
        global _browser_panel, _committed, _pending_diffs
        if _browser_panel is not None and not _committed and bool(_pending_diffs):
            decision = prompt_diff_close_decision(browser)
            if decision == "cancel":
                if event is not None:
                    event.ignore()
                return
            elif decision == "commit":
                try:
                    from aqt.qt import sip
                    if not sip.isdeleted(_browser_panel):
                        _browser_panel.apply_commit(close_browser=False)
                except Exception as e:
                    print(f"[anki-diff] Error committing on close: {e}")
            elif decision == "revert":
                try:
                    from aqt.qt import sip
                    if not sip.isdeleted(_browser_panel):
                        _browser_panel.apply_rollback(close_browser=False)
                except Exception as e:
                    print(f"[anki-diff] Error reverting on close: {e}")

        if _browser_panel is not None:
            try:
                from aqt.qt import sip
                if not sip.isdeleted(_browser_panel):
                    _browser_panel.cleanup()
            except Exception:
                pass
            _browser_panel = None
        clear_pending_diffs()
        if _orig_browser_close:
            try:
                _orig_browser_close(event)
            except Exception:
                pass
    browser.closeEvent = _diff_browser_close

    # Intercepter Echap via keyPressEvent du browser.
    # Le browser Anki gere Echap dans son keyPressEvent natif en appelant self.close()
    # directement, ce qui court-circuite le closeEvent patche ci-dessus et l'event filter
    # global, provoquant une fermeture sans confirmation.
    if getattr(browser, "_orig_diff_key_press_event", None) is None:
        browser._orig_diff_key_press_event = browser.keyPressEvent
    _orig_browser_key_press = browser._orig_diff_key_press_event

    def _diff_browser_key_press(event):
        global _browser_panel, _committed, _pending_diffs
        if (event is not None
                and event.key() == QtCore.Qt.Key.Key_Escape
                and not event.modifiers()
                and _browser_panel is not None
                and not _committed
                and bool(_pending_diffs)):
            # Ne pas intercepter Echap si une QDialog enfant est active
            # (ex: dialogues de lien miroir de Lier les cartes, LinkedCardsDialog, etc.)
            app = QtWidgets.QApplication.instance()
            active_win = app.activeWindow() if app else None
            if (active_win is not None
                    and active_win is not browser
                    and (isinstance(active_win, QtWidgets.QDialog) or hasattr(active_win, "reject"))):
                if _orig_browser_key_press:
                    try:
                        _orig_browser_key_press(event)
                    except Exception:
                        pass
                return
            decision = prompt_diff_close_decision(browser)
            if decision == "cancel":
                event.accept()
                return
            elif decision == "commit":
                try:
                    from aqt.qt import sip
                    if not sip.isdeleted(_browser_panel):
                        _browser_panel.apply_commit(close_browser=False)
                except Exception as e:
                    print(f"[anki-diff] Error committing on Escape: {e}")
            elif decision == "revert":
                try:
                    from aqt.qt import sip
                    if not sip.isdeleted(_browser_panel):
                        _browser_panel.apply_rollback(close_browser=False)
                except Exception as e:
                    print(f"[anki-diff] Error reverting on Escape: {e}")
            event.accept()
            return
        if _orig_browser_key_press:
            try:
                _orig_browser_key_press(event)
            except Exception:
                pass
    browser.keyPressEvent = _diff_browser_key_press

    browser.activateWindow()
    browser.raise_()

_decision_hook = None  # Hook de test
_is_prompting_decision = False
_last_prompt_decision_time = 0.0


def prompt_diff_close_decision(parent=None) -> str:
    """
    Affiche une notification sobre demandant a l'utilisateur s'il souhaite
    conserver les modifications effectuees ou revenir a l'etat anterieur a l'importation.
    Retourne 'commit', 'revert' ou 'cancel'.
    """
    global _decision_hook, _is_prompting_decision, _last_prompt_decision_time
    if _decision_hook is not None:
        return _decision_hook()

    if os.environ.get("QT_QPA_PLATFORM") == "offscreen" or os.environ.get("PYTEST_CURRENT_TEST"):
        return "revert"

    now = time.monotonic()
    if _is_prompting_decision or (now - _last_prompt_decision_time < 0.6):
        return "cancel"

    _is_prompting_decision = True
    _last_prompt_decision_time = now
    try:
        dialog = QtWidgets.QDialog(parent)
        dialog.setWindowTitle("Fermeture de la revue des differences")
        dialog.setModal(True)
        dialog.resize(420, 160)
        try:
            from .ui import _get_style_base
            dialog.setStyleSheet(_get_style_base())
        except Exception:
            pass

        layout = QtWidgets.QVBoxLayout(dialog)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(12)

        title_lbl = QtWidgets.QLabel("Des modifications d'importation sont en attente.")
        title_lbl.setStyleSheet(f"font-size: 13px; font-weight: 700; color: {_CLR_HDR_FG};")
        layout.addWidget(title_lbl)

        info_lbl = QtWidgets.QLabel(
            "Souhaitez-vous conserver les modifications effectuees dans votre collection "
            "ou revenir a l'etat anterieur a l'importation ?"
        )
        info_lbl.setWordWrap(True)
        info_lbl.setStyleSheet(f"font-size: 12px; color: {_CLR_TEXT};")
        layout.addWidget(info_lbl)

        btn_layout = QtWidgets.QVBoxLayout()
        btn_layout.setSpacing(8)

        btn_keep = QtWidgets.QPushButton("Conserver les modifications")
        btn_keep.setCursor(QtGui.QCursor(_Qt.CursorShape.PointingHandCursor))
        btn_keep.setFixedHeight(30)
        try:
            btn_keep.setStyleSheet(_get_style_btn_primary())
        except Exception:
            pass

        btn_revert = QtWidgets.QPushButton("Revenir avant l'importation")
        btn_revert.setCursor(QtGui.QCursor(_Qt.CursorShape.PointingHandCursor))
        btn_revert.setFixedHeight(30)
        try:
            btn_revert.setStyleSheet(_get_style_btn_danger())
        except Exception:
            pass

        btn_cancel = QtWidgets.QPushButton("Annuler")
        btn_cancel.setCursor(QtGui.QCursor(_Qt.CursorShape.PointingHandCursor))
        btn_cancel.setFixedHeight(28)
        try:
            btn_cancel.setStyleSheet(_get_style_btn_secondary())
        except Exception:
            pass

        btn_layout.addWidget(btn_keep)
        btn_layout.addWidget(btn_revert)
        btn_layout.addWidget(btn_cancel)
        layout.addLayout(btn_layout)

        decision = ["cancel"]

        def on_keep():
            decision[0] = "commit"
            dialog.accept()

        def on_revert():
            decision[0] = "revert"
            dialog.accept()

        def on_cancel():
            decision[0] = "cancel"
            dialog.reject()

        btn_keep.clicked.connect(on_keep)
        btn_revert.clicked.connect(on_revert)
        btn_cancel.clicked.connect(on_cancel)

        dialog.exec()
        return decision[0]
    finally:
        _is_prompting_decision = False


def _on_browser_menus_did_init(browser) -> None:
    """Ajoute l'option d'historique dans les menus du browser."""
    action = QtGui.QAction("Historique & AnkiCollab...", browser)
    action.triggered.connect(lambda: _show_standalone_history(browser))
    if hasattr(browser, "form") and hasattr(browser.form, "menu_Cards"):
        browser.form.menu_Cards.addAction(action)
    elif hasattr(browser, "form") and hasattr(browser.form, "menuEdit"):
        browser.form.menuEdit.addAction(action)


def _on_browser_will_show_context_menu(browser, menu) -> None:
    """Ajoute les actions diff et l'historique dans le clic droit du browser."""
    global _browser_panel, _pending_diffs
    nids = browser.selected_notes()
    if not nids:
        return

    # Actions spécifiques au mode Diff : seulement si le panneau diff est actif et qu'il y a des diffs en attente
    if _browser_panel is not None and bool(_pending_diffs):
        # Récupérer les notes sélectionnées qui font partie du diff
        matching_panels: List[NoteDiffPanelWithSync] = []
        for nid in nids:
            real_nid = _staged_to_orig.get(nid, nid)
            p = _browser_panel._note_panels.get(real_nid)
            if p and p not in matching_panels:
                matching_panels.append(p)

        if matching_panels:
            menu.addSeparator()
            count = len(matching_panels)
            if count == 1:
                label_acc = "Diff : Accepter les modifications"
                label_rej = "Diff : Garder l'ancien (rejeter)"
                label_tags = "Diff : Accepter uniquement les tags"
            else:
                label_acc = f"Diff : Accepter les modifications ({count} notes)"
                label_rej = f"Diff : Garder l'ancien ({count} notes)"
                label_tags = f"Diff : Accepter uniquement les tags ({count} notes)"

            def _restore_focus():
                try:
                    if hasattr(browser, "form") and hasattr(browser.form, "tableView") and browser.form.tableView:
                        browser.form.tableView.setFocus()
                    elif hasattr(browser, "table") and browser.table is not None:
                        if hasattr(browser.table, "setFocus"):
                            browser.table.setFocus()
                        if hasattr(browser.table, "_view") and browser.table._view:
                            browser.table._view.setFocus()
                    elif _browser_panel:
                        _browser_panel.setFocus()
                except Exception:
                    pass
                try:
                    browser.activateWindow()
                except Exception:
                    pass

            def _accept_selected():
                for p in matching_panels:
                    p.accept_all_hunks()
                if _browser_panel:
                    _browser_panel._update_status()
                _restore_focus()
                QtCore.QTimer.singleShot(0, _restore_focus)
                QtCore.QTimer.singleShot(50, _restore_focus)

            def _reject_selected():
                for p in matching_panels:
                    if getattr(p._diff, "action", "") == "INSERT":
                        continue
                    p.reject_all_hunks()
                if _browser_panel:
                    _browser_panel._update_status()
                _restore_focus()
                QtCore.QTimer.singleShot(0, _restore_focus)
                QtCore.QTimer.singleShot(50, _restore_focus)

            def _accept_tags_selected():
                for p in matching_panels:
                    p.accept_tags_only()
                if _browser_panel:
                    _browser_panel._update_status()
                _restore_focus()
                QtCore.QTimer.singleShot(0, _restore_focus)
                QtCore.QTimer.singleShot(50, _restore_focus)

            act_acc = menu.addAction(label_acc)
            act_acc.triggered.connect(_accept_selected)

            act_rej = menu.addAction(label_rej)
            act_rej.triggered.connect(_reject_selected)

            act_tags = menu.addAction(label_tags)
            act_tags.triggered.connect(_accept_tags_selected)
            menu.addSeparator()

            # Avertir sur les actions de suggestion AnkiCollab présentes dans le menu pendant le diff
            for act in menu.actions():
                txt = act.text()
                if "Bulk suggest" in txt or ("AnkiCollab" in txt and "Historique" not in txt):
                    act.setToolTip("Une importation AnkiCollab est en cours de révision. Confirmez l'importation avant de suggérer.")

    action = menu.addAction("Historique & AnkiCollab...")
    action.triggered.connect(lambda: _show_standalone_history(browser))


def _show_standalone_history(browser) -> None:
    """Ouvre la fenêtre d'historique autonome pour la note sélectionnée."""
    nids = browser.selected_notes()
    if not nids:
        aqt.utils.showInfo("Veuillez sélectionner une note.")
        return
    nid = nids[0]
    
    try:
        note = browser.col.get_note(nid)
        field_names = [f[0] for f in note.items()]
        current_fields = dict(note.items())
        current_fields["__tags__"] = " ".join(note.tags)
    except Exception as e:
        aqt.utils.showWarning(f"Impossible de charger la note : {e}")
        return

    # Créer le dialogue
    from .ui import _get_style_base
    dialog = QtWidgets.QDialog(browser)
    dialog.setWindowTitle(f"Historique & AnkiCollab — Note {nid}")
    dialog.resize(850, 500)
    dialog.setStyleSheet(_get_style_base())
    
    layout = QtWidgets.QVBoxLayout(dialog)
    layout.setContentsMargins(0, 0, 0, 0)
    
    from .ankicollab_panel import AnkiCollabHistoryPanel
    panel = AnkiCollabHistoryPanel(nid, field_names, current_fields, dialog)
    
    # Callback de restauration
    def on_restore(fields):
        try:
            current_nid = panel._note_id
            target_note = browser.col.get_note(current_nid)
            for name, val in fields.items():
                if name == "__tags__":
                    target_note.tags = [t for t in val.strip().split(" ") if t]
                elif name in target_note:
                    target_note[name] = val
            browser.col.update_note(target_note)
            
            # Recharger l'éditeur et la table
            try:
                browser.loadNote()
                browser.on_notes_changed()
            except Exception:
                pass

            # Si le reviewer affiche cette carte, la recharger
            mw = aqt.mw
            if mw and mw.reviewer and mw.reviewer.card and mw.reviewer.card.nid == current_nid:
                try:
                    mw.reviewer.card.load()
                    mw.reviewer.show()
                except Exception:
                    pass
            
            # Enregistrer le snapshot de restauration complet
            from .history import NoteHistoryDB
            is_single_field = (len(fields) == 1 and len(field_names) > 1)
            NoteHistoryDB.instance().store_snapshot(
                note_id=current_nid,
                fields=dict(target_note.items()),
                source="restore",
                comment=f"Restauration du champ {next(iter(fields.keys()))}" if is_single_field else "Restauration d'une version précédente",
                tags=target_note.tags,
            )
            if is_single_field:
                aqt.utils.tooltip("Champ restauré avec succès")
            else:
                aqt.utils.tooltip("Version restaurée avec succès")
                dialog.accept()
        except Exception as err:
            aqt.utils.showWarning(f"Erreur de restauration : {err}")
            
    def on_protect_tags(tags):
        try:
            current_nid = panel._note_id
            target_note = browser.col.get_note(current_nid)
            if target_note:
                target_note.tags = [t for t in target_note.tags if not t.lower().startswith("ankicollab_protect::")]
                target_note.tags.extend(tags)
                browser.col.update_note(target_note)
                
                # Mettre à jour l'éditeur et la table du navigateur
                try:
                    if hasattr(browser, "editor") and browser.editor:
                        ed_note = _get_editor_note(browser.editor)
                        if ed_note and ed_note.id == current_nid:
                            ed_note.tags = target_note.tags
                            if hasattr(browser.editor, "reload_note"):
                                browser.editor.reload_note()
                            elif hasattr(browser.editor, "loadNote"):
                                browser.editor.loadNote()
                    if hasattr(browser, "loadNote"):
                        browser.loadNote()
                    if hasattr(browser, "on_notes_changed"):
                        browser.on_notes_changed()
                except Exception:
                    pass
                
                aqt.utils.tooltip("Protection des champs enregistrée sur la note")
        except Exception as e:
            aqt.utils.showWarning(f"Erreur lors de la protection : {e}")

    panel.version_retrieved.connect(on_restore)
    panel.protection_tags_applied.connect(on_protect_tags)
    layout.addWidget(panel)
    dialog.exec()


def _show_reviewer_history() -> None:
    """Ouvre la fenêtre d'historique autonome pour la carte actuellement affichée dans le reviewer."""
    mw = aqt.mw
    if not mw or not mw.reviewer or not mw.reviewer.card:
        aqt.utils.showInfo("Aucune carte n'est actuellement affichée dans le reviewer.")
        return
    card = mw.reviewer.card
    try:
        note = card.note()
        nid = note.id
        field_names = [f[0] for f in note.items()]
        current_fields = dict(note.items())
        current_fields["__tags__"] = " ".join(note.tags)
    except Exception as e:
        aqt.utils.showWarning(f"Impossible de charger la note : {e}")
        return

    # Créer le dialogue
    from .ui import _get_style_base
    dialog = QtWidgets.QDialog(mw)
    dialog.setWindowTitle(f"Historique & AnkiCollab — Note {nid}")
    dialog.resize(850, 500)
    dialog.setStyleSheet(_get_style_base())
    
    layout = QtWidgets.QVBoxLayout(dialog)
    layout.setContentsMargins(0, 0, 0, 0)
    
    from .ankicollab_panel import AnkiCollabHistoryPanel
    panel = AnkiCollabHistoryPanel(nid, field_names, current_fields, dialog)
    
    # Callback de restauration
    def on_restore(fields):
        try:
            current_nid = panel._note_id
            target_note = mw.col.get_note(current_nid)
            for name, val in fields.items():
                if name == "__tags__":
                    target_note.tags = [t for t in val.strip().split(" ") if t]
                elif name in target_note:
                    target_note[name] = val
            mw.col.update_note(target_note)
            
            # Recharger le reviewer
            if mw.reviewer and mw.reviewer.card and mw.reviewer.card.nid == current_nid:
                try:
                    mw.reviewer.card.load()
                    mw.reviewer.show()
                except Exception:
                    pass

            # Si le browser est ouvert et affiche cette note, le recharger
            if hasattr(mw, "browser") and mw.browser:
                try:
                    mw.browser.loadNote()
                    mw.browser.on_notes_changed()
                except Exception:
                    pass
            
            # Enregistrer le snapshot de restauration complet
            from .history import NoteHistoryDB
            is_single_field = (len(fields) == 1 and len(field_names) > 1)
            NoteHistoryDB.instance().store_snapshot(
                note_id=current_nid,
                fields=dict(target_note.items()),
                source="restore",
                comment=f"Restauration du champ {next(iter(fields.keys()))}" if is_single_field else "Restauration d'une version précédente",
                tags=target_note.tags,
            )
            if is_single_field:
                aqt.utils.tooltip("Champ restauré avec succès")
            else:
                aqt.utils.tooltip("Version restaurée avec succès")
                dialog.accept()
        except Exception as err:
            aqt.utils.showWarning(f"Erreur de restauration : {err}")
            
    def on_protect_tags(tags):
        try:
            current_nid = panel._note_id
            target_note = mw.col.get_note(current_nid)
            if target_note:
                target_note.tags = [t for t in target_note.tags if not t.lower().startswith("ankicollab_protect::")]
                target_note.tags.extend(tags)
                mw.col.update_note(target_note)
                
                # Recharger le reviewer
                if mw.reviewer and mw.reviewer.card and mw.reviewer.card.nid == current_nid:
                    try:
                        mw.reviewer.card.load()
                        mw.reviewer.show()
                    except Exception:
                        pass

                # Si le browser est ouvert et affiche cette note, le recharger
                if hasattr(mw, "browser") and mw.browser:
                    try:
                        if mw.browser.editor:
                            ed_note = _get_editor_note(mw.browser.editor)
                            if ed_note and ed_note.id == current_nid:
                                ed_note.tags = target_note.tags
                                if hasattr(mw.browser.editor, "reload_note"):
                                    mw.browser.editor.reload_note()
                                elif hasattr(mw.browser.editor, "loadNote"):
                                    mw.browser.editor.loadNote()
                        if hasattr(mw.browser, "loadNote"):
                            mw.browser.loadNote()
                        if hasattr(mw.browser, "on_notes_changed"):
                            mw.browser.on_notes_changed()
                    except Exception:
                        pass
                        
                aqt.utils.tooltip("Protection des champs enregistrée sur la note")
        except Exception as e:
            aqt.utils.showWarning(f"Erreur lors de la protection : {e}")

    panel.version_retrieved.connect(on_restore)
    panel.protection_tags_applied.connect(on_protect_tags)
    layout.addWidget(panel)
    dialog.exec()


def _on_reviewer_did_show_question(card) -> None:
    """Met à jour dynamiquement les panneaux d'historique ouverts lorsque la carte dans le reviewer change."""
    try:
        note = card.note()
        nid = note.id
        field_names = [f[0] for f in note.items()]
        current_fields = dict(note.items())
        current_fields["__tags__"] = " ".join(note.tags)
        
        from .ankicollab_panel import AnkiCollabHistoryPanel
        for ref in AnkiCollabHistoryPanel._instances:
            panel = ref()
            if panel is not None and panel.isVisible():
                panel.update_to_note(nid, field_names, current_fields)

        global _browser_panel
        if _browser_panel is not None:
            try:
                from aqt.qt import sip
                if not sip.isdeleted(_browser_panel) and _browser_panel.isVisible():
                    _browser_panel.select_note(nid)
            except Exception:
                pass
    except Exception:
        pass


def _show_editor_history(editor: Any) -> None:
    mw = aqt.mw
    note = _get_editor_note(editor)
    if not editor or not note:
        aqt.utils.showInfo("Aucune note active.")
        return
    nid = getattr(note, "id", 0)
    if nid <= 0:
        aqt.utils.showInfo("La note doit être enregistrée avant d'afficher son historique.")
        return
    
    field_names = [f[0] for f in note.items()]
    current_fields = dict(note.items())
    current_fields["__tags__"] = " ".join(note.tags)

    from .ui import _get_style_base
    dialog = QtWidgets.QDialog(getattr(editor, "parentWindow", None) or mw)
    dialog.setWindowTitle(f"Historique & AnkiCollab — Note {nid}")
    dialog.resize(850, 500)
    dialog.setStyleSheet(_get_style_base())
    
    layout = QtWidgets.QVBoxLayout(dialog)
    layout.setContentsMargins(0, 0, 0, 0)
    
    from .ankicollab_panel import AnkiCollabHistoryPanel
    panel = AnkiCollabHistoryPanel(nid, field_names, current_fields, dialog)
    
    # Callback de restauration
    def on_restore(fields):
        try:
            current_nid = panel._note_id
            target_note = mw.col.get_note(current_nid)
            for name, val in fields.items():
                if name == "__tags__":
                    target_note.tags = [t for t in val.strip().split(" ") if t]
                elif name in target_note:
                    target_note[name] = val
            mw.col.update_note(target_note)
            
            # Recharger l'éditeur
            try:
                if hasattr(editor, "reload_note"):
                    editor.reload_note()
                elif hasattr(editor, "loadNote"):
                    editor.loadNote()
            except Exception:
                pass

            # Si le reviewer affiche cette carte, la recharger
            if mw.reviewer and mw.reviewer.card and mw.reviewer.card.nid == current_nid:
                try:
                    mw.reviewer.card.load()
                    mw.reviewer.show()
                except Exception:
                    pass
            
            # Enregistrer le snapshot de restauration complet
            from .history import NoteHistoryDB
            is_single_field = (len(fields) == 1 and len(field_names) > 1)
            NoteHistoryDB.instance().store_snapshot(
                note_id=current_nid,
                fields=dict(target_note.items()),
                source="restore",
                comment=f"Restauration du champ {next(iter(fields.keys()))}" if is_single_field else "Restauration d'une version précédente",
                tags=target_note.tags,
            )
            if is_single_field:
                aqt.utils.tooltip("Champ restauré avec succès")
            else:
                aqt.utils.tooltip("Version restaurée avec succès")
                dialog.accept()
        except Exception as err:
            aqt.utils.showWarning(f"Erreur de restauration : {err}")
            
    def on_protect_tags(tags):
        try:
            current_nid = panel._note_id
            target_note = mw.col.get_note(current_nid)
            if target_note:
                target_note.tags = [t for t in target_note.tags if not t.lower().startswith("ankicollab_protect::")]
                target_note.tags.extend(tags)
                mw.col.update_note(target_note)
                
                # Mettre à jour l'éditeur
                try:
                    ed_note = _get_editor_note(editor)
                    if ed_note and ed_note.id == current_nid:
                        ed_note.tags = target_note.tags
                        if hasattr(editor, "reload_note"):
                            editor.reload_note()
                        elif hasattr(editor, "loadNote"):
                            editor.loadNote()
                except Exception:
                    pass
                
                # Si le reviewer affiche cette carte, la recharger
                if mw.reviewer and mw.reviewer.card and mw.reviewer.card.nid == current_nid:
                    try:
                        mw.reviewer.card.load()
                        mw.reviewer.show()
                    except Exception:
                        pass
                
                aqt.utils.tooltip("Protection des champs enregistrée sur la note")
        except Exception as e:
            aqt.utils.showWarning(f"Erreur lors de la protection : {e}")

    panel.version_retrieved.connect(on_restore)
    panel.protection_tags_applied.connect(on_protect_tags)
    layout.addWidget(panel)
    dialog.exec()


def _on_editor_did_init_buttons(buttons: List[Any], editor: Any) -> None:
    """Ajoute le bouton Historique à la toolbar de l'éditeur."""
    btn = editor.addButton(
        icon=None,
        cmd="diff_history_cmd",
        func=lambda e=editor: _show_editor_history(e),
        tip="Historique & AnkiCollab de la note",
        label="Historique",
    )
    buttons.append(btn)


def setup_native_integration() -> None:
    """Enregistre les hooks browser + éditeur. Appelé une fois au chargement de l'addon."""
    gui_hooks.editor_did_init.append(_on_editor_did_init)
    gui_hooks.editor_did_init_shortcuts.append(_on_editor_did_init_shortcuts)
    gui_hooks.editor_did_load_note.append(_on_editor_did_load_note)
    gui_hooks.editor_did_unfocus_field.append(_on_editor_did_unfocus_field)
    gui_hooks.editor_will_show_context_menu.append(_on_editor_will_show_context_menu)
    gui_hooks.editor_did_init_buttons.append(_on_editor_did_init_buttons)
    gui_hooks.browser_menus_did_init.append(_on_browser_menus_did_init)
    gui_hooks.browser_will_show_context_menu.append(_on_browser_will_show_context_menu)
    gui_hooks.reviewer_did_show_question.append(_on_reviewer_did_show_question)

    # Note: L'option de menu dans Outils de la fenêtre principale a été supprimée à la demande de l'utilisateur.
    pass
