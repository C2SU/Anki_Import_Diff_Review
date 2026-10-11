"""
theme.py — Système de palette dynamique et styles dérivés pour Anki Import Diff.

Inspiré de l'architecture de smart_search_medical :
Toutes les couleurs sont dérivées mathématiquement de la QPalette de l'application
ou du widget. Les changements de thème d'Anki (mode nuit, clair, thèmes personnalisés)
sont pris en compte automatiquement via PaletteMixin et changeEvent.
"""

from __future__ import annotations

from typing import Optional, Dict

try:
    from PyQt6 import QtCore, QtGui, QtWidgets
    _Qt = QtCore.Qt
    _QColor = QtGui.QColor
    _QPainter = QtGui.QPainter
    _QPalette = QtGui.QPalette
    _QPen = QtGui.QPen
    _QBrush = QtGui.QBrush
    _QPixmap = QtGui.QPixmap
    _QWidget = QtWidgets.QWidget
    _QFrame = QtWidgets.QFrame
    _QApplication = QtWidgets.QApplication
    _QEvent = QtCore.QEvent
except ImportError:
    from PySide6 import QtCore, QtGui, QtWidgets
    _Qt = QtCore.Qt
    _QColor = QtGui.QColor
    _QPainter = QtGui.QPainter
    _QPalette = QtGui.QPalette
    _QPen = QtGui.QPen
    _QBrush = QtGui.QBrush
    _QPixmap = QtGui.QPixmap
    _QWidget = QtWidgets.QWidget
    _QFrame = QtWidgets.QFrame
    _QApplication = QtWidgets.QApplication
    _QEvent = QtCore.QEvent


def _hex(color: _QColor) -> str:
    """Retourne la couleur au format hexadécimal '#rrggbb'."""
    return color.name()


def blend_colors(base: _QColor, overlay: _QColor, alpha: float) -> _QColor:
    """Mélange 'overlay' à proportion 'alpha' sur 'base' (les deux en QColor)."""
    alpha = max(0.0, min(1.0, alpha))
    out = _QColor(base)
    out.setRed(round(base.red() * (1.0 - alpha) + overlay.red() * alpha))
    out.setGreen(round(base.green() * (1.0 - alpha) + overlay.green() * alpha))
    out.setBlue(round(base.blue() * (1.0 - alpha) + overlay.blue() * alpha))
    return out


def chrome_colors(widget: Optional[_QWidget] = None) -> dict[str, str]:
    """
    Calcule et retourne un dictionnaire complet de couleurs dérivées de la palette.
    Fonctionne harmonieusement en mode sombre comme en mode clair.
    """
    app = _QApplication.instance()
    app_pal = app.palette() if app is not None else _QPalette()
    pal = widget.palette() if widget is not None else app_pal

    def concrete(role: _QPalette.ColorRole) -> _QColor:
        color = pal.color(role)
        if color.alpha() == 0 and app_pal is not None:
            color = app_pal.color(role)
        return color

    base = concrete(_QPalette.ColorRole.Base)
    text = concrete(_QPalette.ColorRole.Text)
    accent = concrete(_QPalette.ColorRole.Highlight)
    accent_text = concrete(_QPalette.ColorRole.HighlightedText)
    window = concrete(_QPalette.ColorRole.Window)
    button = concrete(_QPalette.ColorRole.Button)
    
    is_dark = base.lightness() < 128

    # Couleurs sémantiques adaptées au contraste
    ok = "#34D399" if is_dark else "#059669"
    warn = "#F59E0B" if is_dark else "#D97706"
    err = "#F87171" if is_dark else "#DC2626"

    # Surfaces dérivées
    surface = blend_colors(window, text, 0.055)
    surface_high = blend_colors(window, text, 0.09)
    surface_hover = blend_colors(window, text, 0.13)
    
    border = blend_colors(window, text, 0.16)
    border_strong = blend_colors(window, text, 0.28)
    
    accent_soft = blend_colors(base, accent, 0.16)
    accent_mid = blend_colors(base, accent, 0.32)
    accent_hover = blend_colors(accent, accent_text, 0.12)

    # Couleurs de diff (Ajout / Suppression / Warning)
    add_color = _QColor("#10B981" if is_dark else "#059669")
    rem_color = _QColor("#EF4444" if is_dark else "#DC2626")
    warn_color = _QColor(warn)

    add_bg = blend_colors(base, add_color, 0.18 if is_dark else 0.12)
    add_border = blend_colors(base, add_color, 0.45 if is_dark else 0.35)
    
    rem_bg = blend_colors(base, rem_color, 0.18 if is_dark else 0.10)
    rem_border = blend_colors(base, rem_color, 0.45 if is_dark else 0.35)

    warn_bg = blend_colors(base, warn_color, 0.18 if is_dark else 0.12)
    warn_border = blend_colors(base, warn_color, 0.45 if is_dark else 0.35)

    placeholder = concrete(_QPalette.ColorRole.PlaceholderText)

    return {
        "window": _hex(window),
        "base": _hex(base),
        "text": _hex(text),
        "text_primary": _hex(text),
        "text_secondary": _hex(blend_colors(text, window, 0.35)),
        "text_muted": _hex(placeholder if placeholder.isValid() else blend_colors(text, window, 0.55)),
        "muted": _hex(placeholder if placeholder.isValid() else blend_colors(text, window, 0.55)),
        "accent": _hex(accent),
        "accent_text": _hex(accent_text),
        "accent_soft": _hex(accent_soft),
        "accent_mid": _hex(accent_mid),
        "accent_hover": _hex(accent_hover),
        "surface": _hex(surface),
        "surface_high": _hex(surface_high),
        "surface_hover": _hex(surface_hover),
        "button": _hex(button),
        "border": _hex(border),
        "border_strong": _hex(border_strong),
        "chip_bg": _hex(blend_colors(window, text, 0.08)),
        "chip_border": _hex(blend_colors(window, text, 0.22)),
        "ok": ok,
        "warn": warn,
        "err": err,
        "primary": _hex(accent),
        "primary_hover": _hex(accent_hover),
        "danger": err,
        "danger_hover": "#DC2626" if is_dark else "#B91C1C",
        "success": ok,
        "success_hover": "#059669" if is_dark else "#047857",
        "neutral_bg": _hex(surface_high),
        "neutral_bg_hover": _hex(surface_hover),
        "neutral_text": _hex(text),
        "background": _hex(base),
        "header": _hex(surface),
        "sel": _hex(blend_colors(base, accent, 0.22)),
        
        # Diff Hunk colors
        "add_fg": ok,
        "add_bg": _hex(add_bg),
        "add_border": _hex(add_border),
        "rem_fg": err,
        "rem_bg": _hex(rem_bg),
        "rem_border": _hex(rem_border),
        "warning": warn,
        "warning_bg": _hex(warn_bg),
        "warning_border": _hex(warn_border),
        "preview": _hex(base),
    }


class PaletteMixin:
    """Mixin pour les widgets Qt supportant l'actualisation dynamique du thème."""

    def refresh_palette(self) -> None:
        pass

    def _palette_colors(self) -> dict[str, str]:
        return chrome_colors(self if isinstance(self, _QWidget) else None)

    def changeEvent(self, event: _QEvent) -> None:
        if (
            event.type() in (_QEvent.Type.PaletteChange, _QEvent.Type.ApplicationPaletteChange)
            and not getattr(self, "_refreshing_palette", False)
        ):
            self._refreshing_palette = True
            try:
                self.refresh_palette()
            finally:
                self._refreshing_palette = False
        try:
            super().changeEvent(event)
        except AttributeError:
            pass


class _StatusDot(_QFrame):
    """Pastille indicatrice vectorielle peinte via QPainter (évite les bugs de cache QSS)."""

    def __init__(self, parent: Optional[_QWidget] = None, size: int = 10):
        super().__init__(parent)
        self._color = _QColor("#808080")
        self._size = size
        self.setFixedSize(size, size)

    def set_color(self, color: str | _QColor) -> None:
        self._color = _QColor(color) if isinstance(color, str) else color
        self.update()

    def paintEvent(self, _event) -> None:
        painter = _QPainter(self)
        painter.setRenderHint(_QPainter.RenderHint.Antialiasing, True)
        painter.setPen(_Qt.PenStyle.NoPen)
        painter.setBrush(self._color)
        painter.drawEllipse(0, 0, self._size, self._size)
        painter.end()


class ThemeColor:
    """Compatibilité avec l'ancien système de couleurs lazy-evaluated."""
    def __init__(self, name: str):
        self.name = name
    def __str__(self) -> str:
        return chrome_colors().get(self.name, "#808080")
    def __repr__(self) -> str:
        return chrome_colors().get(self.name, "#808080")
