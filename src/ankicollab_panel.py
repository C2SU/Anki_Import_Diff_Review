"""
ankicollab_panel.py — Widget unifié Historique perso + Suggestions AnkiCollab.

Ce widget est intégré comme onglet dans DiffBrowserPanel (browser_integration.py)
et peut aussi être ouvert en fenêtre standalone depuis le menu Tools.

Layout :
┌─────────────────────────────────────────────────────────────────────┐
│  AnkiCollab · Historique & Suggestions     [Statut connexion]       │
├──────────────────────┬──────────────────────────────────────────────┤
│  VERSIONS LOCALES    │  Champ : Recto                               │
│  ─────────────────── │  ┌──────────────────────────────────────────┐│
│  [LOCAL] 2026-07-09  │  │  <contenu du champ dans cette version>   ││
│  [LOCAL] 2026-07-05  │  └──────────────────────────────────────────┘│
│  ─────────────────── │  Champ : Verso                               │
│  COMMUNAUTE          │  ┌──────────────────────────────────────────┐│
│  [OK] user42  #3f2a  │  │  <contenu>                               ││
│  [..] user17  #1b9c  │  └──────────────────────────────────────────┘│
│  [KO] user03  #0a4e  │                                               │
│                      │  [Recuperer cette version]                    │
│                      │  [Proteger les champs modifies]               │
└──────────────────────┴──────────────────────────────────────────────┘
"""

from __future__ import annotations

import html as _html
import threading
import weakref
from datetime import datetime
from typing import Dict, List, Optional, Callable, Any

try:
    from PyQt6 import QtCore, QtGui, QtWidgets
    _Qt = QtCore.Qt
    _Signal = QtCore.pyqtSignal
except ImportError:
    from PySide6 import QtCore, QtGui, QtWidgets
    _Qt = QtCore.Qt
    _Signal = QtCore.Signal

from .history import NoteHistoryDB, ProtectedFieldsDB, VersionEntry
from .ankicollab_bridge import AnkiCollabBridge, CollabCommit, CollabSuggestion
from .ui import get_colors, ThemeColor, safe_timer_set_text

# ---------------------------------------------------------------------------
# Palette (partagée avec ui.py via imports)
# ---------------------------------------------------------------------------

_CLR_PANEL    = ThemeColor('background')
_CLR_SIDEBAR  = ThemeColor('surface')
_CLR_BORDER   = ThemeColor('border')
_CLR_HDR_BG   = ThemeColor('header')
_CLR_HDR_FG   = ThemeColor('primary')
_CLR_TEXT     = ThemeColor('text_primary')
_CLR_SUBTEXT  = ThemeColor('text_secondary')
_CLR_SEL      = ThemeColor('sel')
_CLR_ADD_BG   = ThemeColor('add_bg')
_CLR_ADD_FG   = ThemeColor('add_fg')
_CLR_ADD_BDR  = ThemeColor('add_border')
_CLR_ACCENT   = ThemeColor('primary')  # indigo pour AnkiCollab
_CLR_ACCENT_H = ThemeColor('primary_hover')
_CLR_WARN     = ThemeColor('warning')  # amber pour protection


def _get_style_list() -> str:
    c = get_colors()
    return f"""
QListWidget {{
    background-color: {c['surface']};
    border: none;
    border-right: 1px solid {c['border']};
    outline: none;
    padding: 2px 0px;
}}
QListWidget::item {{
    padding: 6px 10px;
    border-bottom: 1px solid {c['border']};
    color: {c['text_primary']};
    font-size: 12px;
}}
QListWidget::item:selected {{
    background-color: {c['sel']};
    color: {c['text_primary']};
}}
QListWidget::item:hover:!selected {{
    background-color: {c['surface_hover']};
}}
"""


def _get_style_btn_retrieve() -> str:
    c = get_colors()
    return f"""
QPushButton {{
    background-color: {c['accent']};
    color: {c['accent_text']};
    border: 1px solid {c['accent']};
    border-radius: 8px;
    padding: 6px 16px;
    font-size: 12px;
    font-weight: 600;
}}
QPushButton:hover {{
    background-color: {c['accent_hover']};
}}
QPushButton:pressed {{
    background-color: {c['primary_hover']};
}}
QPushButton:disabled {{
    background-color: {c['border']};
    color: {c['text_muted']};
}}
"""


def _get_style_btn_protect() -> str:
    c = get_colors()
    return f"""
QPushButton {{
    background-color: {c['warning_bg']};
    color: {c['warning']};
    border: 1px solid {c['warning_border']};
    border-radius: 8px;
    padding: 6px 16px;
    font-size: 12px;
    font-weight: 600;
}}
QPushButton:hover {{
    background-color: {c['surface_hover']};
    border-color: {c['warning']};
}}
QPushButton:disabled {{
    background-color: {c['border']};
    color: {c['text_muted']};
    border-color: {c['border']};
}}
"""


def _get_style_btn_open_web() -> str:
    c = get_colors()
    return f"""
QPushButton {{
    background-color: {c['surface_high']};
    color: {c['text_primary']};
    border: 1px solid {c['chip_border']};
    border-radius: 8px;
    padding: 6px 16px;
    font-size: 12px;
    font-weight: 600;
}}
QPushButton:hover {{
    background-color: {c['surface_hover']};
    border-color: {c['accent_mid']};
}}
QPushButton:disabled {{
    background-color: {c['border']};
    color: {c['text_muted']};
    border-color: {c['border']};
}}
"""


def _get_style_section_header() -> str:
    c = get_colors()
    return f"""
QLabel {{
    color: {c['text_secondary']};
    font-size: 10px;
    font-weight: 700;
    letter-spacing: 1px;
    background: {c['header']};
    border-bottom: 1px solid {c['border']};
    padding: 4px 10px;
}}
"""


def _get_style_field_block() -> str:
    c = get_colors()
    return f"""
QFrame {{
    background-color: {c['surface']};
    border: 1px solid {c['border']};
    border-radius: 8px;
}}
"""


# ---------------------------------------------------------------------------
# FieldPreviewBlock — affiche un champ avec son contenu HTML
# ---------------------------------------------------------------------------

class FieldPreviewBlock(QtWidgets.QFrame):
    """Affiche le nom du champ + son contenu (support HTML basique)."""

    def __init__(self, field_name: str, value: str, has_changed: bool = False, on_retrieve: Optional[Callable[[], None]] = None, parent=None):
        super().__init__(parent)
        self.setStyleSheet(_get_style_field_block())
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(8, 6, 8, 6)
        layout.setSpacing(4)

        hdr_layout = QtWidgets.QHBoxLayout()
        hdr_layout.setContentsMargins(0, 0, 0, 0)
        hdr_layout.setSpacing(6)

        hdr = QtWidgets.QLabel(_html.escape(field_name))
        hdr.setStyleSheet(
            f"color: {_CLR_HDR_FG}; font-size: 11px; font-weight: 700; "
            f"background: transparent; border: none;"
        )
        hdr_layout.addWidget(hdr)
        hdr_layout.addStretch()

        # Bouton Copier le texte
        copy_btn = QtWidgets.QPushButton("Copier")
        copy_btn.setCursor(QtGui.QCursor(_Qt.CursorShape.PointingHandCursor))
        copy_btn.setToolTip("Copier le contenu du champ dans le presse-papier")
        copy_btn.setStyleSheet(
            f"QPushButton {{ background: transparent; border: 1px solid {_CLR_BORDER}; "
            f"border-radius: 3px; color: {_CLR_SUBTEXT}; font-size: 10px; padding: 2px 6px; }}"
            f"QPushButton:hover {{ color: {_CLR_TEXT}; background: {_CLR_SEL}; border-color: {_CLR_BORDER}; }}"
        )
        
        def _copy_field():
            app = QtWidgets.QApplication.instance()
            if app:
                clipboard = app.clipboard()
                if clipboard:
                    clipboard.setText(value)
                    copy_btn.setText("Copié !")
                    safe_timer_set_text(copy_btn, "Copier", 1500)

        copy_btn.clicked.connect(_copy_field)
        hdr_layout.addWidget(copy_btn)

        if has_changed and on_retrieve:
            retrieve_btn = QtWidgets.QPushButton("Récupérer ce champ")
            retrieve_btn.setCursor(QtGui.QCursor(_Qt.CursorShape.PointingHandCursor))
            retrieve_btn.setStyleSheet(
                f"QPushButton {{ background: transparent; border: 1px solid {_CLR_BORDER}; "
                f"border-radius: 3px; color: {_CLR_TEXT}; font-size: 10px; padding: 2px 6px; }}"
                f"QPushButton:hover {{ color: #ffffff; background: {_CLR_ACCENT}; border-color: {_CLR_ACCENT}; }}"
            )
            retrieve_btn.clicked.connect(on_retrieve)
            hdr_layout.addWidget(retrieve_btn)

        layout.addLayout(hdr_layout)

        content = QtWidgets.QLabel()
        content.setTextFormat(_Qt.TextFormat.RichText)
        content.setWordWrap(True)
        content.setTextInteractionFlags(
            _Qt.TextInteractionFlag.TextSelectableByMouse | _Qt.TextInteractionFlag.TextSelectableByKeyboard
        )
        content.setStyleSheet(
            f"color: {_CLR_TEXT}; font-size: 12px; background: transparent; border: none;"
        )
        if value.strip():
            content.setText(value)
        else:
            content.setText(f"<i style='color:{_CLR_SUBTEXT};'>[Vide]</i>")
        layout.addWidget(content)


def get_tags_diff_html(old_tags: List[str], current_tags: List[str]) -> str:
    old_set = set(old_tags)
    current_set = set(current_tags)

    parts = []
    # Sort for deterministic display
    all_tags = sorted(list(old_set | current_set))
    for t in all_tags:
        if t in old_set and t not in current_set:
            # Removed in current note (was present in past)
            parts.append(f'<span style="color: #ef4444; text-decoration: line-through;">{_html.escape(t)}</span>')
        elif t in current_set and t not in old_set:
            # Added in current note (was absent in past)
            parts.append(f'<span style="color: #10b981; font-weight: bold;">+{_html.escape(t)}</span>')
        else:
            # Present in both
            parts.append(f'<span style="color: #888888;">{_html.escape(t)}</span>')

    if not parts:
        return "<i style='color: gray;'>Aucun tag</i>"
    return " ".join(parts)


class TagsPreviewBlock(QtWidgets.QFrame):
    def __init__(self, old_tags: List[str], current_tags: List[str], has_changed: bool = False, on_retrieve: Optional[Callable[[], None]] = None, parent=None):
        super().__init__(parent)
        self.setStyleSheet(_get_style_field_block())
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(8, 6, 8, 6)
        layout.setSpacing(4)

        hdr_layout = QtWidgets.QHBoxLayout()
        hdr_layout.setContentsMargins(0, 0, 0, 0)
        hdr_layout.setSpacing(6)

        lbl = QtWidgets.QLabel("Tags")
        lbl.setStyleSheet(f"color: {_CLR_HDR_FG}; font-size: 11px; font-weight: 700; background: transparent; border: none;")
        hdr_layout.addWidget(lbl)
        hdr_layout.addStretch()

        # Bouton Copier les tags
        copy_btn = QtWidgets.QPushButton("Copier")
        copy_btn.setCursor(QtGui.QCursor(_Qt.CursorShape.PointingHandCursor))
        copy_btn.setToolTip("Copier les tags dans le presse-papier")
        copy_btn.setStyleSheet(
            f"QPushButton {{ background: transparent; border: 1px solid {_CLR_BORDER}; "
            f"border-radius: 3px; color: {_CLR_SUBTEXT}; font-size: 10px; padding: 2px 6px; }}"
            f"QPushButton:hover {{ color: {_CLR_TEXT}; background: {_CLR_SEL}; border-color: {_CLR_BORDER}; }}"
        )

        def _copy_tags():
            app = QtWidgets.QApplication.instance()
            if app:
                clipboard = app.clipboard()
                if clipboard:
                    clipboard.setText(" ".join(old_tags))
                    copy_btn.setText("Copié !")
                    safe_timer_set_text(copy_btn, "Copier", 1500)

        copy_btn.clicked.connect(_copy_tags)
        hdr_layout.addWidget(copy_btn)

        if has_changed and on_retrieve:
            retrieve_btn = QtWidgets.QPushButton("Récupérer les tags")
            retrieve_btn.setCursor(QtGui.QCursor(_Qt.CursorShape.PointingHandCursor))
            retrieve_btn.setStyleSheet(
                f"QPushButton {{ background: transparent; border: 1px solid {_CLR_BORDER}; "
                f"border-radius: 3px; color: {_CLR_TEXT}; font-size: 10px; padding: 2px 6px; }}"
                f"QPushButton:hover {{ color: #ffffff; background: {_CLR_ACCENT}; border-color: {_CLR_ACCENT}; }}"
            )
            retrieve_btn.clicked.connect(on_retrieve)
            hdr_layout.addWidget(retrieve_btn)

        layout.addLayout(hdr_layout)

        content = QtWidgets.QLabel()
        content.setTextFormat(_Qt.TextFormat.RichText)
        content.setWordWrap(True)
        content.setTextInteractionFlags(
            _Qt.TextInteractionFlag.TextSelectableByMouse | _Qt.TextInteractionFlag.TextSelectableByKeyboard
        )
        content.setStyleSheet(
            f"color: {_CLR_TEXT}; font-size: 12px; background: transparent; border: none;"
        )
        content.setText(get_tags_diff_html(old_tags, current_tags))
        layout.addWidget(content)


# ---------------------------------------------------------------------------
# VersionLegendBlock — affiche les métadonnées de la version (Légende)
# ---------------------------------------------------------------------------

class VersionLegendBlock(QtWidgets.QFrame):
    """Affiche les métadonnées de la version sélectionnée (Légende)."""

    def __init__(self, kind: str, obj: Any, parent=None):
        super().__init__(parent)
        c = get_colors()
        self.setStyleSheet(
            f"QFrame {{ background-color: {c['header']}; border: 1px solid {c['border']}; border-radius: 6px; }}"
        )
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(6)

        title_lbl = QtWidgets.QLabel()
        title_lbl.setStyleSheet(
            f"font-weight: bold; color: {c['primary']}; font-size: 12px; border: none; background: transparent;"
        )
        
        meta_lbl = QtWidgets.QLabel()
        meta_lbl.setStyleSheet(
            f"color: {c['text_secondary']}; font-size: 11px; border: none; background: transparent;"
        )
        meta_lbl.setWordWrap(True)

        desc_lbl = QtWidgets.QLabel()
        desc_lbl.setStyleSheet(
            f"color: {c['text_primary']}; font-size: 12px; font-style: italic; border: none; background: transparent;"
        )
        desc_lbl.setWordWrap(True)

        if kind == "local":
            title_lbl.setText("Sauvegarde locale")
            meta_lbl.setText(f"Date : {obj.timestamp_str}")
            if obj.comment:
                desc_lbl.setText(f"Message : {obj.comment}")
            else:
                desc_lbl.setVisible(False)
        elif kind == "suggestion":
            title_lbl.setText("Suggestion communauté")
            meta_lbl.setText(f"Auteur : {obj.author}  ·  Date : {obj.created_str}  ·  Votes : {obj.vote_count}")
            if obj.comment:
                desc_lbl.setText(f"Description : {obj.comment}")
            else:
                desc_lbl.setVisible(False)
        elif kind == "commit":
            title_lbl.setText("Commit AnkiCollab")
            meta_lbl.setText(f"Auteur : {obj.author}  ·  Date : {obj.timestamp_str}  ·  Hash : {obj.short_hash}")
            if obj.message:
                desc_lbl.setText(f"Message : {obj.message}")
            else:
                desc_lbl.setVisible(False)

        layout.addWidget(title_lbl)
        layout.addWidget(meta_lbl)
        if desc_lbl.isVisible():
            layout.addWidget(desc_lbl)


# ---------------------------------------------------------------------------
# AnkiCollabHistoryPanel — widget principal
# ---------------------------------------------------------------------------

class AnkiCollabHistoryPanel(QtWidgets.QWidget):
    """
    Panneau unifié :
    - Liste gauche : versions locales + suggestions AnkiCollab
    - Panneau droit : preview des champs de la version sélectionnée
    - Boutons : Récupérer, Protéger les champs

    Signals :
        version_retrieved(Dict[str, str])  — émis quand l'user veut charger une version
        fields_protected(Dict[str, str])   — émis quand l'user protège des champs
    """

    version_retrieved = _Signal(dict)   # {field_name: value}
    fields_protected = _Signal(dict)    # {field_name: value}
    protection_tags_applied = _Signal(list) # [tag1, tag2]
    _instances: List[weakref.ref[AnkiCollabHistoryPanel]] = []

    def __init__(
        self,
        note_id: int,
        field_names: List[str],
        current_fields: Optional[Dict[str, str]] = None,
        parent=None,
    ):
        super().__init__(parent)
        self._note_id = note_id
        self._field_names = field_names
        self._current_fields = current_fields or {}
        self._selected_fields: Optional[Dict[str, str]] = None
        self._history_db = NoteHistoryDB.instance()
        self._protection_db = ProtectedFieldsDB(self._history_db)
        self._bridge = AnkiCollabBridge.instance()
        self._local_versions: List[VersionEntry] = []
        self._collab_commits: List[CollabCommit] = []
        self._collab_suggestions: List[CollabSuggestion] = []

        # Enregistrer cette instance de façon weak
        AnkiCollabHistoryPanel._instances = [ref for ref in AnkiCollabHistoryPanel._instances if ref() is not None]
        AnkiCollabHistoryPanel._instances.append(weakref.ref(self))

        self._build_ui()
        self._load_data()

    # ── Construction ─────────────────────────────────────────────────────

    def _build_ui(self):
        c = get_colors()
        self.setStyleSheet(
            f"QWidget {{ background-color: {c['background']}; color: {c['text_primary']}; "
            f"font-family: 'Segoe UI', 'Inter', sans-serif; }}"
        )

        root = QtWidgets.QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # ── Header ───────────────────────────────────────────────────────
        header = QtWidgets.QFrame()
        header.setStyleSheet(
            f"background-color: {c['header']}; border-bottom: 1px solid {c['border']};"
        )
        header.setFixedHeight(40)
        hl = QtWidgets.QHBoxLayout(header)
        hl.setContentsMargins(12, 0, 12, 0)
        hl.setSpacing(8)

        title = QtWidgets.QLabel("Historique & Versions")
        title.setStyleSheet(
            f"color: {c['primary']}; font-size: 13px; font-weight: 700; "
            f"background: transparent;"
        )

        self._status_lbl = QtWidgets.QLabel()
        self._status_lbl.setStyleSheet(
            f"color: {c['text_secondary']}; font-size: 11px; background: transparent;"
        )
        self._refresh_status()

        refresh_btn = QtWidgets.QPushButton("Actualiser")
        refresh_btn.setFixedHeight(24)
        refresh_btn.setToolTip("Recharger les données")
        refresh_btn.setStyleSheet(
            f"QPushButton {{ background: transparent; border: 1px solid {c['border']}; "
            f"border-radius: 3px; color: {c['text_secondary']}; font-size: 11px; padding: 2px 8px; }}"
            f"QPushButton:hover {{ color: {c['text_primary']}; background: {c['sel']}; }}"
        )
        refresh_btn.setCursor(QtGui.QCursor(_Qt.CursorShape.PointingHandCursor))
        refresh_btn.clicked.connect(self._load_data)

        hl.addWidget(title)
        hl.addStretch()
        hl.addWidget(self._status_lbl)
        hl.addWidget(refresh_btn)
        root.addWidget(header)

        # ── Corps : splitter gauche/droite ───────────────────────────────
        body = QtWidgets.QSplitter(_Qt.Orientation.Horizontal)
        body.setHandleWidth(1)
        body.setStyleSheet(f"QSplitter::handle {{ background: {c['border']}; }}")

        # ── Gauche : liste versions ───────────────────────────────────────
        left = QtWidgets.QWidget()
        left.setStyleSheet(f"background-color: {c['surface']};")
        left.setMinimumWidth(200)
        ll = QtWidgets.QVBoxLayout(left)
        ll.setContentsMargins(0, 0, 0, 0)
        ll.setSpacing(0)

        self._list = QtWidgets.QListWidget()
        self._list.setStyleSheet(_get_style_list())
        self._list.currentRowChanged.connect(self._on_row_changed)
        ll.addWidget(self._list)

        body.addWidget(left)

        # ── Droite : preview champs + boutons ────────────────────────────
        right = QtWidgets.QWidget()
        right.setStyleSheet(f"background-color: {c['background']};")
        rl = QtWidgets.QVBoxLayout(right)
        rl.setContentsMargins(12, 12, 12, 12)
        rl.setSpacing(8)

        # Scroll area pour les champs
        self._fields_scroll = QtWidgets.QScrollArea()
        self._fields_scroll.setWidgetResizable(True)
        self._fields_scroll.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
        self._fields_scroll.setStyleSheet(
            f"background-color: {c['background']}; border: none;"
        )
        self._fields_container = QtWidgets.QWidget()
        self._fields_container.setStyleSheet(f"background-color: {c['background']};")
        self._fields_layout = QtWidgets.QVBoxLayout(self._fields_container)
        self._fields_layout.setContentsMargins(0, 0, 0, 0)
        self._fields_layout.setSpacing(6)
        self._fields_layout.addStretch()
        self._fields_scroll.setWidget(self._fields_container)
        rl.addWidget(self._fields_scroll, stretch=1)

        # ── Boutons d'action ─────────────────────────────────────────────
        btn_row = QtWidgets.QHBoxLayout()
        btn_row.setSpacing(8)

        self._btn_retrieve = QtWidgets.QPushButton("Récupérer cette version")
        self._btn_retrieve.setStyleSheet(_get_style_btn_retrieve())
        self._btn_retrieve.setEnabled(False)
        self._btn_retrieve.setCursor(QtGui.QCursor(_Qt.CursorShape.PointingHandCursor))
        self._btn_retrieve.setToolTip(
            "Charge les champs de cette version dans le panneau diff actif"
        )
        self._btn_retrieve.clicked.connect(self._on_retrieve)

        self._btn_protect = QtWidgets.QPushButton("Protéger les champs")
        self._btn_protect.setStyleSheet(_get_style_btn_protect())
        self._btn_protect.setEnabled(False)
        self._btn_protect.setCursor(QtGui.QCursor(_Qt.CursorShape.PointingHandCursor))
        self._btn_protect.setToolTip(
            "Protège les champs de cette version contre les modifications futures lors du diff"
        )
        self._btn_protect.clicked.connect(self._on_protect)

        self._btn_open_web = QtWidgets.QPushButton("Ouvrir sur AnkiCollab")
        self._btn_open_web.setStyleSheet(_get_style_btn_open_web())
        self._btn_open_web.setEnabled(False)
        self._btn_open_web.setCursor(QtGui.QCursor(_Qt.CursorShape.PointingHandCursor))
        self._btn_open_web.setToolTip(
            "Ouvre la page de révision/historique de cette note sur le site AnkiCollab"
        )
        self._btn_open_web.clicked.connect(self._on_open_web)

        btn_row.addWidget(self._btn_retrieve)
        btn_row.addWidget(self._btn_protect)
        btn_row.addWidget(self._btn_open_web)
        btn_row.addStretch()
        rl.addLayout(btn_row)

        body.addWidget(right)
        body.setStretchFactor(0, 0)
        body.setStretchFactor(1, 1)
        body.setSizes([210, 400])
        root.addWidget(body, stretch=1)

    # ── Chargement des données ────────────────────────────────────────────

    def _load_data(self):
        """Charge les versions locales et (si disponible) les données AnkiCollab.

        Stratégie threads :
        - Thread principal  : SQLite local + requêtes SQL Anki (rapides, <5ms, thread-safe)
        - threading.Thread  : uniquement les appels réseau HTTP AnkiCollab (lents)
          → pas de taskman.run_in_background qui affiche "Traitement..."
        """
        # ── Synchrone : données locales ───────────────────────────────────
        self._local_versions = self._history_db.get_history(self._note_id)
        self._collab_commits = []
        self._collab_suggestions = []
        self._refresh_status()
        self._populate_list()

        # Requêtes SQL rapides (toujours sur le thread principal Qt)
        bridge = self._bridge
        note_id_snap = self._note_id

        deck_hash = bridge.get_deck_hash_for_note(note_id_snap)
        self._btn_open_web.setEnabled(deck_hash is not None)
        self._btn_protect.setVisible(deck_hash is not None)

        if not bridge.is_available():
            return
        guid = bridge.get_note_guid(note_id_snap)
        if not guid:
            return

        # ── Asynchrone : appels réseau uniquement ─────────────────────────
        bridge._get_session()  # init session sur thread principal (thread-safe)

        def _bg_fetch():
            """Uniquement HTTP — aucun accès col.db depuis ce thread."""
            commits = []
            suggestions = []
            try:
                commits = bridge.fetch_note_history(guid)
            except Exception:
                pass
            try:
                suggestions = bridge.fetch_note_suggestions(guid)
            except Exception:
                pass
            return commits, suggestions

        def _on_done(commits, suggestions):
            if self._note_id != note_id_snap:
                return
            try:
                self._collab_commits = commits
                self._collab_suggestions = suggestions
                self._populate_list()
            except RuntimeError:
                pass

        def _run():
            result = _bg_fetch()
            QtCore.QTimer.singleShot(0, lambda: _on_done(*result))

        threading.Thread(target=_run, daemon=True).start()


    def _refresh_status(self):
        status = self._bridge.get_connection_status()
        self._status_lbl.setText(status)

    def _populate_list(self):
        """Remplit la liste avec les versions locales puis les données collab."""
        self._list.blockSignals(True)
        self._list.clear()

        protected = self._protection_db.get_all_protected(self._note_id)

        # ── Section : Versions locales ────────────────────────────────────
        if self._local_versions:
            sep = QtWidgets.QListWidgetItem("  SAUVEGARDES LOCALES")
            sep.setFlags(_Qt.ItemFlag.NoItemFlags)
            sep.setForeground(QtGui.QColor(str(_CLR_SUBTEXT)))
            font = sep.font()
            font.setPointSize(9)
            font.setBold(True)
            sep.setFont(font)
            sep.setBackground(QtGui.QColor(str(_CLR_HDR_BG)))
            self._list.addItem(sep)

            for v in self._local_versions:
                item = QtWidgets.QListWidgetItem()
                item.setData(_Qt.ItemDataRole.UserRole, ("local", v))
                text = f"{v.source_icon}  {v.timestamp_str}"
                if v.comment:
                    text += f"\n     {v.comment}"
                item.setText(text)
                item.setSizeHint(QtCore.QSize(200, 42 if v.comment else 28))
                self._list.addItem(item)

        # ── Section : Suggestions AnkiCollab ─────────────────────────────
        if self._collab_suggestions:
            sep2 = QtWidgets.QListWidgetItem("  SUGGESTIONS COMMUNAUTÉ")
            sep2.setFlags(_Qt.ItemFlag.NoItemFlags)
            sep2.setForeground(QtGui.QColor(str(_CLR_SUBTEXT)))
            font2 = sep2.font()
            font2.setPointSize(9)
            font2.setBold(True)
            sep2.setFont(font2)
            sep2.setBackground(QtGui.QColor(str(_CLR_HDR_BG)))
            self._list.addItem(sep2)

            for s in self._collab_suggestions:
                item = QtWidgets.QListWidgetItem()
                item.setData(_Qt.ItemDataRole.UserRole, ("suggestion", s))
                text = (
                    f"{s.author}  ·  {s.created_str}"
                    f"\n     +{s.vote_count} votes"
                )
                if s.comment:
                    text += f"  —  {s.comment[:40]}"
                item.setText(text)
                item.setSizeHint(QtCore.QSize(200, 42))
                self._list.addItem(item)

        # ── Section : Historique commits AnkiCollab ───────────────────────
        if self._collab_commits:
            sep3 = QtWidgets.QListWidgetItem("  HISTORIQUE COLLAB")
            sep3.setFlags(_Qt.ItemFlag.NoItemFlags)
            sep3.setForeground(QtGui.QColor(str(_CLR_SUBTEXT)))
            font3 = sep3.font()
            font3.setPointSize(9)
            font3.setBold(True)
            sep3.setFont(font3)
            sep3.setBackground(QtGui.QColor(str(_CLR_HDR_BG)))
            self._list.addItem(sep3)

            for c in self._collab_commits:
                item = QtWidgets.QListWidgetItem()
                item.setData(_Qt.ItemDataRole.UserRole, ("commit", c))
                text = (
                    f"{c.short_hash}  ·  {c.timestamp_str}"
                    f"\n     {c.author}  —  {c.message[:40]}"
                )
                item.setText(text)
                item.setSizeHint(QtCore.QSize(200, 42))
                self._list.addItem(item)

        if not self._local_versions and not self._collab_commits and not self._collab_suggestions:
            empty = QtWidgets.QListWidgetItem("  Aucun historique disponible")
            empty.setFlags(_Qt.ItemFlag.NoItemFlags)
            empty.setForeground(QtGui.QColor(str(_CLR_SUBTEXT)))
            self._list.addItem(empty)

        self._list.blockSignals(False)

    # ── Sélection d'une version ───────────────────────────────────────────

    def _on_row_changed(self, row: int):
        if row < 0:
            return
        item = self._list.item(row)
        if item is None:
            return
        data = item.data(_Qt.ItemDataRole.UserRole)
        if data is None:
            return  # séparateur

        kind, obj = data
        if kind == "local":
            self._selected_fields = dict(obj.fields)
            if obj.tags:
                self._selected_fields["__tags__"] = " ".join(obj.tags)
        elif kind == "suggestion" or kind == "commit":
            self._selected_fields = dict(obj.fields)
        else:
            self._selected_fields = None

        self._refresh_fields_preview()
        enabled = self._selected_fields is not None and bool(self._selected_fields)
        self._btn_retrieve.setEnabled(enabled)
        self._btn_protect.setEnabled(enabled)

    def _refresh_fields_preview(self):
        """Met à jour le panneau droit avec les champs de la version sélectionnée."""
        # Supprimer les anciens widgets
        layout = self._fields_layout
        while layout.count() > 1:  # garde le stretch
            item = layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        if not self._selected_fields:
            placeholder = QtWidgets.QLabel(
                f"<i style='color:{_CLR_SUBTEXT};'>Sélectionnez une version dans la liste</i>"
            )
            placeholder.setAlignment(_Qt.AlignmentFlag.AlignCenter)
            placeholder.setStyleSheet("background: transparent;")
            layout.insertWidget(0, placeholder)
            return

        # Trouver l'objet sélectionné et insérer le bloc de légende
        row = self._list.currentRow()
        item = self._list.item(row)
        if item is not None:
            data = item.data(_Qt.ItemDataRole.UserRole)
            if data is not None:
                kind, obj = data
                legend = VersionLegendBlock(kind, obj)
                layout.insertWidget(0, legend)

        protected = self._protection_db.get_all_protected(self._note_id)

        # 1. Tags block first
        fields_offset = 1
        if self._selected_fields and "__tags__" in self._selected_fields:
            old_tags = [t for t in self._selected_fields["__tags__"].strip().split(" ") if t]
            current_tags = [t for t in self._current_fields.get("__tags__", "").strip().split(" ") if t]
            
            has_changed = sorted(old_tags) != sorted(current_tags)
            
            def make_retrieve_tags_callback(tags_val=self._selected_fields["__tags__"]):
                return lambda: self._on_retrieve_field("__tags__", tags_val)

            tags_block = TagsPreviewBlock(
                old_tags=old_tags,
                current_tags=current_tags,
                has_changed=has_changed,
                on_retrieve=make_retrieve_tags_callback(),
                parent=self._fields_container
            )
            layout.insertWidget(1, tags_block)
            fields_offset = 2

        # 2. Afficher les champs dans l'ordre de field_names (ou dans l'ordre du dict)
        field_order = self._field_names or list(self._selected_fields.keys())
        for i, fname in enumerate(field_order):
            if fname == "__tags__":
                continue
            val = self._selected_fields.get(fname, "")
            current_val = self._current_fields.get(fname, "")
            has_changed = val != current_val
            
            def make_retrieve_field_callback(name=fname, value=val):
                return lambda: self._on_retrieve_field(name, value)

            block = FieldPreviewBlock(
                fname, val,
                has_changed=has_changed,
                on_retrieve=make_retrieve_field_callback(),
                parent=self._fields_container
            )

            # Badge si le champ est protégé
            if fname in protected:
                lock_info = QtWidgets.QLabel("Champ protégé")
                lock_info.setStyleSheet(
                    f"color: {_CLR_WARN}; font-size: 10px; background: transparent; "
                    f"border: none;"
                )
                block.layout().addWidget(lock_info)

            # Puisque la légende est insérée à l'index 0, on insère à i + fields_offset
            layout.insertWidget(i + fields_offset, block)

    # ── Actions ──────────────────────────────────────────────────────────

    def _on_retrieve_field(self, fname: str, val: str):
        if self._current_fields is None:
            self._current_fields = {}
        self._current_fields[fname] = val
        self.version_retrieved.emit({fname: val})
        self._refresh_fields_preview()

    def _on_retrieve(self):
        """Émet version_retrieved avec les champs de la version sélectionnée."""
        if not self._selected_fields:
            return
        fields_to_restore = dict(self._selected_fields)
        if self._current_fields is None:
            self._current_fields = {}
        self._current_fields.update(fields_to_restore)
        self.version_retrieved.emit(fields_to_restore)
        self._refresh_fields_preview()

    def _on_protect(self):
        """Affiche le ProtectFieldsDialog d'AnkiCollab avec les champs modifiés pré-cochés."""
        if not self._selected_fields:
            return

        # 1. Identifier les champs qui ont changé
        changed_fields = []
        for fname in self._field_names:
            old_val = self._selected_fields.get(fname, "")
            current_val = self._current_fields.get(fname, "")
            if old_val != current_val:
                changed_fields.append(fname)
        
        if "__tags__" in self._selected_fields:
            old_tags = [t for t in self._selected_fields["__tags__"].strip().split(" ") if t]
            current_tags = [t for t in self._current_fields.get("__tags__", "").strip().split(" ") if t]
            if sorted(old_tags) != sorted(current_tags):
                changed_fields.append("Tags")

        # 2. Importer le ProtectFieldsDialog d'AnkiCollab
        try:
            import importlib
            dialogs_mod = importlib.import_module("1957538407.dialogs")
            ProtectFieldsDialog = dialogs_mod.ProtectFieldsDialog
            var_defs_mod = importlib.import_module("1957538407.var_defs")
            PREFIX_PROTECTED_FIELDS = getattr(var_defs_mod, "PREFIX_PROTECTED_FIELDS", "AnkiCollab_Protect")
        except ImportError:
            QtWidgets.QMessageBox.warning(
                self,
                "AnkiCollab",
                "L'addon AnkiCollab officiel est requis pour gérer les protections de champs."
            )
            return

        # 3. Ouvrir le dialog
        dialog = ProtectFieldsDialog(self._field_names, changed_fields, parent=self)
        if dialog.exec() != QtWidgets.QDialog.DialogCode.Accepted:
            return

        result = dialog.get_selected_fields()
        if result is None:
            return

        selected_fields, protect_tags = result

        # 4. Construire les nouveaux tags de protection
        new_protect_tags = []
        if selected_fields:
            if set(selected_fields) == set(self._field_names):
                new_protect_tags.append(f"{PREFIX_PROTECTED_FIELDS}::All")
            else:
                for field in selected_fields:
                    safe_field = field.replace(' ', '_')
                    new_protect_tags.append(f"{PREFIX_PROTECTED_FIELDS}::{safe_field}")
        
        if protect_tags:
            new_protect_tags.append(f"{PREFIX_PROTECTED_FIELDS}::Tags")

        # 5. Appliquer les tags
        self.fields_protected.emit(dict(self._selected_fields))
        self.protection_tags_applied.emit(new_protect_tags)
        
        # Refresh badge
        self._refresh_fields_preview()
        # Feedback visuel
        self._btn_protect.setText("Champs protégés")
        QtCore.QTimer.singleShot(
            2000,
            lambda: self._btn_protect.setText("Protéger les champs"),
        )

    def _on_open_web(self):
        """Ouvre la page de révision, historique ou commit correspondante sur le site AnkiCollab."""
        try:
            import aqt
            col = aqt.mw.col
            note = col.get_note(self._note_id)
            if not note:
                return
            guid = note.guid
            
            # Obtenir le deck hash
            deck_hash = self._bridge.get_deck_hash_for_note(self._note_id)
            if not deck_hash:
                QtWidgets.QMessageBox.warning(
                    self,
                    "AnkiCollab",
                    "Cette note n'appartient pas à un paquet synchronisé sur AnkiCollab."
                )
                return
            
            # Déterminer quel type d'élément est sélectionné
            item = self._list.currentItem()
            kind = None
            obj = None
            if item:
                data = item.data(_Qt.ItemDataRole.UserRole)
                if data:
                    kind, obj = data

            # Si c'est un commit, on peut aller directement sur sa page de commit
            if kind == "commit":
                url = f"https://www.ankicollab.com/commit/{obj.commit_hash}"
                import webbrowser
                webbrowser.open(url)
                return

            # Sinon, on résout le note_id en faisant le POST vers /resolveNoteForReview
            session = self._bridge._get_session()
            if not session:
                QtWidgets.QMessageBox.warning(
                    self,
                    "AnkiCollab",
                    "Veuillez vous connecter à AnkiCollab dans l'add-on officiel pour effectuer cette action."
                )
                return

            payload = {
                "deck_hash": deck_hash,
                "guid": guid
            }
            # Faire la requête sans suivre les redirections pour intercepter le Location header
            resp = session.post(
                "https://plugin.ankicollab.com/resolveNoteForReview",
                json=payload,
                allow_redirects=False,
                timeout=10
            )
            
            redirect_url = None
            if resp.status_code in (301, 302, 303, 307, 308):
                redirect_url = resp.headers.get("Location")
            elif resp.status_code == 200:
                try:
                    data = resp.json()
                    redirect_url = data.get("redirect") or data.get("url")
                except Exception:
                    redirect_url = resp.text

            if not redirect_url:
                QtWidgets.QMessageBox.warning(
                    self,
                    "AnkiCollab",
                    f"Impossible de résoudre la note sur AnkiCollab (Code : {resp.status_code})."
                )
                return

            # Assurer que l'URL est absolue
            if redirect_url.startswith("/"):
                redirect_url = "https://www.ankicollab.com" + redirect_url
            elif not redirect_url.startswith("http"):
                redirect_url = "https://www.ankicollab.com/" + redirect_url

            # Si l'user a sélectionné la section locale (ou double-clic), on affiche l'historique
            # de la note /note_history/{note_id} à la place de la révision /review/{note_id}
            if kind == "local" or kind is None:
                import re
                match = re.search(r"/review/(\d+)", redirect_url)
                if match:
                    note_id_remote = match.group(1)
                    redirect_url = f"https://www.ankicollab.com/note_history/{note_id_remote}"

            import webbrowser
            webbrowser.open(redirect_url)

        except Exception as e:
            QtWidgets.QMessageBox.critical(
                self,
                "Erreur",
                f"Une erreur est survenue lors de l'ouverture d'AnkiCollab : {e}"
            )

    # ── API publique ─────────────────────────────────────────────────────

    def set_note(self, note_id: int, field_names: List[str],
                 current_fields: Optional[Dict[str, str]] = None) -> None:
        """Change la note affichée (permet de réutiliser le widget)."""
        is_same_note = (note_id == self._note_id)
        saved_selected = dict(self._selected_fields) if (is_same_note and self._selected_fields) else None
        current_row = self._list.currentRow() if is_same_note else -1

        self._note_id = note_id
        self._field_names = field_names
        self._current_fields = current_fields or {}

        if not is_same_note:
            self._selected_fields = None
            self._btn_retrieve.setEnabled(False)
            self._btn_protect.setEnabled(False)
            self._btn_open_web.setEnabled(False)
        else:
            self._selected_fields = saved_selected

        self._load_data()

        if is_same_note and current_row >= 0 and current_row < self._list.count():
            self._list.blockSignals(True)
            self._list.setCurrentRow(current_row)
            self._list.blockSignals(False)
            self._selected_fields = saved_selected
            self._refresh_fields_preview()
            self._btn_retrieve.setEnabled(bool(saved_selected))
            self._btn_protect.setEnabled(bool(saved_selected))

    def update_to_note(self, note_id: int, field_names: List[str],
                       current_fields: Optional[Dict[str, str]] = None) -> None:
        """Met à jour le panneau vers une nouvelle note (ex: changement de carte dans le reviewer)."""
        self.set_note(note_id, field_names, current_fields)
        win = self.window()
        if win and isinstance(win, QtWidgets.QDialog):
            win.setWindowTitle(f"Historique & AnkiCollab — Note {note_id}")

    def store_current_snapshot(self, source: str = "import", comment: str = "") -> None:
        """Stocke un snapshot des champs courants dans l'historique local."""
        if self._current_fields:
            self._history_db.store_snapshot(
                self._note_id, self._current_fields, source=source, comment=comment
            )
            self._local_versions = self._history_db.get_history(self._note_id)
            self._populate_list()


# ---------------------------------------------------------------------------
# AnkiCollabHistoryDialog — fenêtre standalone
# ---------------------------------------------------------------------------

class AnkiCollabHistoryDialog(QtWidgets.QDialog):
    """
    Fenêtre standalone ouvrable depuis Tools > Diff Hunk > Historique.
    Montre l'historique d'une note spécifique.
    """

    def __init__(self, note_id: int, field_names: List[str],
                 current_fields: Optional[Dict[str, str]] = None,
                 on_retrieve: Optional[Callable[[Dict[str, str]], None]] = None,
                 parent=None):
        super().__init__(parent)
        self._on_retrieve = on_retrieve
        self.setWindowTitle(f"Historique · Note {note_id}")
        self.resize(720, 520)
        self.setStyleSheet(
            f"QDialog {{ background-color: {_CLR_PANEL}; color: {_CLR_TEXT}; "
            f"font-family: 'Segoe UI', 'Inter', sans-serif; }}"
        )

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self._panel = AnkiCollabHistoryPanel(
            note_id=note_id,
            field_names=field_names,
            current_fields=current_fields,
            parent=self,
        )
        if on_retrieve:
            self._panel.version_retrieved.connect(on_retrieve)
        layout.addWidget(self._panel, stretch=1)

        # Footer
        footer = QtWidgets.QFrame()
        footer.setStyleSheet(
            f"background-color: {_CLR_HDR_BG}; border-top: 1px solid {_CLR_BORDER};"
        )
        footer.setFixedHeight(48)
        fl = QtWidgets.QHBoxLayout(footer)
        fl.setContentsMargins(12, 0, 12, 0)
        fl.addStretch()
        close_btn = QtWidgets.QPushButton("Fermer")
        close_btn.setStyleSheet(
            f"QPushButton {{ background: #27272a; color: {_CLR_TEXT}; "
            f"border: 1px solid {_CLR_BORDER}; border-radius: 4px; padding: 6px 16px; }}"
            f"QPushButton:hover {{ background: #3f3f46; }}"
        )
        close_btn.clicked.connect(self.accept)
        fl.addWidget(close_btn)
        layout.addWidget(footer)

    @property
    def panel(self) -> AnkiCollabHistoryPanel:
        return self._panel
