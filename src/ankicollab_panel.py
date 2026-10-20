"""
ankicollab_panel.py — Panneau d'historique de note et comparaison des champs modifies.

Affiche l'historique complet de la note (enregistrements locaux, baseline pre-session,
imports, et sauvegardes automatiques Anki .colpkg) sous la forme d'un mode de
comparaison des champs modifies (Avant / Apres), directement inspire de l'ergonomie
de https://ankicollab.com/note_history/<id>.

Fonctionnalites cles :
  - Mode de comparaison des champs modifies : met en evidence uniquement les champs
    qui ont change entre deux versions.
  - Affichage cote-a-cote Avant (precedent) | Apres (mis a jour) avec surlignage des diffs.
  - Restauration granulaire champ par champ (Avant ou Apres) et copie rapide.
  - Elimination des micro-versions de frappe (seules les versions stabilisees sont affichees).
  - Exploration et indexation des sauvegardes .colpkg d'Anki pour retrouver l'historique ancien.
  - Tiroir depliable pour consulter ou retablir les champs inchanges a cette version.
"""

from __future__ import annotations

import difflib
import html as _html
import re
import threading
import weakref
from datetime import datetime
from typing import Dict, List, Optional, Callable, Any, Tuple

try:
    from PyQt6 import QtCore, QtGui, QtWidgets
    _Qt = QtCore.Qt
    _Signal = QtCore.pyqtSignal
except ImportError:
    from PySide6 import QtCore, QtGui, QtWidgets
    _Qt = QtCore.Qt
    _Signal = QtCore.Signal

from .history import (
    NoteHistoryDB,
    ProtectedFieldsDB,
    VersionEntry,
    VersionDelta,
    FieldDelta,
)
from .ankicollab_bridge import AnkiCollabBridge, CollabCommit, CollabSuggestion
from .ui import get_colors, ThemeColor, safe_timer_set_text

# ---------------------------------------------------------------------------
# Palette & Styles
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
_CLR_ACCENT   = ThemeColor('primary')
_CLR_ACCENT_H = ThemeColor('primary_hover')
_CLR_WARN     = ThemeColor('warning')


def _render_diff_sides(old_text: str, new_text: str, colors: dict) -> Tuple[str, str]:
    """
    Genere le HTML formate pour le comparateur cote-a-cote :
      - old_side : texte precedent avec les suppressions surlignees en rouge barre
      - new_side : texte mis a jour avec les ajouts surlignes en vert
    """
    rem_style = (
        f"background-color: {colors.get('rem_bg', '#2b0c0c')}; "
        f"color: {colors.get('rem_fg', '#F87171')}; "
        f"text-decoration: line-through; border-radius: 2px; padding: 1px 3px;"
    )
    add_style = (
        f"background-color: {colors.get('add_bg', '#032117')}; "
        f"color: {colors.get('add_fg', '#34D399')}; "
        f"font-weight: 600; border-radius: 2px; padding: 1px 3px;"
    )

    t1 = re.findall(r"<[^>]+>|[\w\'-]+|\s+|[^\w\s<>]", old_text or "")
    t2 = re.findall(r"<[^>]+>|[\w\'-]+|\s+|[^\w\s<>]", new_text or "")

    sm = difflib.SequenceMatcher(None, t1, t2, autojunk=False)

    old_out = []
    new_out = []

    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            txt1 = "".join(t1[i1:i2])
            txt2 = "".join(t2[j1:j2])
            old_out.append(txt1)
            new_out.append(txt2)
        elif tag == "delete":
            del_txt = "".join(t1[i1:i2])
            old_out.append(f"<span style=\"{rem_style}\">{del_txt}</span>")
        elif tag == "insert":
            ins_txt = "".join(t2[j1:j2])
            new_out.append(f"<span style=\"{add_style}\">{ins_txt}</span>")
        elif tag == "replace":
            del_txt = "".join(t1[i1:i2])
            ins_txt = "".join(t2[j1:j2])
            old_out.append(f"<span style=\"{rem_style}\">{del_txt}</span>")
            new_out.append(f"<span style=\"{add_style}\">{ins_txt}</span>")

    return "".join(old_out), "".join(new_out)


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
    padding: 8px 10px;
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


# ---------------------------------------------------------------------------
# FieldComparisonCard — Carte de comparaison d'un champ modifie
# ---------------------------------------------------------------------------

class FieldComparisonCard(QtWidgets.QFrame):
    """
    Affiche la comparaison Avant | Apres pour un champ ayant change entre deux versions.
    Fournit les boutons de restauration champ par champ et de copie.
    """

    def __init__(
        self,
        field_name: str,
        old_val: str,
        new_val: str,
        current_val: str,
        old_title: str,
        new_title: str,
        render_mode: str = "diff",
        on_restore_old: Optional[Callable[[], None]] = None,
        on_restore_new: Optional[Callable[[], None]] = None,
        parent=None,
    ):
        super().__init__(parent)
        self._c = get_colors()
        self.setStyleSheet(
            f"QFrame {{ background-color: {self._c['surface']}; border: 1px solid {self._c['border']}; "
            f"border-radius: 6px; }}"
        )
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(10, 8, 10, 10)
        layout.setSpacing(8)

        # ── En-tete du champ ─────────────────────────────────────────────────
        hdr_layout = QtWidgets.QHBoxLayout()
        hdr_layout.setContentsMargins(0, 0, 0, 0)
        hdr_layout.setSpacing(6)

        title_lbl = QtWidgets.QLabel(f"Champ : {field_name}")
        title_lbl.setStyleSheet(
            f"color: {self._c['primary']}; font-size: 12px; font-weight: 700; "
            f"background: transparent; border: none;"
        )
        hdr_layout.addWidget(title_lbl)

        # Badge de changement
        if not old_val.strip() and new_val.strip():
            badge_text = "[Ajouté]"
            badge_color = self._c.get('success', '#34D399')
        elif old_val.strip() and not new_val.strip():
            badge_text = "[Supprimé]"
            badge_color = self._c.get('danger', '#F87171')
        else:
            badge_text = "[Modifié]"
            badge_color = self._c.get('text_secondary', '#a6a6a6')

        badge_lbl = QtWidgets.QLabel(badge_text)
        badge_lbl.setStyleSheet(
            f"color: {badge_color}; font-size: 11px; font-weight: 600; "
            f"background: transparent; border: none;"
        )
        hdr_layout.addWidget(badge_lbl)
        hdr_layout.addStretch()

        layout.addLayout(hdr_layout)

        # ── Grille comparative (Avant | Apres) ──────────────────────────────
        grid = QtWidgets.QHBoxLayout()
        grid.setSpacing(8)

        # Colonne Gauche : AVANT
        left_box = self._create_side_box(
            side_title=old_title,
            value=old_val,
            other_value=new_val,
            is_new_side=False,
            current_val=current_val,
            render_mode=render_mode,
            on_restore=on_restore_old,
            restore_btn_label="Rétablir Avant",
        )
        grid.addWidget(left_box, stretch=1)

        # Colonne Droite : APRES
        right_box = self._create_side_box(
            side_title=new_title,
            value=new_val,
            other_value=old_val,
            is_new_side=True,
            current_val=current_val,
            render_mode=render_mode,
            on_restore=on_restore_new,
            restore_btn_label="Rétablir Après",
        )
        grid.addWidget(right_box, stretch=1)

        layout.addLayout(grid)

    def _create_side_box(
        self,
        side_title: str,
        value: str,
        other_value: str,
        is_new_side: bool,
        current_val: str,
        render_mode: str,
        on_restore: Optional[Callable[[], None]],
        restore_btn_label: str,
    ) -> QtWidgets.QFrame:
        c = self._c
        box = QtWidgets.QFrame()
        box.setStyleSheet(
            f"QFrame {{ background-color: {c['background']}; border: 1px solid {c['border']}; border-radius: 4px; }}"
        )
        box_layout = QtWidgets.QVBoxLayout(box)
        box_layout.setContentsMargins(8, 6, 8, 8)
        box_layout.setSpacing(6)

        # Barre superieure du box
        top_bar = QtWidgets.QHBoxLayout()
        top_bar.setContentsMargins(0, 0, 0, 0)
        top_bar.setSpacing(6)

        side_lbl = QtWidgets.QLabel(side_title)
        side_lbl.setStyleSheet(
            f"color: {c['text_secondary']}; font-size: 11px; font-weight: 600; "
            f"background: transparent; border: none;"
        )
        top_bar.addWidget(side_lbl)
        top_bar.addStretch()

        # Bouton Copier
        btn_copy = QtWidgets.QPushButton("Copier")
        btn_copy.setCursor(QtGui.QCursor(_Qt.CursorShape.PointingHandCursor))
        btn_copy.setStyleSheet(
            f"QPushButton {{ background: transparent; border: 1px solid {c['border']}; "
            f"border-radius: 3px; color: {c['text_secondary']}; font-size: 10px; padding: 2px 6px; }}"
            f"QPushButton:hover {{ color: {c['text_primary']}; background: {c['sel']}; border-color: {c['border']}; }}"
        )

        def _do_copy():
            app = QtWidgets.QApplication.instance()
            if app:
                clipboard = app.clipboard()
                if clipboard:
                    clipboard.setText(value)
                    btn_copy.setText("Copié !")
                    safe_timer_set_text(btn_copy, "Copier", 1500)

        btn_copy.clicked.connect(_do_copy)
        top_bar.addWidget(btn_copy)

        # Bouton Retablir
        is_already_active = (value == current_val)
        btn_restore = QtWidgets.QPushButton()
        btn_restore.setCursor(QtGui.QCursor(_Qt.CursorShape.PointingHandCursor))

        if is_already_active:
            btn_restore.setText("Actuel")
            btn_restore.setEnabled(False)
            btn_restore.setStyleSheet(
                f"QPushButton {{ background: transparent; border: 1px solid {c['border']}; "
                f"border-radius: 3px; color: {c['text_secondary']}; font-size: 10px; padding: 2px 6px; }}"
            )
        else:
            btn_restore.setText(restore_btn_label)
            btn_restore.setStyleSheet(
                f"QPushButton {{ background: {c['surface_high']}; border: 1px solid {c['accent']}; "
                f"border-radius: 3px; color: {c['text_primary']}; font-size: 10px; font-weight: 600; padding: 2px 8px; }}"
                f"QPushButton:hover {{ background: {c['accent']}; color: #ffffff; }}"
            )
            if on_restore:
                btn_restore.clicked.connect(on_restore)

        top_bar.addWidget(btn_restore)
        box_layout.addLayout(top_bar)

        # Contenu
        content_lbl = QtWidgets.QLabel()
        content_lbl.setWordWrap(True)
        content_lbl.setTextInteractionFlags(
            _Qt.TextInteractionFlag.TextSelectableByMouse | _Qt.TextInteractionFlag.TextSelectableByKeyboard
        )
        content_lbl.setStyleSheet(
            f"color: {c['text_primary']}; font-size: 12px; background: transparent; border: none; padding: 2px;"
        )

        if not value.strip():
            content_lbl.setText(f"<i style='color:{c['text_secondary']};'>(Vide)</i>")
        else:
            if render_mode == "diff":
                old_h, new_h = _render_diff_sides(value if not is_new_side else other_value,
                                                  other_value if not is_new_side else value,
                                                  c)
                content_lbl.setTextFormat(_Qt.TextFormat.RichText)
                content_lbl.setText(old_h if not is_new_side else new_h)
            elif render_mode == "raw":
                content_lbl.setTextFormat(_Qt.TextFormat.PlainText)
                content_lbl.setText(value)
                content_lbl.setStyleSheet(
                    f"color: {c['text_primary']}; font-family: 'Consolas', monospace; font-size: 11px; "
                    f"background: transparent; border: none; padding: 2px;"
                )
            else:  # render
                content_lbl.setTextFormat(_Qt.TextFormat.RichText)
                content_lbl.setText(value)

        box_layout.addWidget(content_lbl)
        return box


# ---------------------------------------------------------------------------
# TagsComparisonCard — Carte de comparaison pour les tags
# ---------------------------------------------------------------------------

class TagsComparisonCard(QtWidgets.QFrame):
    """Affiche la comparaison des tags entre deux versions avec boutons de restauration."""

    def __init__(
        self,
        old_tags: List[str],
        new_tags: List[str],
        current_tags: List[str],
        old_title: str,
        new_title: str,
        on_restore_old: Optional[Callable[[], None]] = None,
        on_restore_new: Optional[Callable[[], None]] = None,
        parent=None,
    ):
        super().__init__(parent)
        c = get_colors()
        self.setStyleSheet(
            f"QFrame {{ background-color: {c['surface']}; border: 1px solid {c['border']}; "
            f"border-radius: 6px; }}"
        )
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(10, 8, 10, 10)
        layout.setSpacing(8)

        # En-tete
        hdr_layout = QtWidgets.QHBoxLayout()
        title_lbl = QtWidgets.QLabel("Tags")
        title_lbl.setStyleSheet(
            f"color: {c['primary']}; font-size: 12px; font-weight: 700; "
            f"background: transparent; border: none;"
        )
        hdr_layout.addWidget(title_lbl)
        badge_lbl = QtWidgets.QLabel("[Modifié]")
        badge_lbl.setStyleSheet(
            f"color: {c.get('text_secondary', '#a6a6a6')}; font-size: 11px; font-weight: 600; "
            f"background: transparent; border: none;"
        )
        hdr_layout.addWidget(badge_lbl)
        hdr_layout.addStretch()
        layout.addLayout(hdr_layout)

        # Grille
        grid = QtWidgets.QHBoxLayout()
        grid.setSpacing(8)

        # Box gauche
        grid.addWidget(self._create_tag_box(
            side_title=old_title,
            tags=old_tags,
            current_tags=current_tags,
            restore_btn_label="Rétablir Tags Avant",
            on_restore=on_restore_old,
        ), stretch=1)

        # Box droit
        grid.addWidget(self._create_tag_box(
            side_title=new_title,
            tags=new_tags,
            current_tags=current_tags,
            restore_btn_label="Rétablir Tags Après",
            on_restore=on_restore_new,
        ), stretch=1)

        layout.addLayout(grid)

    def _create_tag_box(
        self,
        side_title: str,
        tags: List[str],
        current_tags: List[str],
        restore_btn_label: str,
        on_restore: Optional[Callable[[], None]],
    ) -> QtWidgets.QFrame:
        c = get_colors()
        box = QtWidgets.QFrame()
        box.setStyleSheet(
            f"QFrame {{ background-color: {c['background']}; border: 1px solid {c['border']}; border-radius: 4px; }}"
        )
        box_layout = QtWidgets.QVBoxLayout(box)
        box_layout.setContentsMargins(8, 6, 8, 8)
        box_layout.setSpacing(6)

        top_bar = QtWidgets.QHBoxLayout()
        side_lbl = QtWidgets.QLabel(side_title)
        side_lbl.setStyleSheet(
            f"color: {c['text_secondary']}; font-size: 11px; font-weight: 600; background: transparent; border: none;"
        )
        top_bar.addWidget(side_lbl)
        top_bar.addStretch()

        # Bouton Copier
        btn_copy = QtWidgets.QPushButton("Copier")
        btn_copy.setCursor(QtGui.QCursor(_Qt.CursorShape.PointingHandCursor))
        btn_copy.setStyleSheet(
            f"QPushButton {{ background: transparent; border: 1px solid {c['border']}; "
            f"border-radius: 3px; color: {c['text_secondary']}; font-size: 10px; padding: 2px 6px; }}"
        )

        def _do_copy():
            app = QtWidgets.QApplication.instance()
            if app:
                clipboard = app.clipboard()
                if clipboard:
                    clipboard.setText(" ".join(tags))
                    btn_copy.setText("Copié !")
                    safe_timer_set_text(btn_copy, "Copier", 1500)

        btn_copy.clicked.connect(_do_copy)
        top_bar.addWidget(btn_copy)

        is_already_active = (sorted(tags) == sorted(current_tags))
        btn_restore = QtWidgets.QPushButton()
        btn_restore.setCursor(QtGui.QCursor(_Qt.CursorShape.PointingHandCursor))
        if is_already_active:
            btn_restore.setText("Actuel")
            btn_restore.setEnabled(False)
            btn_restore.setStyleSheet(
                f"QPushButton {{ background: transparent; border: 1px solid {c['border']}; "
                f"border-radius: 3px; color: {c['text_secondary']}; font-size: 10px; padding: 2px 6px; }}"
            )
        else:
            btn_restore.setText(restore_btn_label)
            btn_restore.setStyleSheet(
                f"QPushButton {{ background: {c['surface_high']}; border: 1px solid {c['accent']}; "
                f"border-radius: 3px; color: {c['text_primary']}; font-size: 10px; font-weight: 600; padding: 2px 8px; }}"
                f"QPushButton:hover {{ background: {c['accent']}; color: #ffffff; }}"
            )
            if on_restore:
                btn_restore.clicked.connect(on_restore)

        top_bar.addWidget(btn_restore)
        box_layout.addLayout(top_bar)

        content_lbl = QtWidgets.QLabel()
        content_lbl.setWordWrap(True)
        content_lbl.setTextInteractionFlags(_Qt.TextInteractionFlag.TextSelectableByMouse)
        content_lbl.setStyleSheet(f"color: {c['text_primary']}; font-size: 12px; background: transparent; border: none;")

        if not tags:
            content_lbl.setText(f"<i style='color:{c['text_secondary']};'>Aucun tag</i>")
        else:
            chips = [
                f'<span style="background-color: {c["surface_high"]}; border: 1px solid {c["border"]}; '
                f'border-radius: 3px; padding: 1px 5px; font-size: 11px;">{_html.escape(t)}</span>'
                for t in tags
            ]
            content_lbl.setText(" ".join(chips))

        box_layout.addWidget(content_lbl)
        return box


# ---------------------------------------------------------------------------
# UnchangedFieldsCollapsible — Tiroir depliable pour champs inchanges
# ---------------------------------------------------------------------------

class UnchangedFieldsCollapsible(QtWidgets.QWidget):
    """Permet de deplier et visualiser/retablir les champs inchanges a cette version."""

    def __init__(
        self,
        unchanged_fields: Dict[str, str],
        current_fields: Dict[str, str],
        on_restore_field: Callable[[str, str], None],
        parent=None,
    ):
        super().__init__(parent)
        self._c = get_colors()
        self._unchanged = unchanged_fields
        self._current_fields = current_fields
        self._on_restore = on_restore_field
        self._expanded = False

        self.layout_root = QtWidgets.QVBoxLayout(self)
        self.layout_root.setContentsMargins(0, 4, 0, 4)
        self.layout_root.setSpacing(6)

        count = len(unchanged_fields)
        self.toggle_btn = QtWidgets.QPushButton(f"+ Afficher les {count} champs inchangés à cette version")
        self.toggle_btn.setCursor(QtGui.QCursor(_Qt.CursorShape.PointingHandCursor))
        self.toggle_btn.setStyleSheet(
            f"QPushButton {{ background: {self._c['surface']}; border: 1px solid {self._c['border']}; "
            f"border-radius: 4px; color: {self._c['text_secondary']}; font-size: 11px; padding: 6px 12px; text-align: left; }}"
            f"QPushButton:hover {{ background: {self._c['surface_hover']}; color: {self._c['text_primary']}; }}"
        )
        self.toggle_btn.clicked.connect(self._toggle)
        self.layout_root.addWidget(self.toggle_btn)

        self.container = QtWidgets.QWidget()
        self.container.setVisible(False)
        self.c_layout = QtWidgets.QVBoxLayout(self.container)
        self.c_layout.setContentsMargins(0, 0, 0, 0)
        self.c_layout.setSpacing(6)

        for fname, val in unchanged_fields.items():
            card = QtWidgets.QFrame()
            card.setStyleSheet(
                f"QFrame {{ background-color: {self._c['surface']}; border: 1px solid {self._c['border']}; "
                f"border-radius: 4px; padding: 4px; }}"
            )
            cl = QtWidgets.QHBoxLayout(card)
            cl.setContentsMargins(8, 4, 8, 4)
            cl.setSpacing(8)

            lbl = QtWidgets.QLabel(f"<b>{fname}</b>")
            lbl.setStyleSheet(f"color: {self._c['text_primary']}; font-size: 11px; border: none; background: transparent;")
            cl.addWidget(lbl)

            val_preview = val.strip()[:60] if val.strip() else "(Vide)"
            val_lbl = QtWidgets.QLabel(_html.escape(val_preview))
            val_lbl.setStyleSheet(f"color: {self._c['text_secondary']}; font-size: 11px; border: none; background: transparent;")
            cl.addWidget(val_lbl, stretch=1)

            btn_rest = QtWidgets.QPushButton("Rétablir ce champ")
            btn_rest.setCursor(QtGui.QCursor(_Qt.CursorShape.PointingHandCursor))
            is_active = (self._current_fields.get(fname, "") == val)
            if is_active:
                btn_rest.setText("Actuel")
                btn_rest.setEnabled(False)
                btn_rest.setStyleSheet(
                    f"QPushButton {{ background: transparent; border: 1px solid {self._c['border']}; "
                    f"border-radius: 3px; color: {self._c['text_secondary']}; font-size: 10px; padding: 2px 6px; }}"
                )
            else:
                btn_rest.setStyleSheet(
                    f"QPushButton {{ background: {self._c['surface_high']}; border: 1px solid {self._c['accent']}; "
                    f"border-radius: 3px; color: {self._c['text_primary']}; font-size: 10px; padding: 2px 6px; }}"
                    f"QPushButton:hover {{ background: {self._c['accent']}; color: #ffffff; }}"
                )

                def make_restore(fn=fname, v=val):
                    return lambda: self._on_restore(fn, v)

                btn_rest.clicked.connect(make_restore())

            cl.addWidget(btn_rest)
            self.c_layout.addWidget(card)

        self.layout_root.addWidget(self.container)

    def _toggle(self):
        self._expanded = not self._expanded
        self.container.setVisible(self._expanded)
        count = len(self._unchanged)
        if self._expanded:
            self.toggle_btn.setText(f"- Masquer les {count} champs inchangés")
        else:
            self.toggle_btn.setText(f"+ Afficher les {count} champs inchangés à cette version")


# ---------------------------------------------------------------------------
# AnkiCollabHistoryPanel — Panneau principal unifié (Historique deck perso)
# ---------------------------------------------------------------------------

class AnkiCollabHistoryPanel(QtWidgets.QWidget):
    """
    Panneau d'historique personnel et comparaison des champs modifies :
      - Liste gauche : Versions chronologiques (vN..v1) sans saturation de micro-frappes.
      - Panneau droit : Mode de comparaison des champs modifies (Avant | Apres),
        permettant la restauration granulaire champ par champ ou de toute la note.
      - Bouton de recherche dans les sauvegardes .colpkg d'Anki.

    Signaux :
      version_retrieved(dict) : {field_name: value} emis lors d'une restauration
      fields_protected(dict)  : compatibilite
      protection_tags_applied(list) : compatibilite
    """

    version_retrieved = _Signal(dict)
    fields_protected = _Signal(dict)
    protection_tags_applied = _Signal(list)
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
        self._history_db = NoteHistoryDB.instance()
        self._protection_db = ProtectedFieldsDB(self._history_db)
        self._bridge = AnkiCollabBridge.instance()

        self._history_deltas: List[VersionDelta] = []
        self._selected_delta: Optional[VersionDelta] = None
        self._filter_field: str = "ALL"
        self._render_mode: str = "diff"  # "diff", "render", "raw"
        self._is_scanning_backups = False

        AnkiCollabHistoryPanel._instances = [ref for ref in AnkiCollabHistoryPanel._instances if ref() is not None]
        AnkiCollabHistoryPanel._instances.append(weakref.ref(self))

        self._build_ui()
        self._load_data()

    # ── Construction de l'UI ─────────────────────────────────────────────

    def _build_ui(self):
        c = get_colors()
        self.setStyleSheet(
            f"QWidget {{ background-color: {c['background']}; color: {c['text_primary']}; "
            f"font-family: 'Segoe UI', 'Inter', sans-serif; }}"
        )

        root = QtWidgets.QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # ── En-tete principal ────────────────────────────────────────────────
        header = QtWidgets.QFrame()
        header.setStyleSheet(
            f"background-color: {c['header']}; border-bottom: 1px solid {c['border']};"
        )
        header.setFixedHeight(44)
        hl = QtWidgets.QHBoxLayout(header)
        hl.setContentsMargins(12, 0, 12, 0)
        hl.setSpacing(10)

        title = QtWidgets.QLabel("Historique de la note")
        title.setStyleSheet(
            f"color: {c['primary']}; font-size: 13px; font-weight: 700; background: transparent;"
        )
        hl.addWidget(title)

        self._note_info_lbl = QtWidgets.QLabel()
        self._note_info_lbl.setStyleSheet(
            f"color: {c['text_secondary']}; font-size: 11px; background: transparent;"
        )
        hl.addWidget(self._note_info_lbl)

        hl.addStretch()

        # Statut scan backups
        self._scan_status_lbl = QtWidgets.QLabel()
        self._scan_status_lbl.setStyleSheet(
            f"color: {c['text_secondary']}; font-size: 11px; background: transparent;"
        )
        hl.addWidget(self._scan_status_lbl)

        # Bouton Rechercher dans les sauvegardes Anki
        self._btn_scan_backups = QtWidgets.QPushButton("Rechercher dans les sauvegardes Anki")
        self._btn_scan_backups.setFixedHeight(26)
        self._btn_scan_backups.setCursor(QtGui.QCursor(_Qt.CursorShape.PointingHandCursor))
        self._btn_scan_backups.setStyleSheet(
            f"QPushButton {{ background: {c['surface']}; border: 1px solid {c['border']}; "
            f"border-radius: 3px; color: {c['text_primary']}; font-size: 11px; padding: 2px 10px; }}"
            f"QPushButton:hover {{ background: {c['surface_hover']}; border-color: {c['accent']}; }}"
        )
        self._btn_scan_backups.clicked.connect(self._on_scan_backups)
        hl.addWidget(self._btn_scan_backups)

        # Bouton Actualiser
        refresh_btn = QtWidgets.QPushButton("Actualiser")
        refresh_btn.setFixedHeight(26)
        refresh_btn.setCursor(QtGui.QCursor(_Qt.CursorShape.PointingHandCursor))
        refresh_btn.setStyleSheet(
            f"QPushButton {{ background: transparent; border: 1px solid {c['border']}; "
            f"border-radius: 3px; color: {c['text_secondary']}; font-size: 11px; padding: 2px 8px; }}"
            f"QPushButton:hover {{ color: {c['text_primary']}; background: {c['sel']}; }}"
        )
        refresh_btn.clicked.connect(self._load_data)
        hl.addWidget(refresh_btn)

        root.addWidget(header)

        # ── Barre d'outils secondaire (Filtres & Rendu) ───────────────────────
        filter_bar = QtWidgets.QFrame()
        filter_bar.setStyleSheet(
            f"background-color: {c['surface']}; border-bottom: 1px solid {c['border']};"
        )
        filter_bar.setFixedHeight(36)
        fl = QtWidgets.QHBoxLayout(filter_bar)
        fl.setContentsMargins(12, 0, 12, 0)
        fl.setSpacing(12)

        fl_lbl = QtWidgets.QLabel("Filtre :")
        fl_lbl.setStyleSheet(f"color: {c['text_secondary']}; font-size: 11px; background: transparent;")
        fl.addWidget(fl_lbl)

        self._field_filter_combo = QtWidgets.QComboBox()
        self._field_filter_combo.setFixedHeight(24)
        self._field_filter_combo.setStyleSheet(
            f"QComboBox {{ background: {c['surface_high']}; border: 1px solid {c['border']}; "
            f"border-radius: 3px; color: {c['text_primary']}; font-size: 11px; padding: 1px 8px; }}"
        )
        self._populate_filter_combo()
        self._field_filter_combo.currentIndexChanged.connect(self._on_filter_changed)
        fl.addWidget(self._field_filter_combo)

        fl.addSpacing(10)

        rm_lbl = QtWidgets.QLabel("Affichage :")
        rm_lbl.setStyleSheet(f"color: {c['text_secondary']}; font-size: 11px; background: transparent;")
        fl.addWidget(rm_lbl)

        self._render_mode_combo = QtWidgets.QComboBox()
        self._render_mode_combo.setFixedHeight(24)
        self._render_mode_combo.setStyleSheet(
            f"QComboBox {{ background: {c['surface_high']}; border: 1px solid {c['border']}; "
            f"border-radius: 3px; color: {c['text_primary']}; font-size: 11px; padding: 1px 8px; }}"
        )
        self._render_mode_combo.addItem("Diff visuel (Avant | Après)", "diff")
        self._render_mode_combo.addItem("Rendu HTML", "render")
        self._render_mode_combo.addItem("Code HTML brut", "raw")
        self._render_mode_combo.currentIndexChanged.connect(self._on_render_mode_changed)
        fl.addWidget(self._render_mode_combo)

        fl.addStretch()
        root.addWidget(filter_bar)

        # ── Corps : Splitter gauche (Versions) / droite (Comparaison) ─────────
        body = QtWidgets.QSplitter(_Qt.Orientation.Horizontal)
        body.setHandleWidth(1)
        body.setStyleSheet(f"QSplitter::handle {{ background: {c['border']}; }}")

        # Colonne Gauche : Liste des versions
        left = QtWidgets.QWidget()
        left.setStyleSheet(f"background-color: {c['surface']};")
        left.setMinimumWidth(230)
        ll = QtWidgets.QVBoxLayout(left)
        ll.setContentsMargins(0, 0, 0, 0)
        ll.setSpacing(0)

        self._list = QtWidgets.QListWidget()
        self._list.setStyleSheet(_get_style_list())
        self._list.currentRowChanged.connect(self._on_row_changed)
        ll.addWidget(self._list)
        body.addWidget(left)

        # Colonne Droite : Comparaison des champs modifies
        right = QtWidgets.QWidget()
        right.setStyleSheet(f"background-color: {c['background']};")
        rl = QtWidgets.QVBoxLayout(right)
        rl.setContentsMargins(12, 10, 12, 10)
        rl.setSpacing(8)

        # Banniere de version selectionnee (Actions globales)
        self._banner_frame = QtWidgets.QFrame()
        self._banner_frame.setStyleSheet(
            f"QFrame {{ background-color: {c['surface']}; border: 1px solid {c['border']}; border-radius: 4px; padding: 4px; }}"
        )
        bl = QtWidgets.QHBoxLayout(self._banner_frame)
        bl.setContentsMargins(8, 4, 8, 4)
        bl.setSpacing(8)

        self._banner_title = QtWidgets.QLabel()
        self._banner_title.setStyleSheet(
            f"color: {c['text_primary']}; font-size: 12px; font-weight: 600; border: none; background: transparent;"
        )
        bl.addWidget(self._banner_title)

        self._banner_badge = QtWidgets.QLabel()
        self._banner_badge.setStyleSheet(
            f"color: {c.get('success', '#34D399')}; font-size: 11px; font-weight: 600; border: none; background: transparent;"
        )
        bl.addWidget(self._banner_badge)

        bl.addStretch()

        self._btn_restore_all = QtWidgets.QPushButton("Rétablir toute la note à cette version")
        self._btn_restore_all.setCursor(QtGui.QCursor(_Qt.CursorShape.PointingHandCursor))
        self._btn_restore_all.setStyleSheet(
            f"QPushButton {{ background: {c['surface_high']}; border: 1px solid {c['accent']}; "
            f"border-radius: 3px; color: {c['text_primary']}; font-size: 11px; font-weight: 600; padding: 4px 12px; }}"
            f"QPushButton:hover {{ background: {c['accent']}; color: #ffffff; }}"
        )
        self._btn_restore_all.clicked.connect(self._on_restore_full_version)
        bl.addWidget(self._btn_restore_all)

        rl.addWidget(self._banner_frame)

        # Scroll area des cartes de champs
        self._scroll = QtWidgets.QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
        self._scroll.setStyleSheet(f"background-color: {c['background']}; border: none;")

        self._cards_container = QtWidgets.QWidget()
        self._cards_container.setStyleSheet(f"background-color: {c['background']};")
        self._cards_layout = QtWidgets.QVBoxLayout(self._cards_container)
        self._cards_layout.setContentsMargins(0, 0, 0, 0)
        self._cards_layout.setSpacing(10)
        self._cards_layout.addStretch()

        self._scroll.setWidget(self._cards_container)
        rl.addWidget(self._scroll, stretch=1)

        body.addWidget(right)
        body.setStretchFactor(0, 0)
        body.setStretchFactor(1, 1)
        body.setSizes([240, 520])

        root.addWidget(body, stretch=1)

    # ── Chargement des donnees ───────────────────────────────────────────

    def _load_data(self):
        """Charge l'historique et calcule les deltas de comparaison."""
        self._history_deltas = self._history_db.get_history_with_deltas(
            self._note_id,
            current_fields=self._current_fields,
        )
        ver_count = len(self._history_deltas)
        self._note_info_lbl.setText(f"Note #{self._note_id} · {ver_count} version{'s' if ver_count > 1 else ''}")
        self._populate_list()

    def _populate_filter_combo(self):
        self._field_filter_combo.blockSignals(True)
        self._field_filter_combo.clear()
        self._field_filter_combo.addItem("Tous les champs modifiés", "ALL")
        for fn in self._field_names:
            if fn != "__tags__":
                self._field_filter_combo.addItem(f"Champ : {fn}", fn)
        self._field_filter_combo.addItem("Tags uniquement", "__tags__")
        self._field_filter_combo.blockSignals(False)

    def _populate_list(self):
        self._list.blockSignals(True)
        self._list.clear()

        if not self._history_deltas:
            empty = QtWidgets.QListWidgetItem("  Aucun historique disponible")
            empty.setFlags(_Qt.ItemFlag.NoItemFlags)
            empty.setForeground(QtGui.QColor(str(_CLR_SUBTEXT)))
            self._list.addItem(empty)
            self._list.blockSignals(False)
            self._render_comparison_view()
            return

        for idx, delta in enumerate(self._history_deltas):
            item = QtWidgets.QListWidgetItem()
            item.setData(_Qt.ItemDataRole.UserRole, delta)

            v_entry = delta.version
            v_label = f"v{delta.version_number}"
            src_tag = v_entry.source_icon
            date_str = v_entry.timestamp_str

            # Resume des modifications
            if not delta.previous_version:
                summary = "Version initiale de la note"
            else:
                changed_names = list(delta.changed_fields.keys())
                if changed_names and delta.tags_changed:
                    summary = f"{len(changed_names)} champ(s) + tags modifiés"
                elif changed_names:
                    sample = ", ".join(changed_names[:2])
                    if len(changed_names) > 2:
                        sample += f" (+{len(changed_names)-2})"
                    summary = f"{len(changed_names)} champ(s) : {sample}"
                elif delta.tags_changed:
                    summary = "Tags modifiés"
                else:
                    summary = "Modifications mineures"

            text = f"{v_label} {src_tag}  {date_str}\n  {summary}"
            item.setText(text)
            item.setSizeHint(QtCore.QSize(220, 44))
            self._list.addItem(item)

        self._list.blockSignals(False)
        self._list.setCurrentRow(0)

    # ── Selection & Affichage de comparaison ─────────────────────────────

    def _on_row_changed(self, row: int):
        if row < 0:
            self._selected_delta = None
        else:
            item = self._list.item(row)
            if item:
                self._selected_delta = item.data(_Qt.ItemDataRole.UserRole)
            else:
                self._selected_delta = None
        self._render_comparison_view()

    def _on_filter_changed(self):
        self._filter_field = self._field_filter_combo.currentData() or "ALL"
        self._render_comparison_view()

    def _on_render_mode_changed(self):
        self._render_mode = self._render_mode_combo.currentData() or "diff"
        self._render_comparison_view()

    def _render_comparison_view(self):
        # Vider les cartes actuelles
        layout = self._cards_layout
        while layout.count() > 1:
            item = layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        if not self._selected_delta:
            self._banner_frame.setVisible(False)
            placeholder = QtWidgets.QLabel(
                f"<i style='color:{_CLR_SUBTEXT};'>Sélectionnez une version dans la liste</i>"
            )
            placeholder.setAlignment(_Qt.AlignmentFlag.AlignCenter)
            placeholder.setStyleSheet("background: transparent;")
            layout.insertWidget(0, placeholder)
            return

        delta = self._selected_delta
        self._banner_frame.setVisible(True)

        v_lbl = f"Version v{delta.version_number}"
        v_date = delta.version.timestamp_str
        v_comment = delta.version.comment or delta.version.source_icon
        self._banner_title.setText(f"{v_lbl} · {v_date}  ({v_comment})")

        if delta.is_current:
            self._banner_badge.setText("[Version actuelle]")
            self._banner_badge.setVisible(True)
        else:
            self._banner_badge.setVisible(False)

        # Determiner les labels des deux cotes
        if delta.previous_version:
            prev_num = delta.version_number - 1
            old_title = f"AVANT (v{prev_num} — {delta.previous_version.timestamp_str})"
            new_title = f"APRÈS (v{delta.version_number} — {delta.version.timestamp_str})"
        else:
            old_title = "AVANT (Création)"
            new_title = f"APRÈS (v1 — {delta.version.timestamp_str})"

        # Filtrer les champs
        all_changed = delta.changed_fields
        filtered_fnames = []
        if self._filter_field == "ALL":
            filtered_fnames = [fn for fn in self._field_names if fn in all_changed]
            # Ajouter les champs eventuellement dans changed_fields qui ne seraient pas dans field_names
            for k in all_changed.keys():
                if k not in filtered_fnames and k != "__tags__":
                    filtered_fnames.append(k)
        elif self._filter_field != "__tags__":
            if self._filter_field in all_changed:
                filtered_fnames = [self._filter_field]

        insert_idx = 0

        # Afficher les cartes de champs modifies
        for fname in filtered_fnames:
            f_delta = all_changed[fname]
            old_val = f_delta.old_value
            new_val = f_delta.new_value
            curr_val = self._current_fields.get(fname, "")

            def make_restore_old(fn=fname, v=old_val):
                return lambda: self._on_restore_field(fn, v)

            def make_restore_new(fn=fname, v=new_val):
                return lambda: self._on_restore_field(fn, v)

            card = FieldComparisonCard(
                field_name=fname,
                old_val=old_val,
                new_val=new_val,
                current_val=curr_val,
                old_title=old_title,
                new_title=new_title,
                render_mode=self._render_mode,
                on_restore_old=make_restore_old(),
                on_restore_new=make_restore_new(),
                parent=self._cards_container,
            )
            layout.insertWidget(insert_idx, card)
            insert_idx += 1

        # Afficher la carte de tags si modifiee et concordante avec le filtre
        show_tags = (self._filter_field in ("ALL", "__tags__")) and delta.tags_changed
        if show_tags:
            old_t = delta.old_tags
            new_t = delta.new_tags
            curr_t = [t for t in self._current_fields.get("__tags__", "").strip().split(" ") if t]

            def make_restore_old_tags():
                return lambda: self._on_restore_field("__tags__", " ".join(old_t))

            def make_restore_new_tags():
                return lambda: self._on_restore_field("__tags__", " ".join(new_t))

            tags_card = TagsComparisonCard(
                old_tags=old_t,
                new_tags=new_t,
                current_tags=curr_t,
                old_title=old_title,
                new_title=new_title,
                on_restore_old=make_restore_old_tags(),
                on_restore_new=make_restore_new_tags(),
                parent=self._cards_container,
            )
            layout.insertWidget(insert_idx, tags_card)
            insert_idx += 1

        # Si aucun changement filtre
        if not filtered_fnames and not show_tags:
            msg = "Aucun champ modifié à cette version correspondant au filtre actif."
            if not delta.previous_version:
                msg = "Version initiale de la note : tous les champs créés sont consultables ci-dessous."
            empty_lbl = QtWidgets.QLabel(f"<i style='color:{_CLR_SUBTEXT};'>{msg}</i>")
            empty_lbl.setAlignment(_Qt.AlignmentFlag.AlignCenter)
            empty_lbl.setStyleSheet("background: transparent; padding: 12px;")
            layout.insertWidget(insert_idx, empty_lbl)
            insert_idx += 1

        # Tiroir depliable pour les champs inchanges a cette version
        unchanged = {
            fn: delta.all_fields.get(fn, "")
            for fn in self._field_names
            if fn not in delta.changed_fields and fn != "__tags__"
        }
        if unchanged:
            collapsible = UnchangedFieldsCollapsible(
                unchanged_fields=unchanged,
                current_fields=self._current_fields,
                on_restore_field=self._on_restore_field,
                parent=self._cards_container,
            )
            layout.insertWidget(insert_idx, collapsible)

    # ── Actions de restauration ──────────────────────────────────────────

    def _on_restore_field(self, fname: str, val: str):
        """Restaure un champ specifique."""
        if self._current_fields is None:
            self._current_fields = {}
        self._current_fields[fname] = val
        self.version_retrieved.emit({fname: val})
        self._render_comparison_view()

    def _on_restore_full_version(self):
        """Restaure tous les champs et tags a la version selectionnee."""
        if not self._selected_delta:
            return
        fields_to_restore = dict(self._selected_delta.all_fields)
        if self._selected_delta.version.tags:
            fields_to_restore["__tags__"] = " ".join(self._selected_delta.version.tags)
        if self._current_fields is None:
            self._current_fields = {}
        self._current_fields.update(fields_to_restore)
        self.version_retrieved.emit(fields_to_restore)
        self._render_comparison_view()

    # ── Exploration des sauvegardes .colpkg Anki ─────────────────────────

    def _on_scan_backups(self):
        if self._is_scanning_backups:
            return
        self._is_scanning_backups = True
        self._btn_scan_backups.setEnabled(False)
        self._scan_status_lbl.setText("Recherche dans les sauvegardes Anki...")

        note_id = self._note_id

        def _bg_scan():
            def _progress(curr, tot, found):
                msg = f"Recherche ({curr}/{tot})... {found} version(s) trouvée(s)"
                QtCore.QTimer.singleShot(0, lambda: self._scan_status_lbl.setText(msg))

            try:
                self._history_db.scan_backups_for_note(note_id, progress_callback=_progress)
            except Exception as e:
                print(f"[anki-diff] Error scanning backups: {e}")

            QtCore.QTimer.singleShot(0, _on_done)

        def _on_done():
            self._is_scanning_backups = False
            self._btn_scan_backups.setEnabled(True)
            self._scan_status_lbl.setText("Sauvegardes indexées.")
            QtCore.QTimer.singleShot(3000, lambda: self._scan_status_lbl.setText(""))
            self._load_data()

        threading.Thread(target=_bg_scan, daemon=True).start()

    # ── API publique de synchronisation ──────────────────────────────────

    def set_note(self, note_id: int, field_names: List[str],
                 current_fields: Optional[Dict[str, str]] = None) -> None:
        """Change la note affichee."""
        is_same = (note_id == self._note_id)
        self._note_id = note_id
        self._field_names = field_names
        self._current_fields = current_fields or {}

        if is_same:
            self._render_comparison_view()
            return

        self._populate_filter_combo()
        self._load_data()

    def update_to_note(self, note_id: int, field_names: List[str],
                       current_fields: Optional[Dict[str, str]] = None) -> None:
        self.set_note(note_id, field_names, current_fields)
        win = self.window()
        if win and isinstance(win, QtWidgets.QDialog):
            win.setWindowTitle(f"Historique · Note {note_id}")

    def store_current_snapshot(self, source: str = "import", comment: str = "") -> None:
        if self._current_fields:
            self._history_db.store_snapshot(
                self._note_id, self._current_fields, source=source, comment=comment
            )
            self._load_data()


# ---------------------------------------------------------------------------
# Blocs de compatibilite legues (preserves pour tests ou code existant)
# ---------------------------------------------------------------------------

class FieldPreviewBlock(QtWidgets.QFrame):
    """Bloc de previsualisation de champ conserve pour compatibilite."""
    def __init__(self, field_name: str, value: str, has_changed: bool = False, on_retrieve: Optional[Callable[[], None]] = None, parent=None):
        super().__init__(parent)
        c = get_colors()
        self.setStyleSheet(f"QFrame {{ background-color: {c['surface']}; border: 1px solid {c['border']}; border-radius: 4px; }}")
        l = QtWidgets.QVBoxLayout(self)
        l.setContentsMargins(6, 4, 6, 4)
        lbl = QtWidgets.QLabel(f"<b>{field_name}</b>: {_html.escape(value[:60])}")
        lbl.setStyleSheet(f"color: {c['text_primary']}; border: none; background: transparent;")
        l.addWidget(lbl)


class TagsPreviewBlock(QtWidgets.QFrame):
    """Bloc de previsualisation de tags conserve pour compatibilite."""
    def __init__(self, old_tags: List[str], current_tags: List[str], has_changed: bool = False, on_retrieve: Optional[Callable[[], None]] = None, parent=None):
        super().__init__(parent)
        c = get_colors()
        self.setStyleSheet(f"QFrame {{ background-color: {c['surface']}; border: 1px solid {c['border']}; border-radius: 4px; }}")
        l = QtWidgets.QVBoxLayout(self)
        l.setContentsMargins(6, 4, 6, 4)
        lbl = QtWidgets.QLabel(f"Tags: {' '.join(old_tags)}")
        lbl.setStyleSheet(f"color: {c['text_primary']}; border: none; background: transparent;")
        l.addWidget(lbl)


class VersionLegendBlock(QtWidgets.QFrame):
    """Bloc de legende conserve pour compatibilite."""
    def __init__(self, kind: str, obj: Any, parent=None):
        super().__init__(parent)
        c = get_colors()
        self.setStyleSheet(f"QFrame {{ background-color: {c['header']}; border: 1px solid {c['border']}; border-radius: 4px; }}")
        l = QtWidgets.QVBoxLayout(self)
        l.setContentsMargins(6, 4, 6, 4)
        lbl = QtWidgets.QLabel(f"Version : {kind}")
        lbl.setStyleSheet(f"color: {c['primary']}; border: none; background: transparent;")
        l.addWidget(lbl)


# ---------------------------------------------------------------------------
# AnkiCollabHistoryDialog — Fenetre standalone
# ---------------------------------------------------------------------------

class AnkiCollabHistoryDialog(QtWidgets.QDialog):
    """Fenetre autonome ouvrable depuis les menus pour consulter l'historique d'une note."""

    def __init__(
        self,
        note_id: int,
        field_names: List[str],
        current_fields: Optional[Dict[str, str]] = None,
        on_retrieve: Optional[Callable[[Dict[str, str]], None]] = None,
        parent=None,
    ):
        super().__init__(parent)
        self._on_retrieve = on_retrieve
        self.setWindowTitle(f"Historique · Note {note_id}")
        self.resize(850, 520)
        c = get_colors()
        self.setStyleSheet(
            f"QDialog {{ background-color: {c['background']}; color: {c['text_primary']}; "
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

        footer = QtWidgets.QFrame()
        footer.setStyleSheet(
            f"background-color: {c['header']}; border-top: 1px solid {c['border']};"
        )
        footer.setFixedHeight(44)
        fl = QtWidgets.QHBoxLayout(footer)
        fl.setContentsMargins(12, 0, 12, 0)
        fl.addStretch()
        close_btn = QtWidgets.QPushButton("Fermer")
        close_btn.setStyleSheet(
            f"QPushButton {{ background: {c['surface_high']}; color: {c['text_primary']}; "
            f"border: 1px solid {c['border']}; border-radius: 4px; padding: 4px 16px; }}"
            f"QPushButton:hover {{ background: {c['surface_hover']}; }}"
        )
        close_btn.clicked.connect(self.accept)
        fl.addWidget(close_btn)
        layout.addWidget(footer)

    @property
    def panel(self) -> AnkiCollabHistoryPanel:
        return self._panel
