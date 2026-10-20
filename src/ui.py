"""
ui.py — Interface moderne de Diff Hunk et Historique pour Anki Import Diff.

Inspiré de l'ergonomie et du design de Smart Search Medical :
- Palette dynamique (PaletteMixin, chrome_colors, mode sombre/clair automatique)
- Cartes de Hunks épurées avec micro-contrôles segmentés et indicateurs vectoriels
- Contrôles segmentés en pilules (Diff Hunk / Historique)
- En-tête avec pastilles d'état _StatusDot et barre de filtres interactive
"""

from __future__ import annotations

import html
import re
from typing import List, Optional, Dict

try:
    from PyQt6 import QtCore, QtGui, QtWidgets
    _Qt = QtCore.Qt
    _CheckState = QtCore.Qt.CheckState
    _ItemFlag = QtCore.Qt.ItemFlag
    _Role = QtCore.Qt.ItemDataRole
    _AlignFlag = QtCore.Qt.AlignmentFlag
    _Signal = QtCore.pyqtSignal
except ImportError:
    from PySide6 import QtCore, QtGui, QtWidgets
    _Qt = QtCore.Qt
    _CheckState = QtCore.Qt.CheckState
    _ItemFlag = QtCore.Qt.ItemFlag
    _Role = QtCore.Qt.ItemDataRole
    _AlignFlag = QtCore.Qt.AlignmentFlag
    _Signal = QtCore.Signal

from .savepoint import NoteDiff
from .diff import DiffHunk, compute_hunks, apply_hunks, highlight_diff
from .theme import chrome_colors, blend_colors, PaletteMixin, _StatusDot, ThemeColor

# ---------------------------------------------------------------------------
# AnkiWebView integration — fallback si Anki n'est pas actif (tests)
# ---------------------------------------------------------------------------

try:
    from aqt.webview import AnkiWebView as _AnkiWebView  # type: ignore
    import aqt as _aqt  # type: ignore
except ImportError:
    _AnkiWebView = None  # type: ignore
    _aqt = None  # type: ignore


def _ankiweb_available() -> bool:
    """True quand une session Anki complète est active avec mediaServer."""
    try:
        mw = getattr(_aqt, "mw", None) if _aqt is not None else None
        return (
            _AnkiWebView is not None
            and mw is not None
            and getattr(mw, "mediaServer", None) is not None
        )
    except Exception:
        return False


def safe_timer_set_text(button, text: str, delay_ms: int = 1500) -> None:
    """Modifie le texte du bouton apres un delai sans crash si l'objet C++ est detruit."""
    def _cb():
        try:
            from aqt.qt import sip
            if not sip.isdeleted(button):
                button.setText(text)
        except Exception:
            pass
    QtCore.QTimer.singleShot(delay_ms, _cb)


class _DiffWebView(QtWidgets.QWidget):
    """Wrapper mince autour d'AnkiWebView avec shims QTextBrowser pour les tests."""

    def __init__(self, parent=None, min_h: int = 80, max_h: int = 140):
        super().__init__(parent)
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self._web = _AnkiWebView(parent=self, title="import-diff-preview")
        layout.addWidget(self._web)
        self.setMinimumHeight(min_h)
        self.setMaximumHeight(max_h)
        self._last_plain: str = ""

    def stdHtml(self, body: str, *args, **kwargs):
        self._web.stdHtml(body, *args, **kwargs)

    def setHtml(self, html_str: str):
        self._web.setHtml(html_str)

    def toPlainText(self) -> str:
        return self._last_plain

    def clear(self):
        self._last_plain = ""
        self._web.setHtml("")


def _make_preview_widget(parent=None, min_h: int = 80, max_h: int = 140) -> QtWidgets.QWidget:
    if _ankiweb_available():
        return _DiffWebView(parent=parent, min_h=min_h, max_h=max_h)
    c = chrome_colors(parent)
    tb = QtWidgets.QTextBrowser(parent)
    tb.setStyleSheet(
        f"background-color:{c['base']}; color:{c['text']}; "
        f"border:1px solid {c['border']}; border-radius:6px; "
        f"font-family:sans-serif; font-size:12px; padding:6px;"
    )
    tb.setMinimumHeight(min_h)
    tb.setMaximumHeight(max_h)
    return tb


def _set_preview_html(widget: QtWidgets.QWidget, body_html: str, model_css: str = "") -> None:
    if isinstance(widget, _DiffWebView):
        extra_css = f"<style>{model_css}</style>" if model_css else ""
        widget._last_plain = body_html
        widget.stdHtml(
            f"{extra_css}"
            f'<div style="padding:8px;word-wrap:break-word;">{body_html}</div>'
        )
    else:
        c = chrome_colors(widget)
        full = (
            '<html><head><style>'
            f'body{{background:{c["base"]};color:{c["text"]};'
            f'font-family:sans-serif;font-size:12px;margin:6px;word-wrap:break-word;}}'
            f'{model_css}'
            '</style></head>'
            f'<body>{body_html}</body></html>'
        )
        widget.setHtml(full)


class _AutoResizingTextBrowser(QtWidgets.QTextBrowser):
    """
    QTextBrowser sans ascenseurs qui adapte sa hauteur au contenu exact,
    garantit la sélection fluide au curseur I-Beam, le copier-coller Ctrl+C
    et un menu contextuel complet pour copier la sélection.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setReadOnly(True)
        self.setOpenExternalLinks(False)
        self.setOpenLinks(False)
        self.setTextInteractionFlags(
            _Qt.TextInteractionFlag.TextSelectableByMouse |
            _Qt.TextInteractionFlag.TextSelectableByKeyboard
        )
        self.viewport().setCursor(QtGui.QCursor(_Qt.CursorShape.IBeamCursor))
        self.setCursor(QtGui.QCursor(_Qt.CursorShape.IBeamCursor))
        self.setFocusPolicy(_Qt.FocusPolicy.ClickFocus)
        self.setVerticalScrollBarPolicy(_Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setHorizontalScrollBarPolicy(_Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        try:
            self.setSizePolicy(
                QtWidgets.QSizePolicy.Policy.Expanding,
                QtWidgets.QSizePolicy.Policy.Fixed
            )
        except AttributeError:
            self.setSizePolicy(
                QtWidgets.QSizePolicy.Expanding,
                QtWidgets.QSizePolicy.Fixed
            )
        self.setLineWrapMode(QtWidgets.QTextBrowser.LineWrapMode.WidgetWidth)
        self.document().setDocumentMargin(6)
        self._adjusting_height = False  # guard contre la réentrance
        self.document().documentLayout().documentSizeChanged.connect(self._adjust_height)

    def _adjust_height(self):
        # Guard de réentrance : setFixedHeight peut provoquer un nouveau documentSizeChanged
        # lors d'un double-clic (sélection de mot), créant une boucle et freezant l'UI.
        if self._adjusting_height:
            return
        self._adjusting_height = True
        try:
            vp_rect = self.viewport().contentsRect()
            w = vp_rect.width() if vp_rect.width() > 10 else (self.width() - 2 * self.frameWidth())
            if w > 10:
                self.document().setTextWidth(w)
            doc_h = int(self.document().size().height()) + 8
            self.setFixedHeight(max(doc_h, 28))
        finally:
            self._adjusting_height = False

    def resizeEvent(self, e):
        super().resizeEvent(e)
        vp_rect = self.viewport().contentsRect()
        w = vp_rect.width() if vp_rect.width() > 10 else (e.size().width() - 2 * self.frameWidth())
        if w > 10:
            self.document().setTextWidth(w)
        self._adjust_height()

    def setHtml(self, html_str: str):
        self.document().setDocumentMargin(6)
        super().setHtml(html_str)
        self.viewport().setCursor(QtGui.QCursor(_Qt.CursorShape.IBeamCursor))
        self._adjust_height()


    def keyPressEvent(self, event: QtGui.QKeyEvent):
        if event.matches(QtGui.QKeySequence.StandardKey.Copy) or (
            event.key() == _Qt.Key.Key_C and event.modifiers() == _Qt.KeyboardModifier.ControlModifier
        ):
            self.copy()
            event.accept()
            return
        elif event.matches(QtGui.QKeySequence.StandardKey.SelectAll) or (
            event.key() == _Qt.Key.Key_A and event.modifiers() == _Qt.KeyboardModifier.ControlModifier
        ):
            self.selectAll()
            event.accept()
            return
        elif event.key() in (_Qt.Key.Key_Up, _Qt.Key.Key_Down) and not event.modifiers():
            event.ignore()
            return
        super().keyPressEvent(event)

    def contextMenuEvent(self, event: QtGui.QContextMenuEvent):
        menu = QtWidgets.QMenu(self)
        c = chrome_colors(self)
        menu.setStyleSheet(f"""
            QMenu {{
                background-color: {c['surface']};
                color: {c['text_primary']};
                border: 1px solid {c['border']};
                border-radius: 6px;
                padding: 4px;
                font-size: 12px;
            }}
            QMenu::item {{
                padding: 5px 20px 5px 12px;
                border-radius: 4px;
            }}
            QMenu::item:selected {{
                background-color: {c['sel']};
                color: {c['text_primary']};
            }}
            QMenu::separator {{
                height: 1px;
                background-color: {c['border']};
                margin: 4px 6px;
            }}
        """)
        has_sel = bool(self.textCursor().hasSelection())
        act_copy = menu.addAction("Copier la sélection" if has_sel else "Copier tout le champ")
        act_copy.setShortcut(QtGui.QKeySequence("Ctrl+C"))
        def _do_copy():
            if has_sel:
                self.copy()
            else:
                app = QtWidgets.QApplication.instance()
                if app and app.clipboard():
                    app.clipboard().setText(self.toPlainText())
        act_copy.triggered.connect(_do_copy)

        menu.addSeparator()
        act_select_all = menu.addAction("Tout sélectionner")
        act_select_all.setShortcut(QtGui.QKeySequence("Ctrl+A"))
        act_select_all.triggered.connect(self.selectAll)

        menu.exec(event.globalPos())


# ---------------------------------------------------------------------------
# Couleurs et compatibilité avec l'ancien système
# ---------------------------------------------------------------------------

def get_colors() -> dict:
    """Retourne la palette dérivée dynamique."""
    return chrome_colors()


_CLR_ADD_BG    = ThemeColor('add_bg')
_CLR_ADD_FG    = ThemeColor('add_fg')
_CLR_ADD_BDR   = ThemeColor('add_border')
_CLR_REM_BG    = ThemeColor('rem_bg')
_CLR_REM_FG    = ThemeColor('rem_fg')
_CLR_REM_BDR   = ThemeColor('rem_border')
_CLR_CTX_BG    = ThemeColor('background')
_CLR_CTX_FG    = ThemeColor('text_muted')
_CLR_HDR_BG    = ThemeColor('header')
_CLR_HDR_FG    = ThemeColor('primary')
_CLR_PANEL     = ThemeColor('background')
_CLR_SIDEBAR   = ThemeColor('surface')
_CLR_BORDER    = ThemeColor('border')
_CLR_SEL       = ThemeColor('sel')
_CLR_BTN_ACC   = ThemeColor('ok')
_CLR_BTN_ACC_H = ThemeColor('primary_hover')
_CLR_BTN_REJ   = ThemeColor('err')
_CLR_BTN_REJ_H = ThemeColor('primary_hover')
_CLR_TEXT      = ThemeColor('text_primary')
_CLR_SUBTEXT   = ThemeColor('text_secondary')
_CLR_PREVIEW   = ThemeColor('preview')
_CLR_ACCENT    = ThemeColor('primary')
_CLR_WARN      = ThemeColor('warning')


def _get_style_base() -> str:
    c = chrome_colors()
    return f"""
QDialog, QWidget {{
    background-color: {c['background']};
    color: {c['text_primary']};
    font-family: "Segoe UI", "Inter", -apple-system, sans-serif;
    font-size: 13px;
}}
QSplitter::handle {{
    background: {c['border']};
    width: 1px;
}}
QScrollBar:vertical {{
    background: transparent;
    width: 8px;
    margin: 0;
}}
QScrollBar::handle:vertical {{
    background: {c['border_strong']};
    border-radius: 4px;
    min-height: 24px;
}}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
    height: 0px;
}}
QScrollBar:horizontal {{
    background: transparent;
    height: 8px;
    margin: 0;
}}
QScrollBar::handle:horizontal {{
    background: {c['border_strong']};
    border-radius: 4px;
    min-width: 24px;
}}
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{
    width: 0px;
}}
"""


def _get_style_sidebar() -> str:
    c = chrome_colors()
    return f"""
QListWidget {{
    background-color: {c['surface']};
    border: none;
    border-right: 1px solid {c['border']};
    outline: none;
    padding: 4px 0px;
}}
QListWidget::item {{
    padding: 8px 12px;
    border-bottom: 1px solid {c['border']};
    border-radius: 6px;
    margin: 2px 4px;
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


def _get_style_filter_bar() -> str:
    c = chrome_colors()
    return f"""
QWidget {{
    background-color: {c['surface']};
    border-bottom: 1px solid {c['border']};
}}
QToolButton, QPushButton {{
    background: {c['chip_bg']};
    color: {c['text_secondary']};
    border: 1px solid {c['chip_border']};
    padding: 3px 10px;
    font-size: 11px;
    font-weight: 600;
    border-radius: 11px;
}}
QToolButton:hover, QPushButton:hover {{
    color: {c['text_primary']};
    background: {c['surface_hover']};
    border-color: {c['accent_mid']};
}}
QToolButton:checked, QPushButton:checked {{
    color: {c['accent_text']};
    background: {c['accent']};
    border-color: {c['accent']};
}}
"""


def _get_style_btn_accept() -> str:
    c = chrome_colors()
    return f"""
QPushButton, QToolButton {{
    background-color: {c['add_bg']};
    color: {c['add_fg']};
    border: 1px solid {c['add_border']};
    border-radius: 6px;
    padding: 3px 10px;
    font-size: 11px;
    font-weight: 600;
}}
QPushButton:hover, QToolButton:hover {{
    background-color: {c['ok']};
    color: #ffffff;
    border-color: {c['ok']};
}}
QPushButton:disabled, QToolButton:disabled {{
    background-color: {c['surface']};
    border-color: {c['border']};
    color: {c['text_muted']};
}}
"""


def _get_style_btn_reject() -> str:
    c = chrome_colors()
    return f"""
QPushButton, QToolButton {{
    background-color: {c['rem_bg']};
    color: {c['rem_fg']};
    border: 1px solid {c['rem_border']};
    border-radius: 6px;
    padding: 3px 10px;
    font-size: 11px;
    font-weight: 600;
}}
QPushButton:hover, QToolButton:hover {{
    background-color: {c['err']};
    color: #ffffff;
    border-color: {c['err']};
}}
QPushButton:disabled, QToolButton:disabled {{
    background-color: {c['surface']};
    border-color: {c['border']};
    color: {c['text_muted']};
}}
"""


def _get_style_btn_primary() -> str:
    c = chrome_colors()
    return f"""
QPushButton {{
    background-color: {c['accent']};
    color: {c['accent_text']};
    border: 1px solid {c['accent']};
    border-radius: 8px;
    padding: 7px 22px;
    font-size: 13px;
    font-weight: 600;
}}
QPushButton:hover {{
    background-color: {c['accent_hover']};
}}
QPushButton:pressed {{
    background-color: {c['accent_hover']};
}}
"""


def _get_style_btn_secondary() -> str:
    c = chrome_colors()
    return f"""
QPushButton {{
    background-color: {c['surface_high']};
    color: {c['text_primary']};
    border: 1px solid {c['chip_border']};
    border-radius: 8px;
    padding: 7px 18px;
    font-size: 13px;
    font-weight: 500;
}}
QPushButton:hover {{
    background-color: {c['surface_hover']};
    border-color: {c['accent_mid']};
}}
"""


def _get_style_btn_toggle() -> str:
    """Style pour les boutons toggle à pilule (Apercu, Comparer)."""
    c = chrome_colors()
    return f"""
QPushButton {{
    background-color: {c['chip_bg']};
    color: {c['text_secondary']};
    border: 1px solid {c['chip_border']};
    border-radius: 11px;
    padding: 2px 9px;
    font-size: 11px;
    font-weight: 500;
}}
QPushButton:hover {{
    color: {c['text_primary']};
    background: {c['surface_hover']};
    border-color: {c['accent_mid']};
}}
QPushButton:checked {{
    color: {c['accent_text']};
    background: {c['accent']};
    border-color: {c['accent']};
    font-weight: 600;
}}
"""


def _get_style_progress() -> str:
    c = chrome_colors()
    return f"""
QProgressBar {{
    background-color: {c['border']};
    border: none;
    border-radius: 2px;
    height: 3px;
    text-align: center;
}}
QProgressBar::chunk {{
    background-color: {c['accent']};
    border-radius: 2px;
}}
"""


def _get_model_css(model_name: str) -> str:
    try:
        if not _ankiweb_available():
            return ""
        col = _aqt.mw.col
        model = col.models.by_name(model_name)
        if model is None:
            return ""
        return model.get("css", "")
    except Exception:
        return ""


def _get_note_title(note: NoteDiff) -> str:
    for fname in note.field_names:
        val = (note.original_fields.get(fname) or note.updated_fields.get(fname) or "").strip()
        val = re.sub(r"<[^>]+>", "", val).strip()
        if val:
            return val[:60] + ("…" if len(val) > 60 else "")
    return f"Note {note.note_id}"


# ---------------------------------------------------------------------------
# HunkWidget: carte de diff épurée pour un seul DiffHunk
# ---------------------------------------------------------------------------

class HunkWidget(QtWidgets.QFrame, PaletteMixin):
    """Affiche un DiffHunk sous forme de carte moderne avec micro-actions."""

    stateChanged = _Signal()

    def __init__(self, hunk: DiffHunk, parent=None, protected: bool = False):
        super().__init__(parent)
        self.setObjectName("hunkWidget")
        self.hunk = hunk
        self._protected = protected
        self._focused = False
        self._build()

    def _build(self):
        self.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
        root = QtWidgets.QVBoxLayout(self)
        root.setContentsMargins(0, 2, 0, 4)
        root.setSpacing(0)

        # ── Contexte avant ──────────────────────────────────────────────
        if self.hunk.context_before.strip():
            ctx = QtWidgets.QLabel(self)
            ctx.setTextFormat(_Qt.TextFormat.RichText)
            c = self._palette_colors()
            ctx.setText(
                f'<span style="color:{c["text_muted"]};font-family:monospace;font-size:11px;">'
                f'{html.escape(self.hunk.context_before)}</span>'
            )
            ctx.setWordWrap(True)
            ctx.setContentsMargins(14, 2, 8, 2)
            root.addWidget(ctx)

        # ── Conteneur du diff (carte) ───────────────────────────────────
        self._diff_frame = QtWidgets.QFrame(self)
        self._diff_frame.setObjectName("diffFrame")
        diff_layout = QtWidgets.QHBoxLayout(self._diff_frame)
        diff_layout.setContentsMargins(0, 0, 8, 0)
        diff_layout.setSpacing(8)

        # Gouttière latérale (barre d'état)
        self._gutter = QtWidgets.QFrame(self._diff_frame)
        self._gutter.setFixedWidth(4)
        diff_layout.addWidget(self._gutter)

        # Contenu central (lignes supprimées et ajoutées)
        content = QtWidgets.QVBoxLayout()
        content.setContentsMargins(8, 6, 8, 6)
        content.setSpacing(4)

        old_text = "".join(self.hunk.old_parts)
        new_text = "".join(self.hunk.new_parts)

        c = self._palette_colors()

        if old_text:
            self._rem_label = QtWidgets.QLabel(self._diff_frame)
            self._rem_label.setTextFormat(_Qt.TextFormat.RichText)
            self._rem_label.setWordWrap(True)
            self._rem_label.setText(
                f'<span style="color:{c["rem_fg"]};font-weight:700;font-size:12px;">− </span>'
                f'<span style="color:{c["rem_fg"]};font-size:12px;text-decoration:line-through;">{html.escape(old_text)}</span>'
            )
            content.addWidget(self._rem_label)
        else:
            self._rem_label = None

        if new_text:
            self._add_label = QtWidgets.QLabel(self._diff_frame)
            self._add_label.setTextFormat(_Qt.TextFormat.RichText)
            self._add_label.setWordWrap(True)
            self._add_label.setText(
                f'<span style="color:{c["add_fg"]};font-weight:700;font-size:12px;">+ </span>'
                f'<span style="color:{c["add_fg"]};font-size:12px;">{html.escape(new_text)}</span>'
            )
            content.addWidget(self._add_label)
        else:
            self._add_label = None

        diff_layout.addLayout(content, stretch=1)

        # Actions de hunk : micro-boutons [+] [−]
        btn_layout = QtWidgets.QHBoxLayout()
        btn_layout.setSpacing(4)
        btn_layout.setContentsMargins(0, 0, 0, 0)

        if self._protected:
            lock_badge = QtWidgets.QLabel("Protégé", self._diff_frame)
            lock_badge.setToolTip("Ce champ est protégé — hunk auto-rejeté")
            lock_badge.setStyleSheet(
                f"color:{c['warn']}; font-size:11px; font-weight:600; background:{c['warning_bg']}; "
                f"border:1px solid {c['warning_border']}; border-radius:8px; padding:1px 6px;"
            )
            btn_layout.addWidget(lock_badge)

        self._btn_acc = QtWidgets.QToolButton(self._diff_frame)
        self._btn_acc.setText("+")
        self._btn_acc.setToolTip("Accepter ce changement")
        self._btn_acc.setFixedSize(24, 24)
        self._btn_acc.setCursor(QtGui.QCursor(_Qt.CursorShape.PointingHandCursor))
        self._btn_acc.clicked.connect(self._toggle_accepted)
        btn_layout.addWidget(self._btn_acc)

        self._btn_rej = QtWidgets.QToolButton(self._diff_frame)
        self._btn_rej.setText("−")
        self._btn_rej.setToolTip("Rejeter ce changement")
        self._btn_rej.setFixedSize(24, 24)
        self._btn_rej.setCursor(QtGui.QCursor(_Qt.CursorShape.PointingHandCursor))
        self._btn_rej.clicked.connect(self._toggle_rejected)
        btn_layout.addWidget(self._btn_rej)

        diff_layout.addLayout(btn_layout)
        root.addWidget(self._diff_frame)

        # ── Contexte après ──────────────────────────────────────────────
        if self.hunk.context_after.strip():
            ctx2 = QtWidgets.QLabel(self)
            ctx2.setTextFormat(_Qt.TextFormat.RichText)
            ctx2.setText(
                f'<span style="color:{c["text_muted"]};font-family:monospace;font-size:11px;">'
                f'{html.escape(self.hunk.context_after)}</span>'
            )
            ctx2.setWordWrap(True)
            ctx2.setContentsMargins(14, 2, 8, 2)
            root.addWidget(ctx2)

        self._refresh_state()

    def _toggle_accepted(self):
        self.hunk.accepted = True
        self._refresh_state()
        self.stateChanged.emit()

    def _toggle_rejected(self):
        self.hunk.accepted = False
        self._refresh_state()
        self.stateChanged.emit()

    def toggle_decision(self):
        """Bascule accepted/rejected — utilisé par la navigation clavier."""
        self.hunk.accepted = not self.hunk.accepted
        self._refresh_state()
        self.stateChanged.emit()

    def set_focused(self, focused: bool):
        """Marque ce hunk comme focalisé (navigation clavier / mode Review)."""
        self._focused = focused
        self._refresh_state()

    def refresh_palette(self):
        self._refresh_state()

    def _refresh_state(self):
        c = self._palette_colors()
        if self.hunk.accepted:
            color = c['add_border']
            bg = c['add_bg']
            gutter_color = c['ok']
        else:
            color = c['rem_border']
            bg = c['rem_bg']
            gutter_color = c['err']

        if self._protected and not self.hunk.accepted:
            color = c['warning_border']
            bg = c['warning_bg']
            gutter_color = c['warn']

        # Bordure de focus (navigation clavier / mode Review)
        if self._focused:
            border_style = f"border: 2px solid {c['accent']};"
        else:
            border_style = f"border: 1px solid {color};"

        self._gutter.setStyleSheet(
            f"background-color: {gutter_color}; border-top-left-radius: 6px; border-bottom-left-radius: 6px;"
        )
        self._diff_frame.setStyleSheet(
            f"QFrame#diffFrame {{ background-color: {bg}; border-radius: 6px; {border_style} }}"
        )
        # Micro boutons styling
        if self.hunk.accepted:
            self._btn_acc.setStyleSheet(
                f"background:{c['ok']}; color:#ffffff; border:none; border-radius:12px; font-weight:700;"
            )
            self._btn_rej.setStyleSheet(
                f"background:transparent; color:{c['text_muted']}; border:1px solid {c['border']}; border-radius:12px;"
            )
        else:
            self._btn_acc.setStyleSheet(
                f"background:transparent; color:{c['text_muted']}; border:1px solid {c['border']}; border-radius:12px;"
            )
            self._btn_rej.setStyleSheet(
                f"background:{c['err']}; color:#ffffff; border:none; border-radius:12px; font-weight:700;"
            )


# ---------------------------------------------------------------------------
# FieldDiffPanel: panneau de diff par champ
# ---------------------------------------------------------------------------

def _unfold_and_style_html(raw_html: str, model_css: str, c: dict, placeholder: str = "") -> str:
    """
    Formate le contenu HTML pour un affichage pleine largeur en format aperçu style carte,
    en dépliant systématiquement toutes les sections habituellement masquées ou repliées.
    """
    clean = re.sub(r"<[^>]+>", "", raw_html or "").replace("&nbsp;", " ").strip()
    if not clean:
        muted_color = c["text_muted"]
        content = f'<i style="color:{muted_color}; font-size:11px;">{placeholder}</i>'
    else:
        # Remplacer les styles inline de masquage
        content = re.sub(
            r'(style\s*=\s*["\'][^"\']*?)display\s*:\s*none\s*;?',
            r'\1display:block;',
            raw_html.strip(),
            flags=re.IGNORECASE
        )
        content = re.sub(
            r'(style\s*=\s*["\'][^"\']*?)visibility\s*:\s*hidden\s*;?',
            r'\1visibility:visible;',
            content,
            flags=re.IGNORECASE
        )

    unfolding_css = """
        .barHider {
            display: none !important;
        }
        .section, .items, [id*="MegaContainer"], [id*="Container"],
        [id*="infoSupplementaires"], [id*="mnemonics"], [id*="sources"],
        [id*="comments"], [id*="cartesLiees"], [id*="description"],
        [id*="illustration"], [id*="nom"], [id*="tags"] {
            display: block !important;
            visibility: visible !important;
            opacity: 1 !important;
            height: auto !important;
            max-height: none !important;
        }
        div[style*="display:none"], div[style*="display: none"],
        span[style*="display:none"], span[style*="display: none"],
        [style*="display:none" i], [style*="display: none" i],
        [hidden] {
            display: block !important;
            visibility: visible !important;
        }
        details, details > * {
            display: block !important;
        }
    """

    full_html = (
        f"<html><head><style>"
        f"body {{ font-family:-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; "
        f"font-size:12px; line-height:1.45; color:{c['text_primary']}; margin:0; padding:0; word-wrap:break-word; overflow-wrap:break-word; }}"
        f"{unfolding_css}"
        f"{model_css}"
        f"</style></head><body>"
        f"{content}"
        f"</body></html>"
    )
    return full_html


class TagsDiffPanel(QtWidgets.QFrame, PaletteMixin):
    """
    Panneau affichant la comparaison détaillée des tags (anciens vs nouveaux).
    Affiche des badges/chips clairs pour les tags ajoutés (+), supprimés (−) et conservés,
    avec boutons pour garder les anciens ou accepter les tags d'import.
    """
    tagsChanged = _Signal()

    def __init__(self, diff: Any, parent=None):
        super().__init__(parent)
        self.diff = diff
        self._build()

    def _build(self):
        c = self._palette_colors()
        self.setStyleSheet(
            f"background-color:{c['surface']}; border:1px solid {c['border']}; border-radius:6px; margin-bottom:6px;"
        )
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(8, 6, 8, 6)
        layout.setSpacing(6)

        # Header Row
        hdr_row = QtWidgets.QHBoxLayout()
        hdr_row.setContentsMargins(0, 0, 0, 0)
        hdr_row.setSpacing(6)

        lbl = QtWidgets.QLabel("TAGS", self)
        lbl.setStyleSheet(f"color:{c['text_secondary']}; font-size:11px; font-weight:700; background:transparent; border:none;")
        hdr_row.addWidget(lbl)

        self._badge = QtWidgets.QLabel(self)
        self._badge.setAlignment(_Qt.AlignmentFlag.AlignCenter)
        hdr_row.addWidget(self._badge)

        hdr_row.addStretch()

        self._btn_copy = QtWidgets.QPushButton("Copier", self)
        self._btn_copy.setCursor(QtGui.QCursor(_Qt.CursorShape.PointingHandCursor))
        self._btn_copy.setStyleSheet(
            f"QPushButton {{ background-color:transparent; color:{c['text_muted']}; "
            f"border:1px solid {c['border']}; border-radius:3px; padding:2px 8px; font-size:11px; }}"
            f"QPushButton:hover {{ color:{c['text_primary']}; background-color:{c['surface_hover']}; border-color:{c['border_strong']}; }}"
        )
        self._btn_copy.clicked.connect(self._copy_tags)
        hdr_row.addWidget(self._btn_copy)

        layout.addLayout(hdr_row)

        orig_set = set(self.diff.original_tags or [])
        upd_set = set(self.diff.updated_tags or [])
        has_tag_diff = (orig_set != upd_set) and (getattr(self.diff, "action", "UPDATE") == "UPDATE")

        if has_tag_diff:
            btn_row = QtWidgets.QHBoxLayout()
            btn_row.setContentsMargins(0, 0, 0, 0)
            btn_row.setSpacing(6)

            self._btn_keep_old = QtWidgets.QPushButton("Garder anciens tags", self)
            self._btn_keep_old.setStyleSheet(_get_style_btn_reject())
            self._btn_keep_old.setCursor(QtGui.QCursor(_Qt.CursorShape.PointingHandCursor))
            self._btn_keep_old.setToolTip("Conserver les tags de la version pré-maj")
            self._btn_keep_old.clicked.connect(self._keep_old_tags)
            btn_row.addWidget(self._btn_keep_old, stretch=1)

            self._btn_accept_new = QtWidgets.QPushButton("Accepter tags d'import", self)
            self._btn_accept_new.setStyleSheet(_get_style_btn_accept())
            self._btn_accept_new.setCursor(QtGui.QCursor(_Qt.CursorShape.PointingHandCursor))
            self._btn_accept_new.setToolTip("Accepter les tags de l'importation")
            self._btn_accept_new.clicked.connect(self._accept_new_tags)
            btn_row.addWidget(self._btn_accept_new, stretch=1)

            layout.addLayout(btn_row)

        # Tags content label
        self._tags_display = QtWidgets.QLabel(self)
        self._tags_display.setWordWrap(True)
        self._tags_display.setTextFormat(_Qt.TextFormat.RichText)
        self._tags_display.setTextInteractionFlags(
            _Qt.TextInteractionFlag.TextSelectableByMouse | _Qt.TextInteractionFlag.TextSelectableByKeyboard
        )
        layout.addWidget(self._tags_display)

        self.refresh()

    def refresh(self):
        c = self._palette_colors()
        orig = self.diff.original_tags or []
        upd = self.diff.updated_tags or []
        orig_set = set(orig)
        upd_set = set(upd)
        has_diff = (orig_set != upd_set) and (getattr(self.diff, "action", "UPDATE") == "UPDATE")

        added = [t for t in upd if t not in orig_set]
        removed = [t for t in orig if t not in upd_set]
        common = [t for t in orig if t in upd_set]

        target = getattr(self.diff, "tags_active_target", "PROP")

        if not has_diff:
            self._badge.setText("Inchangé")
            self._badge.setStyleSheet(
                f"color:{c['text_muted']}; font-size:10px; font-weight:600; background:{c['surface_high']}; "
                f"border:1px solid {c['border']}; border-radius:8px; padding:1px 6px;"
            )
            if not self.diff.tags:
                self._tags_display.setText(f'<i style="color:{c["text_muted"]};font-size:11px;">[Aucun tag]</i>')
            else:
                chips = " ".join([
                    f'<span style="background:{c["chip_bg"]}; color:{c["text_secondary"]}; '
                    f'border:1px solid {c["chip_border"]}; border-radius:4px; padding:2px 6px; font-size:11px;">'
                    f'{html.escape(t)}</span>'
                    for t in self.diff.tags
                ])
                self._tags_display.setText(chips)
            return

        if target == "OLD":
            self._badge.setText("Anciens tags gardés")
            self._badge.setStyleSheet(
                f"color:{c['rem_fg']}; font-size:10px; font-weight:600; background:{c['rem_bg']}; "
                f"border:1px solid {c['rem_border']}; border-radius:8px; padding:1px 6px;"
            )
        else:
            diff_summary = []
            if added:
                diff_summary.append(f"+{len(added)}")
            if removed:
                diff_summary.append(f"−{len(removed)}")
            summary_str = f" ({', '.join(diff_summary)})" if diff_summary else ""
            self._badge.setText(f"Tags d'import{summary_str}")
            self._badge.setStyleSheet(
                f"color:{c['add_fg']}; font-size:10px; font-weight:600; background:{c['add_bg']}; "
                f"border:1px solid {c['add_border']}; border-radius:8px; padding:1px 6px;"
            )

        chip_parts = []
        for t in common:
            chip_parts.append(
                f'<span style="background:{c["chip_bg"]}; color:{c["text_secondary"]}; '
                f'border:1px solid {c["chip_border"]}; border-radius:4px; padding:2px 6px; font-size:11px;">'
                f'{html.escape(t)}</span>'
            )
        for t in added:
            chip_parts.append(
                f'<span style="background:{c["add_bg"]}; color:{c["add_fg"]}; '
                f'border:1px solid {c["add_border"]}; border-radius:4px; padding:2px 6px; font-size:11px; font-weight:600;">'
                f'+{html.escape(t)}</span>'
            )
        for t in removed:
            chip_parts.append(
                f'<span style="background:{c["rem_bg"]}; color:{c["rem_fg"]}; '
                f'border:1px solid {c["rem_border"]}; border-radius:4px; padding:2px 6px; font-size:11px; text-decoration:line-through;">'
                f'−{html.escape(t)}</span>'
            )

        self._tags_display.setText(" ".join(chip_parts))

    def _keep_old_tags(self):
        self.diff.tags = list(self.diff.original_tags)
        self.diff.tags_active_target = "OLD"
        self.refresh()
        self.tagsChanged.emit()

    def _accept_new_tags(self):
        self.diff.tags = list(self.diff.updated_tags)
        self.diff.tags_active_target = "PROP"
        self.refresh()
        self.tagsChanged.emit()

    def _copy_tags(self):
        app = QtWidgets.QApplication.instance()
        if app and app.clipboard():
            app.clipboard().setText(" ".join(self.diff.tags))
            self._btn_copy.setText("Copié !")
            safe_timer_set_text(self._btn_copy, "Copier", 1500)


class FieldDiffPanel(QtWidgets.QFrame, PaletteMixin):
    """
    Affiche un champ avec machine à états 4 versions (old_orig, prop_orig, old_mod, prop_mod)
    et rendu format aperçu pleine page déplié (style carte).
    """

    previewChanged = _Signal()

    def __init__(self, field_name: str, old_val: str, new_val: str,
                 model_css: str = "", protected: bool = False, diff: Optional[Any] = None, parent=None):
        super().__init__(parent)
        self.field_name = field_name
        self.diff = diff
        self._model_css = model_css
        self._protected = protected

        if diff is not None:
            self.old_orig = diff.original_fields.get(field_name, old_val)
            self.prop_orig = diff.updated_fields.get(field_name, new_val)
            self.old_mod = diff.old_mod_fields.setdefault(field_name, self.old_orig)
            self.prop_mod = diff.prop_mod_fields.setdefault(field_name, self.prop_orig)
            self.active_target = diff.field_active_target.setdefault(field_name, "PROP")
        else:
            self.old_orig = old_val
            self.prop_orig = new_val
            self.old_mod = old_val
            self.prop_mod = new_val
            self.active_target = "PROP"

        self.old_val = self.old_orig
        self.new_val = self.prop_orig
        self.hunks: List[DiffHunk] = compute_hunks(self.old_orig, self.prop_orig)
        self._hunk_widgets: List[HunkWidget] = []
        self._preview_visible: bool = False
        self._compare_visible: bool = False

        if self._protected:
            for h in self.hunks:
                h.accepted = False
        else:
            init_acc = (self.active_target == "PROP")
            for h in self.hunks:
                h.accepted = init_acc

        for hunk in self.hunks:
            hw = HunkWidget(hunk, None, protected=self._protected)
            hw.stateChanged.connect(self._on_hunk_state_changed)
            self._hunk_widgets.append(hw)

        self._build()

    def _build(self):
        self.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
        root = QtWidgets.QVBoxLayout(self)
        root.setContentsMargins(0, 4, 0, 10)
        root.setSpacing(4)

        c = self._palette_colors()

        # ── En-tête du champ ─────────────────────────────────────────────
        hdr = QtWidgets.QFrame(self)
        hdr.setStyleSheet(
            f"background-color:{c['surface']}; border-radius:6px; "
            f"border: 1px solid {c['border']};"
        )
        hdr_layout = QtWidgets.QVBoxLayout(hdr)
        hdr_layout.setContentsMargins(8, 6, 8, 6)
        hdr_layout.setSpacing(4)

        # Ligne 1 : Nom du champ + Badge + Bouton Copier
        row1 = QtWidgets.QHBoxLayout()
        row1.setContentsMargins(0, 0, 0, 0)
        row1.setSpacing(6)

        field_lbl = QtWidgets.QLabel(self.field_name, hdr)
        field_lbl.setStyleSheet(
            f"color:{c['text_primary']}; font-weight:700; font-size:12px; border:none; background:transparent;"
        )
        row1.addWidget(field_lbl, stretch=1)

        self._badge = QtWidgets.QLabel(hdr)
        self._badge.setAlignment(_Qt.AlignmentFlag.AlignCenter)
        row1.addWidget(self._badge)

        self._btn_copy = QtWidgets.QPushButton("Copier", hdr)
        self._btn_copy.setCursor(QtGui.QCursor(_Qt.CursorShape.PointingHandCursor))
        self._btn_copy.setToolTip(f"Copier le contenu actuellement affiché pour '{self.field_name}'")
        self._btn_copy.setStyleSheet(
            f"QPushButton {{ background-color:transparent; color:{c['text_muted']}; "
            f"border:1px solid {c['border']}; border-radius:3px; padding:2px 8px; font-size:11px; }}"
            f"QPushButton:hover {{ color:{c['text_primary']}; background-color:{c['surface_hover']}; border-color:{c['border_strong']}; }}"
        )
        def _copy_field():
            app = QtWidgets.QApplication.instance()
            if app and app.clipboard():
                text_to_copy = self._old_val_browser.toPlainText()
                app.clipboard().setText(text_to_copy)
                self._btn_copy.setText("Copié !")
                safe_timer_set_text(self._btn_copy, "Copier", 1500)
        self._btn_copy.clicked.connect(_copy_field)
        row1.addWidget(self._btn_copy)

        hdr_layout.addLayout(row1)

        # Ligne 2 : Boutons d'action souples
        row2 = QtWidgets.QHBoxLayout()
        row2.setContentsMargins(0, 0, 0, 0)
        row2.setSpacing(6)

        self._btn_reject_all = QtWidgets.QPushButton("Garder l'ancien", hdr)
        self._btn_reject_all.setObjectName("diff_reject_all_field_btn")
        self._btn_reject_all.setStyleSheet(_get_style_btn_reject())
        self._btn_reject_all.setMinimumHeight(22)
        self._btn_reject_all.setCursor(QtGui.QCursor(_Qt.CursorShape.PointingHandCursor))
        self._btn_reject_all.setToolTip("Garder la version pré-maj (Rejeter les modifications d'import)")
        self._btn_reject_all.clicked.connect(self._reject_all)
        row2.addWidget(self._btn_reject_all, stretch=1)

        self._btn_accept_all = QtWidgets.QPushButton("Accepter", hdr)
        self._btn_accept_all.setObjectName("diff_accept_all_field_btn")
        self._btn_accept_all.setStyleSheet(_get_style_btn_accept())
        self._btn_accept_all.setMinimumHeight(22)
        self._btn_accept_all.setCursor(QtGui.QCursor(_Qt.CursorShape.PointingHandCursor))
        self._btn_accept_all.setToolTip("Accepter la proposition d'import pour ce champ")
        self._btn_accept_all.clicked.connect(self._accept_all)
        row2.addWidget(self._btn_accept_all, stretch=1)

        self._btn_reset_editor = QtWidgets.QPushButton("Rétablir originel", hdr)
        self._btn_reset_editor.setObjectName("diff_reset_editor_btn")
        self._btn_reset_editor.setStyleSheet(
            f"QPushButton {{ background-color:{c['chip_bg']}; color:{c['text_secondary']}; "
            f"border:1px solid {c['chip_border']}; border-radius:4px; padding:2px 8px; font-size:11px; font-weight:600; }}"
            f"QPushButton:hover {{ color:{c['text_primary']}; background-color:{c['surface_hover']}; }}"
        )
        self._btn_reset_editor.setMinimumHeight(22)
        self._btn_reset_editor.setCursor(QtGui.QCursor(_Qt.CursorShape.PointingHandCursor))
        self._btn_reset_editor.clicked.connect(self._on_reset_editor_clicked)
        self._btn_reset_editor.hide()
        row2.addWidget(self._btn_reset_editor)

        hdr_layout.addLayout(row2)
        root.addWidget(hdr)

        # ── Zone Version affichée dans le panneau ──────────────────────────
        orig_container = QtWidgets.QFrame(self)
        orig_container.setStyleSheet(
            f"background-color:{c['surface']}; border:1px solid {c['border']}; border-radius:6px;"
        )
        orig_layout = QtWidgets.QVBoxLayout(orig_container)
        orig_layout.setContentsMargins(8, 6, 8, 6)
        orig_layout.setSpacing(3)

        label_row = QtWidgets.QHBoxLayout()
        label_row.setContentsMargins(0, 0, 0, 0)
        label_row.setSpacing(6)

        self._orig_lbl = QtWidgets.QLabel("Version pré-maj", orig_container)
        self._orig_lbl.setStyleSheet(
            f"color:{c['text_secondary']}; font-size:10px; font-weight:700; "
            f"text-transform:uppercase; letter-spacing:0.5px; background:transparent; border:none;"
        )
        label_row.addWidget(self._orig_lbl, stretch=1)

        self._btn_reset_diff = QtWidgets.QPushButton("Rétablir originel", orig_container)
        self._btn_reset_diff.setStyleSheet(
            f"QPushButton {{ background-color:transparent; color:{c['text_muted']}; "
            f"border:1px solid {c['border']}; border-radius:3px; padding:1px 6px; font-size:10px; }}"
            f"QPushButton:hover {{ color:{c['text_primary']}; background-color:{c['surface_hover']}; }}"
        )
        self._btn_reset_diff.setCursor(QtGui.QCursor(_Qt.CursorShape.PointingHandCursor))
        self._btn_reset_diff.clicked.connect(self._on_reset_diff_clicked)
        self._btn_reset_diff.hide()
        label_row.addWidget(self._btn_reset_diff)

        orig_layout.addLayout(label_row)

        self._old_val_browser = _AutoResizingTextBrowser(orig_container)
        self._old_val_browser.setStyleSheet(
            f"background-color:{c['base']}; color:{c['text_primary']}; "
            f"border:1px solid {c['border']}; border-radius:4px; padding:0px; font-size:12px; "
            f"selection-background-color:{c['accent']}; selection-color:{c['accent_text']};"
        )
        orig_layout.addWidget(self._old_val_browser)
        root.addWidget(orig_container)

        self._refresh_display()

    def _set_browser_html_preserve_scroll(self, html_content: str):
        if getattr(self, "_last_rendered_html", None) == html_content:
            return
        self._last_rendered_html = html_content
        vsb = self._old_val_browser.verticalScrollBar()
        scroll_pos = vsb.value() if vsb else 0
        self._old_val_browser.setHtml(html_content)
        if vsb and scroll_pos > 0:
            vsb.setValue(scroll_pos)

    def _refresh_display(self):
        c = self._palette_colors()
        is_unchanged = (self.old_orig == self.prop_orig)

        if self._protected:
            self._badge.setText("Protégé")
            self._badge.setStyleSheet(
                f"color:{c['warn']}; font-size:11px; font-weight:600; background:{c['warning_bg']}; "
                f"border:1px solid {c['warning_border']}; border-radius:10px; padding:2px 8px;"
            )
            self._btn_reject_all.hide()
            self._btn_accept_all.hide()
            self._btn_reset_editor.hide()
            self._orig_lbl.setText("Version protégée")
            self._btn_reset_diff.hide()
            html_content = _unfold_and_style_html(self.old_orig, self._model_css, c, "[Champ protégé]")
            self._set_browser_html_preserve_scroll(html_content)
            return

        if is_unchanged:
            self._badge.setText("Inchangé")
            self._badge.setStyleSheet(
                f"color:{c['text_muted']}; font-size:11px; background:{c['surface_high']}; "
                f"border:1px solid {c['border']}; border-radius:10px; padding:2px 8px;"
            )
            self._btn_reject_all.hide()
            self._btn_accept_all.hide()
            self._btn_reset_editor.hide()
            self._orig_lbl.setText("Version inchangée")
            self._btn_reset_diff.hide()
            html_content = _unfold_and_style_html(self.old_orig, self._model_css, c, "[Champ vide]")
            self._set_browser_html_preserve_scroll(html_content)
            return

        self._btn_reject_all.show()
        self._btn_accept_all.show()

        if self.active_target == "PROP":
            # L'éditeur contient la proposition (prop_mod)
            # Le diff affiche l'ancienne version (old_mod)
            disp_val = self.old_mod
            is_old_modified = (self.old_mod != self.old_orig)
            is_prop_modified = (self.prop_mod != self.prop_orig)

            if is_prop_modified:
                self._badge.setText("Proposition modifiée")
                self._badge.setStyleSheet(
                    f"color:{c['add_fg']}; font-size:11px; font-weight:600; background:{c['add_bg']}; "
                    f"border:1px solid {c['add_border']}; border-radius:10px; padding:2px 8px;"
                )
                self._btn_reset_editor.setText("Rétablir proposition d'import")
                self._btn_reset_editor.show()
            else:
                self._badge.setText("Accepté")
                self._badge.setStyleSheet(
                    f"color:{c['ok']}; font-size:11px; font-weight:600; background:{c['surface_high']}; "
                    f"border:1px solid {c['border']}; border-radius:10px; padding:2px 8px;"
                )
                self._btn_reset_editor.hide()

            self._btn_reject_all.setText("Garder l'ancien")
            self._btn_reject_all.setEnabled(True)
            self._btn_accept_all.setText("Accepter")
            self._btn_accept_all.setEnabled(False)

            if is_old_modified:
                self._orig_lbl.setText("Ancien modifié (sauvegarde)")
                self._btn_reset_diff.setText("Rétablir ancien originel")
                self._btn_reset_diff.show()
            else:
                self._orig_lbl.setText("Version pré-maj")
                self._btn_reset_diff.hide()

            placeholder = "[Champ vide dans la version pré-maj]"
            html_content = _unfold_and_style_html(disp_val, self._model_css, c, placeholder)
            self._set_browser_html_preserve_scroll(html_content)

        else:
            # L'éditeur contient l'ancien (old_mod)
            # Le diff affiche la proposition (prop_mod)
            disp_val = self.prop_mod
            is_old_modified = (self.old_mod != self.old_orig)
            is_prop_modified = (self.prop_mod != self.prop_orig)

            if is_old_modified:
                self._badge.setText("Ancien modifié")
                self._badge.setStyleSheet(
                    f"color:{c['rem_fg']}; font-size:11px; font-weight:600; background:{c['rem_bg']}; "
                    f"border:1px solid {c['rem_border']}; border-radius:10px; padding:2px 8px;"
                )
                self._btn_reset_editor.setText("Rétablir ancien originel")
                self._btn_reset_editor.show()
            else:
                self._badge.setText("Ancien gardé")
                self._badge.setStyleSheet(
                    f"color:{c['rem_fg']}; font-size:11px; font-weight:600; background:{c['rem_bg']}; "
                    f"border:1px solid {c['rem_border']}; border-radius:10px; padding:2px 8px;"
                )
                self._btn_reset_editor.hide()

            self._btn_reject_all.setText("Garder l'ancien")
            self._btn_reject_all.setEnabled(False)
            self._btn_accept_all.setText("Accepter")
            self._btn_accept_all.setEnabled(True)

            if is_prop_modified:
                self._orig_lbl.setText("Proposition avec modif")
                self._btn_reset_diff.setText("Rétablir proposition d'import")
                self._btn_reset_diff.show()
            else:
                self._orig_lbl.setText("Proposition d'import")
                self._btn_reset_diff.hide()

            placeholder = "[Champ vide dans la proposition]"
            html_content = _unfold_and_style_html(disp_val, self._model_css, c, placeholder)
            self._set_browser_html_preserve_scroll(html_content)

    def _accept_all(self):
        self.active_target = "PROP"
        if self.diff:
            self.diff.field_active_target[self.field_name] = "PROP"
            self.diff.merged_fields[self.field_name] = self.prop_mod
            if self.prop_mod != self.prop_orig:
                self.diff.custom_edit_fields[self.field_name] = self.prop_mod
            else:
                self.diff.custom_edit_fields.pop(self.field_name, None)
        for h in self.hunks:
            h.accepted = True
        for hw in self._hunk_widgets:
            hw.hunk.accepted = True
            hw._refresh_state()
        self._refresh_display()
        self.previewChanged.emit()

    def _reject_all(self):
        if self.diff and getattr(self.diff, "action", "") == "INSERT":
            return
        self.active_target = "OLD"
        if self.diff:
            self.diff.field_active_target[self.field_name] = "OLD"
            self.diff.merged_fields[self.field_name] = self.old_mod
            if self.old_mod != self.old_orig:
                self.diff.custom_edit_fields[self.field_name] = self.old_mod
            else:
                self.diff.custom_edit_fields.pop(self.field_name, None)
        for h in self.hunks:
            h.accepted = False
        for hw in self._hunk_widgets:
            hw.hunk.accepted = False
            hw._refresh_state()
        self._refresh_display()
        self.previewChanged.emit()

    def _on_reset_editor_clicked(self):
        if self.active_target == "PROP":
            self.reset_prop_to_orig()
        else:
            self.reset_old_to_orig()

    def _on_reset_diff_clicked(self):
        if self.active_target == "PROP":
            self.reset_old_to_orig()
        else:
            self.reset_prop_to_orig()

    def reset_old_to_orig(self):
        self.old_mod = self.old_orig
        if self.diff:
            self.diff.old_mod_fields[self.field_name] = self.old_orig
            if self.active_target == "OLD":
                self.diff.merged_fields[self.field_name] = self.old_orig
                self.diff.custom_edit_fields.pop(self.field_name, None)
        self._refresh_display()
        if self.active_target == "OLD":
            self.previewChanged.emit()

    def reset_prop_to_orig(self):
        self.prop_mod = self.prop_orig
        if self.diff:
            self.diff.prop_mod_fields[self.field_name] = self.prop_orig
            if self.active_target == "PROP":
                self.diff.merged_fields[self.field_name] = self.prop_orig
                self.diff.custom_edit_fields.pop(self.field_name, None)
        self._refresh_display()
        if self.active_target == "PROP":
            self.previewChanged.emit()

    def on_editor_modified(self, new_val: str):
        if self.active_target == "PROP":
            if self.prop_mod == new_val:
                return
            self.prop_mod = new_val
            if self.diff:
                self.diff.prop_mod_fields[self.field_name] = new_val
        else:
            if self.old_mod == new_val:
                return
            self.old_mod = new_val
            if self.diff:
                self.diff.old_mod_fields[self.field_name] = new_val
        self._refresh_display()

    def _on_hunk_state_changed(self):
        if self.diff and getattr(self.diff, "action", "") == "INSERT":
            self.active_target = "PROP"
            self.diff.field_active_target[self.field_name] = "PROP"
            self.diff.merged_fields[self.field_name] = self.get_merged_value()
            self._refresh_display()
            self.previewChanged.emit()
            return
        if all(h.accepted for h in self.hunks):
            self.active_target = "PROP"
        elif not any(h.accepted for h in self.hunks):
            self.active_target = "OLD"
        if self.diff:
            self.diff.field_active_target[self.field_name] = self.active_target
            self.diff.merged_fields[self.field_name] = self.get_merged_value()
        self._refresh_display()
        self.previewChanged.emit()

    def get_merged_value(self) -> str:
        if getattr(self, "_protected", False):
            return self.old_orig
        if self.diff and getattr(self.diff, "action", "") == "INSERT":
            return self.prop_mod
        if self.active_target == "OLD":
            return self.old_mod
        return self.prop_mod

    def _update_badge_and_buttons(self):
        self._refresh_display()

    def _refresh_preview(self):
        self._refresh_display()
        self.previewChanged.emit()

    def _refresh_preview_panel(self):
        self._refresh_display()

    def _refresh_compare_panel(self):
        pass

    def _toggle_preview(self, checked: bool = False):
        pass

    def _toggle_compare(self, checked: bool = False):
        pass


# ---------------------------------------------------------------------------
# OldFieldsPanel: Présentation claire et copiable de l'état antérieur
# ---------------------------------------------------------------------------

class OldFieldsPanel(QtWidgets.QScrollArea, PaletteMixin):
    """
    Affiche tous les champs d'origine d'une note (état antérieur).
    Chaque champ est affiché dans un conteneur style éditeur, avec son rendu
    HTML complet et la possibilité de sélectionner et copier n'importe quelle
    partie du texte au curseur, ou de copier le champ complet en un clic.
    """

    def __init__(self, note: Optional[NoteDiff] = None, parent=None):
        super().__init__(parent)
        self._note = note
        self.setWidgetResizable(True)
        self.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
        c = self._palette_colors()
        self.setStyleSheet(f"background-color:{c['base']}; border:none;")
        self.viewport().setStyleSheet(f"background-color:{c['base']};")

        self._container = QtWidgets.QWidget()
        self._container.setStyleSheet(f"background-color:{c['base']};")
        self._layout = QtWidgets.QVBoxLayout(self._container)
        self._layout.setContentsMargins(16, 12, 16, 16)
        self._layout.setSpacing(10)
        self.setWidget(self._container)
        self._render_fields()

    def set_note(self, note: Optional[NoteDiff]):
        self._note = note
        self._render_fields()

    def _clear_layout(self):
        while self._layout.count():
            item = self._layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()

    def _render_fields(self):
        self._clear_layout()
        c = self._palette_colors()
        if self._note is None:
            lbl = QtWidgets.QLabel(
                f"<center><span style='color:{c['text_muted']};font-size:12px;'>"
                f"Sélectionnez une note pour afficher l'état antérieur</span></center>"
            )
            lbl.setTextFormat(_Qt.TextFormat.RichText)
            self._layout.addStretch()
            self._layout.addWidget(lbl)
            self._layout.addStretch()
            return
        note = self._note
        model_css = _get_model_css(note.model_name)

        # ── Header Card ──────────────────────────────────────────────────
        hdr_frame = QtWidgets.QFrame()
        hdr_frame.setStyleSheet(
            f"background-color:{c['surface']}; border-radius:8px; "
            f"border:1px solid {c['border']}; padding:8px 12px;"
        )
        hdr_l = QtWidgets.QHBoxLayout(hdr_frame)
        hdr_l.setContentsMargins(0, 0, 0, 0)
        hdr_l.setSpacing(8)

        title_lbl = QtWidgets.QLabel(f"<b>État antérieur</b> · {html.escape(note.model_name)}")
        title_lbl.setTextFormat(_Qt.TextFormat.RichText)
        title_lbl.setStyleSheet(f"color:{c['text_primary']}; font-size:13px; border:none; background:transparent;")
        hdr_l.addWidget(title_lbl)
        hdr_l.addStretch()

        copy_all_btn = QtWidgets.QPushButton("Tout copier")
        copy_all_btn.setCursor(QtGui.QCursor(_Qt.CursorShape.PointingHandCursor))
        copy_all_btn.setToolTip("Copier tous les champs antérieurs dans le presse-papier")
        copy_all_btn.setStyleSheet(
            f"QPushButton {{ background-color:{c['chip_bg']}; color:{c['text_secondary']}; "
            f"border:1px solid {c['chip_border']}; border-radius:4px; padding:3px 10px; font-size:11px; font-weight:600; }}"
            f"QPushButton:hover {{ color:{c['text_primary']}; background-color:{c['surface_hover']}; }}"
        )
        def _copy_all():
            parts = []
            for fname in note.field_names:
                val = note.original_fields.get(fname, "")
                parts.append(f"--- {fname} ---\n{val}")
            app = QtWidgets.QApplication.instance()
            if app and app.clipboard():
                app.clipboard().setText("\n\n".join(parts))
                copy_all_btn.setText("Copié !")
                safe_timer_set_text(copy_all_btn, "Tout copier", 1500)

        copy_all_btn.clicked.connect(_copy_all)
        hdr_l.addWidget(copy_all_btn)
        self._layout.addWidget(hdr_frame)

        # ── Liste des champs d'origine ──────────────────────────────────
        for field_name in note.field_names:
            val = note.original_fields.get(field_name, "")
            card = QtWidgets.QFrame()
            card.setStyleSheet(
                f"background-color:{c['surface']}; border-radius:8px; "
                f"border:1px solid {c['border']}; padding:8px 10px;"
            )
            card_l = QtWidgets.QVBoxLayout(card)
            card_l.setContentsMargins(0, 0, 0, 0)
            card_l.setSpacing(6)

            # Field header row
            f_hdr_l = QtWidgets.QHBoxLayout()
            f_hdr_l.setContentsMargins(0, 0, 0, 0)
            f_hdr_l.setSpacing(6)

            f_name_lbl = QtWidgets.QLabel(field_name)
            f_name_lbl.setStyleSheet(
                f"color:{c['text_secondary']}; font-size:11px; font-weight:700; "
                f"text-transform:uppercase; letter-spacing:0.5px; border:none; background:transparent;"
            )
            f_hdr_l.addWidget(f_name_lbl)
            f_hdr_l.addStretch()

            btn_copy_field = QtWidgets.QPushButton("Copier")
            btn_copy_field.setCursor(QtGui.QCursor(_Qt.CursorShape.PointingHandCursor))
            btn_copy_field.setToolTip(f"Copier le champ '{field_name}'")
            btn_copy_field.setStyleSheet(
                f"QPushButton {{ background-color:transparent; color:{c['text_muted']}; "
                f"border:1px solid {c['border']}; border-radius:3px; padding:1px 6px; font-size:10px; }}"
                f"QPushButton:hover {{ color:{c['text_primary']}; background-color:{c['surface_hover']}; border-color:{c['border_strong']}; }}"
            )

            def _make_copy_fn(text_val, btn):
                def _do_copy():
                    app = QtWidgets.QApplication.instance()
                    if app and app.clipboard():
                        app.clipboard().setText(text_val)
                        btn.setText("Copié !")
                        safe_timer_set_text(btn, "Copier", 1500)
                return _do_copy

            btn_copy_field.clicked.connect(_make_copy_fn(val, btn_copy_field))
            f_hdr_l.addWidget(btn_copy_field)
            card_l.addLayout(f_hdr_l)

            # Zone texte sélectionnable au curseur, style éditeur
            tb = _AutoResizingTextBrowser()
            tb.setStyleSheet(
                f"background-color:{c['base']}; color:{c['text_primary']}; "
                f"border:1px solid {c['border']}; border-radius:6px; padding:6px; font-size:13px; "
                f"selection-background-color:{c['accent']}; selection-color:{c['accent_text']};"
            )
            tb.setHtml(_unfold_and_style_html(val, model_css, c, "[champ vide]"))
            card_l.addWidget(tb)
            self._layout.addWidget(card)

        self._layout.addStretch()


# ---------------------------------------------------------------------------
# NoteDiffPanel: volet de diff et d'historique pour une note
# ---------------------------------------------------------------------------

class NoteDiffPanel(QtWidgets.QScrollArea, PaletteMixin):
    def __init__(self, note: NoteDiff, parent=None):
        super().__init__(parent)
        self.note = note
        self._field_panels: Dict[str, FieldDiffPanel] = {}
        self._protected_fields: Dict[str, str] = {}
        self._history_panel = None

        try:
            from .history import ProtectedFieldsDB
            pdb = ProtectedFieldsDB()
            self._protected_fields = pdb.get_all_protected(note.note_id)
        except Exception:
            pass

        self.setWidgetResizable(True)
        self.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
        c = self._palette_colors()
        self.setStyleSheet(f"background-color:{c['base']}; border:none;")

        container = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(container)
        layout.setContentsMargins(16, 14, 16, 16)
        layout.setSpacing(12)

        # ── Note Header Card ────────────────────────────────────────────
        header_card = QtWidgets.QFrame()
        header_card.setStyleSheet(
            f"background-color:{c['surface']}; border-radius:10px; "
            f"border:1px solid {c['border']}; padding:10px 14px;"
        )
        hc_layout = QtWidgets.QHBoxLayout(header_card)
        hc_layout.setContentsMargins(0, 0, 0, 0)
        hc_layout.setSpacing(10)

        # Action Badge
        if note.action == "INSERT":
            action_bg, action_fg, action_text = c['add_bg'], c['add_fg'], "+ AJOUT"
        elif note.action == "DELETE":
            action_bg, action_fg, action_text = c['rem_bg'], c['rem_fg'], "− SUPPR"
        else:
            action_bg, action_fg, action_text = c['accent_soft'], c['accent'], "~ MODIF"

        action_badge = QtWidgets.QLabel(action_text)
        action_badge.setStyleSheet(
            f"background:{action_bg}; color:{action_fg}; font-size:10px; font-weight:700; "
            f"border-radius:6px; padding:3px 6px; border:none;"
        )
        hc_layout.addWidget(action_badge)

        title_lbl = QtWidgets.QLabel(
            f"<b>{html.escape(_get_note_title(note))}</b>"
            f"<span style='color:{c['text_muted']};font-size:11px;margin-left:8px;'>"
            f" · {html.escape(note.model_name)}</span>"
        )
        title_lbl.setTextFormat(_Qt.TextFormat.RichText)
        title_lbl.setStyleSheet("border:none; background:transparent; font-size:14px;")
        hc_layout.addWidget(title_lbl, 1)

        # Contrôle segmenté (Diff vs Historique)
        seg_control = QtWidgets.QWidget()
        seg_control.setStyleSheet(_get_style_filter_bar())
        seg_layout = QtWidgets.QHBoxLayout(seg_control)
        seg_layout.setContentsMargins(0, 0, 0, 0)
        seg_layout.setSpacing(4)

        self._btn_tab_diff = QtWidgets.QToolButton()
        self._btn_tab_diff.setText("Diff Hunk")
        self._btn_tab_diff.setCheckable(True)
        self._btn_tab_diff.setChecked(True)
        self._btn_tab_diff.clicked.connect(lambda: self._set_page(0))

        self._btn_tab_orig = QtWidgets.QToolButton()
        self._btn_tab_orig.setText("Anciens champs")
        self._btn_tab_orig.setCheckable(True)
        self._btn_tab_orig.clicked.connect(lambda: self._set_page(1))

        self._btn_tab_hist = QtWidgets.QToolButton()
        self._btn_tab_hist.setText("Historique & Collab")
        self._btn_tab_hist.setCheckable(True)
        self._btn_tab_hist.clicked.connect(lambda: self._set_page(2))

        self._seg_group = QtWidgets.QButtonGroup(self)
        self._seg_group.addButton(self._btn_tab_diff)
        self._seg_group.addButton(self._btn_tab_orig)
        self._seg_group.addButton(self._btn_tab_hist)

        seg_layout.addWidget(self._btn_tab_diff)
        seg_layout.addWidget(self._btn_tab_orig)
        seg_layout.addWidget(self._btn_tab_hist)
        hc_layout.addWidget(seg_control)

        layout.addWidget(header_card)

        # ── Stacked pages (0: Diff, 1: Anciens champs, 2: Historique) ──
        self._stack = QtWidgets.QStackedWidget()

        # Page 0 : Diff
        diff_page = QtWidgets.QWidget()
        diff_layout = QtWidgets.QVBoxLayout(diff_page)
        diff_layout.setContentsMargins(0, 0, 0, 0)
        diff_layout.setSpacing(0)

        model_css = _get_model_css(note.model_name)
        for field_name in note.field_names:
            old_val = note.original_fields.get(field_name, "")
            new_val = note.updated_fields.get(field_name, "")
            is_protected = field_name in self._protected_fields
            panel = FieldDiffPanel(
                field_name, old_val, new_val,
                model_css=model_css,
                protected=is_protected,
                parent=diff_page,
            )
            self._field_panels[field_name] = panel
            diff_layout.addWidget(panel)

        diff_layout.addStretch()
        self._stack.addWidget(diff_page)

        # Page 1 : Anciens champs
        self._old_fields_panel = OldFieldsPanel(note, self._stack)
        self._stack.addWidget(self._old_fields_panel)

        # Page 2 : Historique / AnkiCollab
        self._history_placeholder = QtWidgets.QWidget()
        self._history_layout = QtWidgets.QVBoxLayout(self._history_placeholder)
        self._history_layout.setContentsMargins(0, 0, 0, 0)
        self._stack.addWidget(self._history_placeholder)

        layout.addWidget(self._stack)
        self.setWidget(container)

    def _set_page(self, idx: int):
        self._stack.setCurrentIndex(idx)
        self._btn_tab_diff.setChecked(idx == 0)
        self._btn_tab_orig.setChecked(idx == 1)
        self._btn_tab_hist.setChecked(idx == 2)
        if idx == 2 and self._history_panel is None:
            try:
                from .ankicollab_panel import AnkiCollabHistoryPanel
                current_fields = {fn: self.note.updated_fields.get(fn, "") for fn in self.note.field_names}
                current_fields["__tags__"] = " ".join(self.note.tags)
                self._history_panel = AnkiCollabHistoryPanel(
                    note_id=self.note.note_id,
                    field_names=self.note.field_names,
                    current_fields=current_fields,
                    parent=self._history_placeholder
                )
                self._history_panel.version_retrieved.connect(self._on_version_retrieved)
                self._history_panel.fields_protected.connect(self._on_fields_protected)
                self._history_panel.protection_tags_applied.connect(self._on_protection_tags_applied)
                self._history_layout.addWidget(self._history_panel)
            except Exception:
                pass

    def _on_version_retrieved(self, fields: Dict[str, str]) -> None:
        for field_name, new_val in fields.items():
            if field_name == "__tags__":
                self.note.tags = [t for t in new_val.strip().split(" ") if t]
                if self._history_panel is not None:
                    self._history_panel._current_fields["__tags__"] = new_val
                    self._history_panel._refresh_fields_preview()
                continue
            fp = self._field_panels.get(field_name)
            if fp is not None:
                from .diff import compute_hunks
                fp.new_val = new_val
                fp.hunks = compute_hunks(fp.old_val, new_val)
                for h in fp.hunks:
                    h.accepted = True
                fp.previewChanged.emit()

    def _on_fields_protected(self, fields: Dict[str, str]) -> None:
        self._protected_fields.update(fields)
        for field_name in fields:
            if field_name == "__tags__":
                continue
            fp = self._field_panels.get(field_name)
            if fp is not None:
                fp._protected = True
                for hw in fp._hunk_widgets:
                    hw.hunk.accepted = False
                    hw._protected = True
                    hw._refresh_state()
                fp.previewChanged.emit()

    def _on_protection_tags_applied(self, tags: List[str]) -> None:
        self.note.tags = [t for t in self.note.tags if not t.lower().startswith("ankicollab_protect::")]
        self.note.tags.extend(tags)

    def get_merged_fields(self) -> Dict[str, str]:
        result = {}
        for name, panel in self._field_panels.items():
            if name in self.note.custom_edit_fields:
                result[name] = self.note.custom_edit_fields[name]
            else:
                result[name] = panel.get_merged_value()
        return result

    def has_any_changes(self) -> bool:
        for field_name in self.note.field_names:
            if self._field_panels[field_name].hunks:
                return True
        return False


# ---------------------------------------------------------------------------
# NoteListItemWidget: élément de liste dans la barre latérale
# ---------------------------------------------------------------------------

class NoteListItemWidget(QtWidgets.QWidget, PaletteMixin):
    def __init__(self, note: NoteDiff, parent=None):
        super().__init__(parent)
        self.note = note
        layout = QtWidgets.QHBoxLayout(self)
        layout.setContentsMargins(8, 6, 8, 6)
        layout.setSpacing(8)

        c = self._palette_colors()

        if note.action == "INSERT":
            badge_text, badge_color, badge_bg = "+", c['add_fg'], c['add_bg']
        elif note.action == "DELETE":
            badge_text, badge_color, badge_bg = "−", c['rem_fg'], c['rem_bg']
        else:
            badge_text, badge_color, badge_bg = "~", c['accent'], c['accent_soft']

        badge = QtWidgets.QLabel(badge_text)
        badge.setFixedSize(20, 20)
        badge.setAlignment(_Qt.AlignmentFlag.AlignCenter)
        badge.setStyleSheet(
            f"color: {badge_color}; background: {badge_bg}; "
            f"border: 1px solid {badge_color}; border-radius: 10px; "
            f"font-size: 11px; font-weight: 700;"
        )
        layout.addWidget(badge)

        text_col = QtWidgets.QVBoxLayout()
        text_col.setSpacing(2)

        title = QtWidgets.QLabel(_get_note_title(note))
        title.setStyleSheet(f"color: {c['text_primary']}; font-size: 12px; font-weight: 500; background: transparent;")
        title.setWordWrap(False)

        model_lbl = QtWidgets.QLabel(note.model_name)
        model_lbl.setStyleSheet(f"color: {c['text_muted']}; font-size: 10px; background: transparent;")

        text_col.addWidget(title)
        text_col.addWidget(model_lbl)
        layout.addLayout(text_col, stretch=1)

        self._cb = QtWidgets.QCheckBox()
        self._cb.setChecked(not note.excluded)
        self._cb.setToolTip("Inclure dans le commit")
        self._cb.setStyleSheet("background: transparent;")
        layout.addWidget(self._cb)

        self._cb.toggled.connect(self._on_toggled)

    def _on_toggled(self, checked: bool):
        self.note.excluded = not checked


# ---------------------------------------------------------------------------
# VisualDiffDialog: dialogue principal
# ---------------------------------------------------------------------------

class VisualDiffDialog(QtWidgets.QDialog, PaletteMixin):
    def __init__(self, note_diffs: List[NoteDiff], parent=None):
        super().__init__(parent)
        self.note_diffs = note_diffs
        self._note_panels: Dict[int, NoteDiffPanel] = {}
        self._current_idx: int = 0
        self._filter_mode: str = "all"

        self._note_hunks: Dict[int, Dict[str, List[DiffHunk]]] = {}
        try:
            from .history import ProtectedFieldsDB
            pdb = ProtectedFieldsDB.instance() if hasattr(ProtectedFieldsDB, "instance") else ProtectedFieldsDB()
        except Exception:
            pdb = None

        for note in self.note_diffs:
            self._note_hunks[note.note_id] = {}
            protected_fields = pdb.get_all_protected(note.note_id) if pdb else {}
            for field_name in note.field_names:
                old_val = note.original_fields.get(field_name, "")
                new_val = note.updated_fields.get(field_name, "")
                hunks = compute_hunks(old_val, new_val)
                if field_name in protected_fields:
                    for h in hunks:
                        h.accepted = False
                self._note_hunks[note.note_id][field_name] = hunks

        self.setWindowTitle("Anki Import Diff")
        self.resize(1060, 700)
        self.setMinimumSize(620, 400)
        self.setStyleSheet(_get_style_base())

        self._setup_ui()
        if self.note_diffs:
            self._list.setCurrentRow(0)

        self._shortcuts: List[QtGui.QShortcut] = []
        self._setup_shortcuts()
        self.installEventFilter(self)

    def reject(self):
        if self.note_diffs:
            try:
                from .browser_integration import prompt_diff_close_decision
                decision = prompt_diff_close_decision(self)
                if decision == "cancel":
                    return
                elif decision == "commit":
                    self._on_commit()
                    return
                elif decision == "revert":
                    super().reject()
                    return
            except Exception:
                pass
        super().reject()

    def closeEvent(self, event: QtGui.QCloseEvent):
        if self.note_diffs:
            try:
                from .browser_integration import prompt_diff_close_decision
                decision = prompt_diff_close_decision(self)
                if decision == "cancel":
                    if event is not None:
                        event.ignore()
                    return
                elif decision == "commit":
                    self._on_commit()
                    if event is not None:
                        event.accept()
                    return
                elif decision == "revert":
                    super().reject()
                    if event is not None:
                        event.accept()
                    return
            except Exception:
                pass
        if event is not None:
            event.accept()

    def eventFilter(self, obj: QtCore.QObject, event: QtCore.QEvent) -> bool:
        if event.type() == QtCore.QEvent.Type.KeyPress:
            key = event.key()
            mods = event.modifiers()
            has_alt = bool(mods & QtCore.Qt.KeyboardModifier.AltModifier)
            has_ctrl = bool(mods & QtCore.Qt.KeyboardModifier.ControlModifier)
            has_shift = bool(mods & QtCore.Qt.KeyboardModifier.ShiftModifier)

            is_escape = (key == QtCore.Qt.Key.Key_Escape and not (has_alt or has_ctrl or has_shift))
            if is_escape:
                # Vérifier si l'utilisateur est dans un champ texte éditable
                app = QtWidgets.QApplication.instance()
                focus_w = app.focusWidget() if app else None
                if focus_w is not None and isinstance(focus_w, (QtWidgets.QLineEdit, QtWidgets.QTextEdit, QtWidgets.QPlainTextEdit)):
                    # Laisser Échap fermer/annuler l'édition du champ
                    return False
                self.reject()
                return True

            is_accept = (
                (key == QtCore.Qt.Key.Key_F2 and not (has_alt or has_ctrl))
                or (has_alt and key in (QtCore.Qt.Key.Key_A, QtCore.Qt.Key.Key_V, QtCore.Qt.Key.Key_Y))
                or (has_ctrl and has_alt and key == QtCore.Qt.Key.Key_A)
                or (has_ctrl and has_shift and key == QtCore.Qt.Key.Key_A)
            )

            is_reject = (
                (key == QtCore.Qt.Key.Key_F3 and not (has_alt or has_ctrl))
                or (has_alt and key in (QtCore.Qt.Key.Key_R, QtCore.Qt.Key.Key_N))
                or (has_ctrl and has_alt and key == QtCore.Qt.Key.Key_R)
                or (has_ctrl and has_shift and key == QtCore.Qt.Key.Key_R)
            )

            is_up = (key == QtCore.Qt.Key.Key_Up and not (has_alt or has_ctrl or has_shift))
            is_down = (key == QtCore.Qt.Key.Key_Down and not (has_alt or has_ctrl or has_shift))

            if is_accept:
                self._accept_all_global()
                return True

            if is_reject:
                self._reject_all_global()
                return True

            if is_up or is_down:
                app = QtWidgets.QApplication.instance()
                focus_w = app.focusWidget() if app else None
                is_text_edit = (
                    focus_w is not None
                    and isinstance(focus_w, (QtWidgets.QLineEdit, QtWidgets.QTextEdit, QtWidgets.QPlainTextEdit))
                    and not getattr(focus_w, "isReadOnly", lambda: False)()
                )
                if not is_text_edit:
                    if is_up:
                        self._prev_note()
                    else:
                        self._next_note()
                    return True

        return super().eventFilter(obj, event)

    def _setup_shortcuts(self):
        shortcuts_map = [
            ("F2", self._accept_all_global),
            ("F3", self._reject_all_global),
            ("A", self._accept_all_global),
            ("Alt+A", self._accept_all_global),
            ("Alt+V", self._accept_all_global),
            ("Ctrl+Alt+A", self._accept_all_global),
            ("Alt+Y", self._accept_all_global),
            ("Ctrl+Shift+A", self._accept_all_global),
            ("R", self._reject_all_global),
            ("Alt+R", self._reject_all_global),
            ("Ctrl+Alt+R", self._reject_all_global),
            ("Alt+N", self._reject_all_global),
            ("Ctrl+Shift+R", self._reject_all_global),
            ("Right", self._next_note),
            ("Left", self._prev_note),
            ("Down", self._next_note),
            ("Up", self._prev_note),
            ("Tab", self._focus_next_hunk),
            ("Shift+Tab", self._focus_prev_hunk),
            ("Space", self._toggle_focused_hunk),
            ("Y", self._accept_focused_hunk),
            ("N", self._reject_focused_hunk),
        ]
        for key, callback in shortcuts_map:
            sc = QtGui.QShortcut(QtGui.QKeySequence(key), self)
            sc.setContext(_Qt.ShortcutContext.WindowShortcut)
            sc.activated.connect(callback)
            self._shortcuts.append(sc)

    def _get_all_hunk_widgets(self) -> List[HunkWidget]:
        """Retourne tous les HunkWidget de la note courante, dans l'ordre."""
        if not self.note_diffs or self._current_idx >= len(self.note_diffs):
            return []
        note = self.note_diffs[self._current_idx]
        panel = self._note_panels.get(note.note_id)
        if not panel:
            return []
        widgets: List[HunkWidget] = []
        for field_name in note.field_names:
            fp = panel._field_panels.get(field_name)
            if fp:
                widgets.extend(fp._hunk_widgets)
        return widgets

    def _focus_next_hunk(self):
        widgets = self._get_all_hunk_widgets()
        if not widgets:
            return
        if not hasattr(self, "_focused_hunk_idx"):
            self._focused_hunk_idx = -1
        # Défocaliser l'ancien
        if 0 <= self._focused_hunk_idx < len(widgets):
            widgets[self._focused_hunk_idx].set_focused(False)
        self._focused_hunk_idx = (self._focused_hunk_idx + 1) % len(widgets)
        widgets[self._focused_hunk_idx].set_focused(True)
        self._scroll_to_hunk(widgets[self._focused_hunk_idx])
        self._update_review_counter()

    def _focus_prev_hunk(self):
        widgets = self._get_all_hunk_widgets()
        if not widgets:
            return
        if not hasattr(self, "_focused_hunk_idx"):
            self._focused_hunk_idx = 0
        if 0 <= self._focused_hunk_idx < len(widgets):
            widgets[self._focused_hunk_idx].set_focused(False)
        self._focused_hunk_idx = (self._focused_hunk_idx - 1) % len(widgets)
        widgets[self._focused_hunk_idx].set_focused(True)
        self._scroll_to_hunk(widgets[self._focused_hunk_idx])
        self._update_review_counter()

    def _toggle_focused_hunk(self):
        widgets = self._get_all_hunk_widgets()
        if not hasattr(self, "_focused_hunk_idx") or not widgets:
            return
        idx = self._focused_hunk_idx
        if 0 <= idx < len(widgets):
            widgets[idx].toggle_decision()
            self._update_status()
            self._update_review_counter()

    def _accept_focused_hunk(self):
        widgets = self._get_all_hunk_widgets()
        if not hasattr(self, "_focused_hunk_idx") or not widgets:
            return
        idx = self._focused_hunk_idx
        if 0 <= idx < len(widgets):
            widgets[idx].hunk.accepted = True
            widgets[idx]._refresh_state()
            widgets[idx].stateChanged.emit()
            self._update_status()
            self._update_review_counter()
            # Avancer automatiquement au hunk suivant
            self._focus_next_hunk()

    def _reject_focused_hunk(self):
        widgets = self._get_all_hunk_widgets()
        if not hasattr(self, "_focused_hunk_idx") or not widgets:
            return
        idx = self._focused_hunk_idx
        if 0 <= idx < len(widgets):
            widgets[idx].hunk.accepted = False
            widgets[idx]._refresh_state()
            widgets[idx].stateChanged.emit()
            self._update_status()
            self._update_review_counter()
            # Avancer automatiquement au hunk suivant
            self._focus_next_hunk()

    def _scroll_to_hunk(self, hw: HunkWidget):
        """Scroll le panneau courant pour rendre le hunk focalisé visible."""
        try:
            note = self.note_diffs[self._current_idx]
            panel = self._note_panels.get(note.note_id)
            if panel and hasattr(panel, "ensureWidgetVisible"):
                panel.ensureWidgetVisible(hw, 20, 20)
        except Exception:
            pass

    def _update_review_counter(self):
        """Met à jour le compteur de progression Mode Review dans le header."""
        if not hasattr(self, "_review_counter_lbl"):
            return
        widgets = self._get_all_hunk_widgets()
        total = len(widgets)
        if total == 0:
            self._review_counter_lbl.setText("")
            return
        idx = getattr(self, "_focused_hunk_idx", -1)
        if idx < 0:
            self._review_counter_lbl.setText(f"{total} hunks")
        else:
            self._review_counter_lbl.setText(f"Hunk {min(idx + 1, total)}/{total}")

    def _next_note(self):
        row = self._list.currentRow()
        if row < self._list.count() - 1:
            self._list.setCurrentRow(row + 1)

    def _prev_note(self):
        row = self._list.currentRow()
        if row > 0:
            self._list.setCurrentRow(row - 1)

    def _setup_ui(self):
        root = QtWidgets.QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        c = self._palette_colors()

        # ── En-tête principal ────────────────────────────────────────────
        header = QtWidgets.QFrame()
        header.setStyleSheet(
            f"background-color:{c['surface']}; border-bottom:1px solid {c['border']};"
        )
        header.setFixedHeight(54)
        hl = QtWidgets.QHBoxLayout(header)
        hl.setContentsMargins(16, 0, 16, 0)
        hl.setSpacing(12)

        self._header_dot = _StatusDot(header, size=12)
        self._header_dot.set_color(c['accent'])
        hl.addWidget(self._header_dot)

        logo = QtWidgets.QLabel("Anki Import Diff", header)
        logo.setStyleSheet(f"color:{c['text_primary']}; font-size:15px; font-weight:700; background:transparent;")
        hl.addWidget(logo)

        n_upd = sum(1 for d in self.note_diffs if d.action == "UPDATE")
        n_ins = sum(1 for d in self.note_diffs if d.action == "INSERT")
        n_del = sum(1 for d in self.note_diffs if d.action == "DELETE")
        parts = []
        if n_ins: parts.append(f"<span style='color:{c['add_fg']};font-weight:600;'>+{n_ins} ajoutées</span>")
        if n_del: parts.append(f"<span style='color:{c['rem_fg']};font-weight:600;'>−{n_del} supprimées</span>")
        if n_upd: parts.append(f"<span style='color:{c['accent']};font-weight:600;'>~{n_upd} modifiées</span>")
        summary_lbl = QtWidgets.QLabel("  ·  ".join(parts), header)
        summary_lbl.setTextFormat(_Qt.TextFormat.RichText)
        summary_lbl.setStyleSheet("background:transparent; font-size:12px; margin-left:8px;")

        hl.addWidget(summary_lbl)
        hl.addStretch()

        self._acc_all_btn = QtWidgets.QPushButton("Tout accepter", header)
        self._acc_all_btn.setObjectName("diff_accept_all_global_btn")
        self._acc_all_btn.setStyleSheet(_get_style_btn_accept())
        self._acc_all_btn.setCursor(QtGui.QCursor(_Qt.CursorShape.PointingHandCursor))
        self._acc_all_btn.clicked.connect(self._accept_all_global)

        self._rej_all_btn = QtWidgets.QPushButton("Tout rejeter", header)
        self._rej_all_btn.setObjectName("diff_reject_all_global_btn")
        self._rej_all_btn.setStyleSheet(_get_style_btn_reject())
        self._rej_all_btn.setCursor(QtGui.QCursor(_Qt.CursorShape.PointingHandCursor))
        self._rej_all_btn.clicked.connect(self._reject_all_global)

        # Séparateur visuel
        sep_line = QtWidgets.QFrame(header)
        sep_line.setFrameShape(QtWidgets.QFrame.Shape.VLine)
        sep_line.setStyleSheet(f"color:{c['border']}; margin:10px 2px;")

        # Compteur hunk (Mode Review)
        self._review_counter_lbl = QtWidgets.QLabel("", header)
        self._review_counter_lbl.setStyleSheet(
            f"color:{c['text_muted']}; font-size:11px; font-weight:500; background:transparent;"
        )
        self._review_counter_lbl.setMinimumWidth(70)

        # Bouton Mode Review
        self._btn_review_mode = QtWidgets.QPushButton("Mode Review", header)
        self._btn_review_mode.setObjectName("diff_review_mode_btn")
        self._btn_review_mode.setCheckable(True)
        self._btn_review_mode.setChecked(False)
        self._btn_review_mode.setStyleSheet(_get_style_btn_toggle())
        self._btn_review_mode.setCursor(QtGui.QCursor(_Qt.CursorShape.PointingHandCursor))
        self._btn_review_mode.setToolTip(
            "Mode Review : Tab/Shift+Tab pour naviguer, Espace/Y/N pour décider"
        )
        self._btn_review_mode.clicked.connect(self._toggle_review_mode)

        hl.addWidget(self._acc_all_btn)
        hl.addWidget(self._rej_all_btn)
        hl.addWidget(sep_line)
        hl.addWidget(self._review_counter_lbl)
        hl.addWidget(self._btn_review_mode)
        root.addWidget(header)

        # ── Barre de progression ─────────────────────────────────────────
        self._progress_bar = QtWidgets.QProgressBar()
        self._progress_bar.setStyleSheet(_get_style_progress())
        self._progress_bar.setFixedHeight(3)
        self._progress_bar.setTextVisible(False)
        self._progress_bar.setMinimum(0)
        self._progress_bar.setMaximum(max(len(self.note_diffs), 1))
        self._progress_bar.setValue(0)
        root.addWidget(self._progress_bar)

        # ── Splitter Corps ───────────────────────────────────────────────
        body = QtWidgets.QSplitter(_Qt.Orientation.Horizontal)
        body.setHandleWidth(1)
        body.setStyleSheet(f"QSplitter::handle {{ background:{c['border']}; }}")

        # Sidebar gauche
        sidebar = QtWidgets.QWidget()
        sidebar.setStyleSheet(f"background-color:{c['surface']};")
        sidebar.setMinimumWidth(240)
        sl = QtWidgets.QVBoxLayout(sidebar)
        sl.setContentsMargins(0, 0, 0, 0)
        sl.setSpacing(0)

        # Barre de filtres (Pill Chips)
        filter_bar = QtWidgets.QWidget()
        filter_bar.setStyleSheet(_get_style_filter_bar())
        filter_bar.setFixedHeight(38)
        fl = QtWidgets.QHBoxLayout(filter_bar)
        fl.setContentsMargins(8, 4, 8, 4)
        fl.setSpacing(4)

        self._filter_btns: Dict[str, QtWidgets.QPushButton] = {}
        for key, label in [("all", "Tout"), ("pending", "En attente"), ("accepted", "Acceptés"), ("rejected", "Rejetés")]:
            btn = QtWidgets.QPushButton(label)
            btn.setCheckable(True)
            btn.setChecked(key == "all")
            btn.clicked.connect(lambda _, k=key: self._set_filter(k))
            self._filter_btns[key] = btn
            fl.addWidget(btn)

        sl.addWidget(filter_bar)

        self._list = QtWidgets.QListWidget()
        self._list.setStyleSheet(_get_style_sidebar())
        self._list.setSpacing(0)
        self._list.currentRowChanged.connect(self._on_row_changed)
        self._populate_list()
        sl.addWidget(self._list)

        # Boutons sélection sidebar
        sb_btns = QtWidgets.QHBoxLayout()
        sb_btns.setContentsMargins(8, 8, 8, 8)
        sb_btns.setSpacing(6)
        self._btn_sel_all = QtWidgets.QPushButton("Sélectionner Tout")
        self._btn_sel_all.setObjectName("diff_sel_all_btn")
        self._btn_sel_all.setStyleSheet(_get_style_btn_secondary())
        self._btn_sel_all.setFixedHeight(28)
        self._btn_sel_all.clicked.connect(self._select_all)

        self._btn_sel_none = QtWidgets.QPushButton("Désélectionner")
        self._btn_sel_none.setObjectName("diff_sel_none_btn")
        self._btn_sel_none.setStyleSheet(_get_style_btn_secondary())
        self._btn_sel_none.setFixedHeight(28)
        self._btn_sel_none.clicked.connect(self._deselect_all)

        sb_btns.addWidget(self._btn_sel_all)
        sb_btns.addWidget(self._btn_sel_none)
        sl.addLayout(sb_btns)

        body.addWidget(sidebar)

        # Panneau central / stack
        self._stack = QtWidgets.QStackedWidget()
        self._stack.setStyleSheet(f"background-color:{c['base']};")

        body.addWidget(self._stack)
        body.setStretchFactor(0, 0)
        body.setStretchFactor(1, 1)
        body.setSizes([270, 890])

        root.addWidget(body, stretch=1)

        # ── Barre inférieure (Footer) ────────────────────────────────────
        footer = QtWidgets.QFrame()
        footer.setStyleSheet(
            f"background-color:{c['surface']}; border-top:1px solid {c['border']};"
        )
        footer.setFixedHeight(54)
        fl2 = QtWidgets.QHBoxLayout(footer)
        fl2.setContentsMargins(16, 0, 16, 0)
        fl2.setSpacing(12)

        self._footer_dot = _StatusDot(footer, size=10)
        self._footer_dot.set_color(c['ok'])
        fl2.addWidget(self._footer_dot)

        self._status_lbl = QtWidgets.QLabel("", footer)
        self._status_lbl.setStyleSheet(f"color:{c['text_secondary']}; font-size:12px; font-weight:500; background:transparent;")
        fl2.addWidget(self._status_lbl)
        fl2.addStretch()

        self._btn_cancel = QtWidgets.QPushButton("Annuler", footer)
        self._btn_cancel.setObjectName("diff_cancel_btn")
        self._btn_cancel.setStyleSheet(_get_style_btn_secondary())
        self._btn_cancel.clicked.connect(self.reject)
        self._btn_cancel.setCursor(QtGui.QCursor(_Qt.CursorShape.PointingHandCursor))

        self._btn_commit = QtWidgets.QPushButton("Valider les Changements", footer)
        self._btn_commit.setObjectName("diff_commit_btn")
        self._btn_commit.setStyleSheet(_get_style_btn_primary())
        self._btn_commit.clicked.connect(self._on_commit)
        self._btn_commit.setCursor(QtGui.QCursor(_Qt.CursorShape.PointingHandCursor))

        fl2.addWidget(self._btn_cancel)
        fl2.addWidget(self._btn_commit)
        root.addWidget(footer)

        self._update_status()

    def _set_filter(self, mode: str):
        self._filter_mode = mode
        for key, btn in self._filter_btns.items():
            btn.setChecked(key == mode)
        self._populate_list()

    def _populate_list(self):
        self._list.blockSignals(True)
        self._list.clear()
        c = self._palette_colors()
        for i, note in enumerate(self.note_diffs):
            if self._filter_mode != "all":
                panel = self._note_panels.get(note.note_id)
                if panel:
                    total_h = sum(len(fp.hunks) for fp in panel._field_panels.values())
                    accepted_h = sum(
                        sum(1 for h in fp.hunks if h.accepted)
                        for fp in panel._field_panels.values()
                    )
                    if self._filter_mode == "accepted" and accepted_h < total_h:
                        continue
                    if self._filter_mode == "rejected" and accepted_h > 0:
                        continue
                    if self._filter_mode == "pending" and (accepted_h == total_h or accepted_h == 0):
                        continue

            item = QtWidgets.QListWidgetItem()
            item.setData(_Role.UserRole, note)

            if note.action == "INSERT":
                icon_char = "+"
                color = c['add_fg']
            elif note.action == "DELETE":
                icon_char = "−"
                color = c['rem_fg']
            else:
                icon_char = "~"
                color = c['accent']

            px = QtGui.QPixmap(14, 14)
            px.fill(QtGui.QColor(0, 0, 0, 0))
            painter = QtGui.QPainter(px)
            painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, True)
            painter.setBrush(QtGui.QBrush(QtGui.QColor(str(color))))
            painter.setPen(QtCore.Qt.PenStyle.NoPen)
            painter.drawEllipse(0, 0, 14, 14)
            painter.end()
            item.setIcon(QtGui.QIcon(px))

            title = _get_note_title(note)
            item.setText(f"  {icon_char}  {title}\n  {note.model_name}")
            item.setSizeHint(QtCore.QSize(220, 52))
            item.setFlags(item.flags() | _ItemFlag.ItemIsUserCheckable)
            item.setCheckState(
                _CheckState.Checked if not note.excluded else _CheckState.Unchecked
            )
            self._list.addItem(item)

        self._list.itemChanged.connect(self._on_item_changed)
        self._list.blockSignals(False)

    def _on_row_changed(self, row: int):
        if row < 0 or row >= self._list.count():
            return
        item = self._list.item(row)
        note = item.data(_Role.UserRole)
        if not note:
            return

        panel = self._note_panels.get(note.note_id)
        if not panel:
            panel = NoteDiffPanel(note, self._stack)
            self._note_panels[note.note_id] = panel
            self._stack.addWidget(panel)

        self._stack.setCurrentWidget(panel)
        self._current_idx = row

        # Réinitialiser le focus hunk à chaque changement de note
        self._focused_hunk_idx = -1
        # Si Mode Review actif, lancer automatiquement sur le premier hunk
        if hasattr(self, "_btn_review_mode") and self._btn_review_mode.isChecked():
            self._focus_next_hunk()
            self._update_review_counter()
        else:
            self._update_review_counter()

        self._update_status()
        self._update_progress()

    def _on_item_changed(self, item: QtWidgets.QListWidgetItem):
        note = item.data(_Role.UserRole)
        if note:
            note.excluded = (item.checkState() == _CheckState.Unchecked)
        self._update_status()

    def _select_all(self):
        self._list.blockSignals(True)
        for i in range(self._list.count()):
            self._list.item(i).setCheckState(_CheckState.Checked)
            self.note_diffs[i].excluded = False
        self._list.blockSignals(False)
        self._update_status()

    def _deselect_all(self):
        self._list.blockSignals(True)
        for i in range(self._list.count()):
            self._list.item(i).setCheckState(_CheckState.Unchecked)
            self.note_diffs[i].excluded = True
        self._list.blockSignals(False)
        self._update_status()

    def _accept_all_global(self):
        for note in self.note_diffs:
            note.custom_edit_fields.clear()
            note.tags = list(note.updated_tags)
            note.tags_active_target = "PROP"
        for fields_hunks in self._note_hunks.values():
            for hunks in fields_hunks.values():
                for h in hunks:
                    h.accepted = True

        for panel in self._note_panels.values():
            for fp in panel._field_panels.values():
                for hw in fp._hunk_widgets:
                    hw._refresh_state()
                if fp._hunk_widgets:
                    fp._refresh_preview()
            if hasattr(panel, "_tags_panel") and panel._tags_panel:
                panel._tags_panel.refresh()
        self._update_status()

    def _reject_all_global(self):
        for note in self.note_diffs:
            note.tags = list(note.original_tags)
            note.tags_active_target = "OLD"
        for fields_hunks in self._note_hunks.values():
            for hunks in fields_hunks.values():
                for h in hunks:
                    h.accepted = False

        for panel in self._note_panels.values():
            for fp in panel._field_panels.values():
                for hw in fp._hunk_widgets:
                    hw._refresh_state()
                if fp._hunk_widgets:
                    fp._refresh_preview()
            if hasattr(panel, "_tags_panel") and panel._tags_panel:
                panel._tags_panel.refresh()
        self._update_status()

    def _toggle_review_mode(self, checked: bool):
        """Active/désactive le Mode Review (navigation hunk par hunk)."""
        if checked:
            # Initialiser sur le premier hunk
            self._focused_hunk_idx = -1
            self._focus_next_hunk()
            self._update_review_counter()
        else:
            # Défocaliser tous les hunks
            widgets = self._get_all_hunk_widgets()
            for hw in widgets:
                hw.set_focused(False)
            self._focused_hunk_idx = -1
            self._review_counter_lbl.setText("")

    def _update_progress(self):
        self._progress_bar.setValue(self._current_idx + 1)


    def _update_status(self):
        included = sum(1 for d in self.note_diffs if not d.excluded)
        total_hunks = 0
        accepted_hunks = 0
        for note_id, fields_hunks in self._note_hunks.items():
            for hunks in fields_hunks.values():
                total_hunks += len(hunks)
                accepted_hunks += sum(1 for h in hunks if h.accepted)
        self._status_lbl.setText(
            f"{included}/{len(self.note_diffs)} notes sélectionnées  ·  "
            f"{accepted_hunks}/{total_hunks} hunks acceptés"
        )
        c = self._palette_colors()
        if accepted_hunks == total_hunks and total_hunks > 0:
            self._footer_dot.set_color(c['ok'])
        elif accepted_hunks > 0:
            self._footer_dot.set_color(c['warn'])
        else:
            self._footer_dot.set_color(c['err'] if total_hunks > 0 else c['text_muted'])

    def _on_commit(self):
        try:
            from .history import NoteHistoryDB
            db = NoteHistoryDB.instance()
            for note in self.note_diffs:
                if not note.excluded:
                    panel = self._note_panels.get(note.note_id)
                    if panel:
                        note.merged_fields = panel.get_merged_fields()
                    else:
                        merged = {}
                        for field_name in note.field_names:
                            old_val = note.original_fields.get(field_name, "")
                            new_val = note.updated_fields.get(field_name, "")
                            hunks = self._note_hunks[note.note_id][field_name]
                            merged[field_name] = apply_hunks(old_val, new_val, hunks)
                        note.merged_fields = merged
                    db.store_snapshot(
                        note.note_id,
                        note.original_fields,
                        source="import",
                        comment=f"Avant import ({note.action})",
                        tags=note.tags,
                    )
        except Exception:
            pass

        for note in self.note_diffs:
            panel = self._note_panels.get(note.note_id)
            if panel:
                note.merged_fields = panel.get_merged_fields()
            else:
                merged = {}
                for field_name in note.field_names:
                    old_val = note.original_fields.get(field_name, "")
                    new_val = note.updated_fields.get(field_name, "")
                    hunks = self._note_hunks[note.note_id][field_name]
                    merged[field_name] = apply_hunks(old_val, new_val, hunks)
                note.merged_fields = merged
        self.accept()

    def cleanup(self) -> None:
        try:
            from aqt.qt import sip
            for panel in list(self._note_panels.values()):
                for fp in list(panel._field_panels.values()):
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
            for btn_attr in ("_acc_all_btn", "_rej_all_btn", "_btn_sel_all", "_btn_sel_none", "_btn_cancel", "_btn_commit"):
                if hasattr(self, btn_attr) and getattr(self, btn_attr) is not None:
                    try:
                        btn = getattr(self, btn_attr)
                        setattr(self, btn_attr, None)
                        if not sip.isdeleted(btn):
                            btn.deleteLater()
                    except Exception:
                        pass
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Compat shims pour les tests
    # ------------------------------------------------------------------

    @property
    def list_widget(self) -> QtWidgets.QListWidget:
        return self._list

    @property
    def field_widgets(self) -> Dict[str, "_FieldWidgetShim"]:
        if not self.note_diffs:
            return {}
        note = self.note_diffs[self._current_idx]
        panel = self._note_panels.get(note.note_id)
        if panel is None:
            return {}

        if not hasattr(self, "_compat_shim_cache"):
            self._compat_shim_cache: Dict[tuple, "_FieldWidgetShim"] = {}

        result: Dict[str, "_FieldWidgetShim"] = {}
        for field_name, fp in panel._field_panels.items():
            key = (note.note_id, field_name)
            if key not in self._compat_shim_cache:
                self._compat_shim_cache[key] = _FieldWidgetShim(fp, note, field_name)
            result[field_name] = self._compat_shim_cache[key]
        return result

    @property
    def diff_browser(self) -> QtWidgets.QWidget:
        if not hasattr(self, "_compat_diff_browser"):
            self._compat_diff_browser = _make_preview_widget(
                parent=self, min_h=120, max_h=400
            )
            self._compat_diff_browser.hide()
            self._refresh_compat_browser()
        return self._compat_diff_browser

    @property
    def raw_html_checkbox(self) -> QtWidgets.QCheckBox:
        if not hasattr(self, "_compat_raw_cb"):
            self._compat_raw_cb = QtWidgets.QCheckBox(self)
            self._compat_raw_cb.hide()
            self._compat_raw_cb.toggled.connect(self._refresh_compat_browser)
        return self._compat_raw_cb

    def _refresh_compat_browser(self):
        if not hasattr(self, "_compat_diff_browser"):
            return
        note = self.note_diffs[self._current_idx] if self.note_diffs else None
        if note is None:
            if hasattr(self._compat_diff_browser, "clear"):
                self._compat_diff_browser.clear()
            return
        raw = hasattr(self, "_compat_raw_cb") and self._compat_raw_cb.isChecked()
        diff_html = ""
        for field_name in note.field_names:
            old_val = note.original_fields.get(field_name, "")
            new_val = note.updated_fields.get(field_name, "")
            if raw:
                old_val = html.escape(old_val)
                new_val = html.escape(new_val)
            highlighted = highlight_diff(old_val, new_val)
            diff_html += f"<b>{html.escape(field_name)}</b>:<br>{highlighted}<br><br>"
        model_css = _get_model_css(note.model_name)
        _set_preview_html(self._compat_diff_browser, diff_html, model_css)

    def select_all(self):
        self._select_all()

    def deselect_all(self):
        self._deselect_all()

    def load_note(self, note: NoteDiff):
        idx = next((i for i, n in enumerate(self.note_diffs) if n.note_id == note.note_id), None)
        if idx is not None:
            self._list.setCurrentRow(idx)


# ---------------------------------------------------------------------------
# Compat shim classes
# ---------------------------------------------------------------------------

class _FieldWidgetShim(dict):
    def __init__(self, fp: "FieldDiffPanel", note: NoteDiff, field_name: str):
        super().__init__()
        self._fp = fp
        self._note = note
        self._field_name = field_name
        self._orig_val = note.original_fields.get(field_name, "")
        self._imp_val  = note.updated_fields.get(field_name, "")
        self._build()

    def _build(self):
        orig_val = self._orig_val
        imp_val  = self._imp_val

        orig_radio = QtWidgets.QRadioButton()
        imp_radio  = QtWidgets.QRadioButton()
        edit_radio = QtWidgets.QRadioButton()

        self._btn_group = QtWidgets.QButtonGroup()
        self._btn_group.addButton(orig_radio)
        self._btn_group.addButton(imp_radio)
        self._btn_group.addButton(edit_radio)

        text_edit = _ShimTextEdit(self._note, self._field_name, imp_val)
        imp_radio.setChecked(True)
        text_edit.setEnabled(False)
        self._note.merged_fields[self._field_name] = imp_val

        orig_radio.toggled.connect(
            lambda checked, v=orig_val: self._on_mode("orig", v) if checked else None
        )
        imp_radio.toggled.connect(
            lambda checked, v=imp_val: self._on_mode("imp", v) if checked else None
        )
        edit_radio.toggled.connect(
            lambda checked: self._on_edit_mode() if checked else None
        )

        self["orig_radio"] = orig_radio
        self["imp_radio"]  = imp_radio
        self["edit_radio"] = edit_radio
        self["text_edit"]  = text_edit

    def _on_mode(self, mode: str, val: str):
        te = self["text_edit"]
        te._set_value(val)
        te.setEnabled(False)
        self._note.merged_fields[self._field_name] = val
        accepted = (mode == "imp")
        for hw in self._fp._hunk_widgets:
            hw.hunk.accepted = accepted
            hw._refresh_state()
        if hasattr(self._fp, "_refresh_preview"):
            self._fp._refresh_preview()

    def _on_edit_mode(self):
        te = self["text_edit"]
        te.setEnabled(True)
        custom = self._note.custom_edit_fields.get(self._field_name, self._imp_val)
        te._set_value(custom)
        self._note.merged_fields[self._field_name] = custom


class _ShimTextEdit(QtWidgets.QTextEdit):
    def __init__(self, note: NoteDiff, field_name: str, initial_val: str):
        super().__init__()
        self._note = note
        self._field_name = field_name
        self._ignore = False
        self.setPlainText(initial_val)
        self.textChanged.connect(self._on_changed)

    def _set_value(self, val: str):
        self._ignore = True
        self.setPlainText(val)
        self._ignore = False

    def _on_changed(self):
        if self._ignore:
            return
        val = self.toPlainText()
        self._note.merged_fields[self._field_name] = val
        self._note.custom_edit_fields[self._field_name] = val


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def show_diff_dialog(note_diffs: List[NoteDiff]) -> Optional[List[NoteDiff]]:
    dialog = VisualDiffDialog(note_diffs)
    try:
        if dialog.exec() == QtWidgets.QDialog.DialogCode.Accepted:
            return dialog.note_diffs
        return None
    finally:
        try:
            dialog.cleanup()
        except Exception:
            pass
