"""
Tests unitaires pour la persistance et la restauration de la mise en page de l'explorateur
dans anki_import_diff (tailles de splitters, géométrie, Screen 1 vs Screen 2).
"""

import os
import json
import pytest
from unittest.mock import MagicMock
from PyQt6 import QtWidgets, QtCore

import aqt
from src.browser_integration import (
    _save_browser_state,
    _restore_browser_state,
    _load_browser_layout_state,
    _scale_sizes,
    _get_layout_file_path,
    DiffBrowserPanel,
)


def test_scale_sizes():
    # Taille totale identique
    assert _scale_sizes([260, 740], 1000) == [260, 740]
    # Redimensionnement agrandi
    scaled = _scale_sizes([260, 740], 1200)
    assert scaled[0] + scaled[1] == 1200
    assert abs(scaled[0] - int(260 * 1.2)) <= 1
    # Redimensionnement rétréci
    scaled_small = _scale_sizes([260, 740], 800)
    assert scaled_small[0] + scaled_small[1] == 800
    # Cas limites
    assert _scale_sizes([], 1000) == []
    assert _scale_sizes([100], 1000) == [100]
    assert _scale_sizes([260, 740], 0) == [260, 740]


def test_save_and_load_layout_state(tmp_path, monkeypatch):
    saved_config = {}
    mock_am = MagicMock()
    mock_am.getConfig.side_effect = lambda m: saved_config.copy()
    def mock_write(m, c):
        nonlocal saved_config
        saved_config = c.copy()
    mock_am.writeConfig.side_effect = mock_write
    
    mock_mw = MagicMock()
    mock_mw.addonManager = mock_am
    monkeypatch.setattr(aqt, "mw", mock_mw)

    # Mock user_files path
    layout_file = str(tmp_path / "browser_diff_layout.json")
    monkeypatch.setattr("src.browser_integration._get_layout_file_path", lambda: layout_file)

    # Create dummy browser with form.splitter
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    browser = QtWidgets.QMainWindow()
    browser.resize(1000, 600)
    browser.form = MagicMock()
    main_splitter = QtWidgets.QSplitter(QtCore.Qt.Orientation.Horizontal)
    w_table = QtWidgets.QWidget()
    w_editor = QtWidgets.QWidget()
    main_splitter.addWidget(w_table)
    main_splitter.addWidget(w_editor)
    main_splitter.resize(1000, 600)
    main_splitter.setSizes([260, 740])
    browser.form.splitter = main_splitter

    sub_splitter = QtWidgets.QSplitter(QtCore.Qt.Orientation.Horizontal)
    w_sub1 = QtWidgets.QWidget()
    w_sub2 = QtWidgets.QWidget()
    sub_splitter.addWidget(w_sub1)
    sub_splitter.addWidget(w_sub2)
    sub_splitter.resize(740, 600)
    sub_splitter.setSizes([480, 260])

    _save_browser_state(browser, sub_splitter)

    # Vérifier que le fichier local et l'addonManager ont bien reçu les données
    state = _load_browser_layout_state()
    assert abs(state.get("main_splitter_sizes")[0] - 260) <= 5
    assert abs(state.get("editor_diff_sizes")[0] - 480) <= 5
    assert "geometry" in state

    assert os.path.exists(layout_file)
    with open(layout_file, "r", encoding="utf-8") as f:
        data = json.load(f)
    assert abs(data["main_splitter_sizes"][0] - 260) <= 5
    assert abs(data["editor_diff_sizes"][0] - 480) <= 5


def test_restore_browser_state_applies_screen_1_proportions(tmp_path, monkeypatch):
    layout_file = str(tmp_path / "browser_diff_layout.json")
    with open(layout_file, "w", encoding="utf-8") as f:
        json.dump({
            "main_splitter_sizes": [260, 740],
            "editor_diff_sizes": [480, 260]
        }, f)
    monkeypatch.setattr("src.browser_integration._get_layout_file_path", lambda: layout_file)
    monkeypatch.setattr(aqt, "mw", None)

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    browser = QtWidgets.QMainWindow()
    browser.form = MagicMock()
    main_splitter = QtWidgets.QSplitter(QtCore.Qt.Orientation.Horizontal)
    w_table = QtWidgets.QWidget()
    w_right = QtWidgets.QWidget()
    main_splitter.addWidget(w_table)
    main_splitter.addWidget(w_right)
    main_splitter.resize(1000, 600)
    browser.form.splitter = main_splitter

    sub_splitter = QtWidgets.QSplitter(QtCore.Qt.Orientation.Horizontal)
    w_sub1 = QtWidgets.QWidget()
    w_sub2 = QtWidgets.QWidget()
    sub_splitter.addWidget(w_sub1)
    sub_splitter.addWidget(w_sub2)
    sub_splitter.resize(740, 600)

    _restore_browser_state(browser, sub_splitter)

    # Proportions restaurées conformes au Screen 1
    assert main_splitter.sizes()[0] < main_splitter.sizes()[1]
    assert abs(main_splitter.sizes()[0] - 260) <= 5
    assert sub_splitter.sizes()[0] > sub_splitter.sizes()[1]
    assert abs(sub_splitter.sizes()[0] - 480) <= 5


def test_pre_diff_splitter_restored_cleanly(monkeypatch):
    monkeypatch.setattr(aqt, "mw", None)
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    browser = QtWidgets.QMainWindow()
    browser.form = MagicMock()
    form_splitter = QtWidgets.QSplitter(QtCore.Qt.Orientation.Horizontal)
    table = QtWidgets.QWidget()
    fields_area = QtWidgets.QWidget()
    form_splitter.addWidget(table)
    form_splitter.addWidget(fields_area)
    form_splitter.resize(1000, 600)
    form_splitter.setSizes([250, 750])
    browser.form.splitter = form_splitter

    # Initialiser DiffBrowserPanel
    panel = DiffBrowserPanel.__new__(DiffBrowserPanel)
    QtWidgets.QWidget.__init__(panel)
    panel._browser = browser
    panel._editor_diff_splitter = None
    panel._original_splitter_widget = None
    panel._save_timer = None

    initial_sizes = form_splitter.sizes()

    # Simuler injection
    panel._inject_into_browser_splitter()
    assert panel._pre_diff_main_splitter_sizes == initial_sizes
    assert form_splitter.widget(1) == panel._editor_diff_splitter

    # Simuler fermeture / restauration
    panel._restore_browser_splitter()
    assert form_splitter.widget(1) == fields_area
    assert form_splitter.sizes() == initial_sizes
