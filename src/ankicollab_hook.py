"""
ankicollab_hook.py — Interception des mises à jour AnkiCollab pour Anki Import Diff.

Intercepte l'installation des mises à jour de paquets partagés via AnkiCollab
(addon officiel ID 1957538407) afin d'exécuter un dry-run avec capture des
différences (savepoint SQLite) et d'ouvrir la vue Visual Diff dans l'explorateur
Anki pour permettre à l'utilisateur de réviser, valider ou rejeter les modifications.
"""

from __future__ import annotations

import importlib
import os
import sys
import threading
from typing import Any, Callable, Dict, List, Optional, Set

try:
    import aqt
    from aqt import mw, gui_hooks
    from aqt.operations import QueryOp
    from aqt.utils import tooltip
except ImportError:
    aqt = None
    mw = None
    gui_hooks = None
    QueryOp = None
    tooltip = None

from .savepoint import NoteDiff, capture_import_changes
from .browser_integration import open_browser_diff

_ANKICOLLAB_ADDON_ID = "1957538407"

_in_collab_dry_run = False
_is_hooked = False
_is_suggestions_hooked = False
_new_deck_subscriptions: Set[str] = set()


def is_in_collab_dry_run() -> bool:
    """Retourne True si un import AnkiCollab est en cours de simulation savepoint."""
    return _in_collab_dry_run


def confirm_import_or_abort_suggestion(parent_widget=None) -> bool:
    """
    Vérifie si une révision de diff AnkiCollab est en cours.
    Si oui, bloque la suggestion et invite l'utilisateur à confirmer l'importation.
    Retourne True si l'importation a été confirmée (la suggestion peut continuer),
    ou False si la suggestion doit être annulée.
    """
    from .browser_integration import is_diff_session_active, get_active_browser_panel
    if not is_diff_session_active():
        return True

    # Une session de diff est active : inviter l'utilisateur à confirmer l'importation
    try:
        from PyQt6.QtWidgets import QMessageBox, QApplication
    except ImportError:
        try:
            from PySide6.QtWidgets import QMessageBox, QApplication
        except ImportError:
            return True

    parent = parent_widget or (QApplication.activeWindow() if QApplication.instance() else None) or mw

    msg_box = QMessageBox(parent)
    msg_box.setWindowTitle("Importation AnkiCollab en attente")
    msg_box.setText(
        "Une importation AnkiCollab est actuellement en cours de révision dans le panneau Diff.\n\n"
        "Pour pouvoir proposer une modification (suggestion) à AnkiCollab, vous devez "
        "d'abord confirmer (appliquer) les modifications en attente sur votre collection.\n\n"
        "Souhaitez-vous confirmer l'importation maintenant ?"
    )
    msg_box.setIcon(QMessageBox.Icon.Question)

    btn_confirm = msg_box.addButton("Confirmer l'importation", QMessageBox.ButtonRole.AcceptRole)
    btn_review = msg_box.addButton("Continuer la révision", QMessageBox.ButtonRole.ActionRole)
    btn_cancel = msg_box.addButton("Annuler", QMessageBox.ButtonRole.RejectRole)
    msg_box.setDefaultButton(btn_confirm)

    msg_box.exec()
    clicked = msg_box.clickedButton()

    panel = get_active_browser_panel()

    if clicked == btn_confirm:
        if panel:
            panel.apply_commit()
        return True
    elif clicked == btn_review:
        if panel and hasattr(panel, "_browser") and panel._browser:
            try:
                panel._browser.activateWindow()
                panel._browser.raise_()
                panel.setFocus()
            except Exception:
                pass
        return False
    else:
        return False


def hook_ankicollab_suggestions() -> bool:
    """
    Branche la sécurité sur suggest_notes, suggest_subdeck et bulk_suggest_handler
    de l'addon AnkiCollab pour empêcher de proposer un hunk/suggestion tant qu'une
    importation diff est en attente de validation.
    """
    global _is_suggestions_hooked
    em_name = f"{_ANKICOLLAB_ADDON_ID}.export_manager"
    hk_name = f"{_ANKICOLLAB_ADDON_ID}.hooks"

    em = sys.modules.get(em_name)
    if em is None:
        if mw is not None and getattr(mw, "col", None) is not None:
            try:
                em = importlib.import_module(em_name)
            except (Exception, SystemExit):
                em = None

    hk = sys.modules.get(hk_name)
    if hk is None:
        if mw is not None and getattr(mw, "col", None) is not None:
            try:
                hk = importlib.import_module(hk_name)
            except (Exception, SystemExit):
                hk = None

    hooked_any = False

    if em and hasattr(em, "suggest_notes"):
        orig_suggest_notes = em.suggest_notes
        if not (getattr(orig_suggest_notes, "_import_diff_hooked", False) is True):
            def hooked_suggest_notes(nids, rationale_id, editor=None):
                parent = (editor.widget if editor and hasattr(editor, "widget") else None)
                if not confirm_import_or_abort_suggestion(parent):
                    return None
                return orig_suggest_notes(nids, rationale_id, editor)

            hooked_suggest_notes._import_diff_hooked = True
            hooked_suggest_notes._orig_fn = orig_suggest_notes
            em.suggest_notes = hooked_suggest_notes
            if hk and hasattr(hk, "suggest_notes"):
                hk.suggest_notes = hooked_suggest_notes
            hooked_any = True

    if em and hasattr(em, "suggest_subdeck"):
        orig_suggest_subdeck = em.suggest_subdeck
        if not (getattr(orig_suggest_subdeck, "_import_diff_hooked", False) is True):
            def hooked_suggest_subdeck(did):
                if not confirm_import_or_abort_suggestion():
                    return None
                return orig_suggest_subdeck(did)

            hooked_suggest_subdeck._import_diff_hooked = True
            hooked_suggest_subdeck._orig_fn = orig_suggest_subdeck
            em.suggest_subdeck = hooked_suggest_subdeck
            if hk and hasattr(hk, "suggest_subdeck"):
                hk.suggest_subdeck = hooked_suggest_subdeck
            hooked_any = True

    if hk and hasattr(hk, "bulk_suggest_handler"):
        orig_bulk = hk.bulk_suggest_handler
        if not (getattr(orig_bulk, "_import_diff_hooked", False) is True):
            def hooked_bulk_suggest_handler(browser, nids):
                if not confirm_import_or_abort_suggestion(browser):
                    return None
                return orig_bulk(browser, nids)

            hooked_bulk_suggest_handler._import_diff_hooked = True
            hooked_bulk_suggest_handler._orig_fn = orig_bulk
            hk.bulk_suggest_handler = hooked_bulk_suggest_handler
            hooked_any = True

    if hooked_any:
        _is_suggestions_hooked = True
    return _is_suggestions_hooked


def is_ankicollab_diff_enabled() -> bool:
    """True si le diff visuel est activé globalement et pour AnkiCollab."""
    try:
        from .hooks import is_visual_diff_enabled
        if not is_visual_diff_enabled():
            return False
    except Exception:
        pass

    try:
        if mw and hasattr(mw, "addonManager"):
            cfg = mw.addonManager.getConfig("anki_import_diff")
            if isinstance(cfg, dict):
                return bool(cfg.get("ankicollab_diff_enabled", True))
    except Exception:
        pass
    return True


def setup_ankicollab_hook() -> bool:
    """
    Tente de brancher les hooks d'interception sur AnkiCollab (import_manager et export_manager).
    Retourne True si le branchement a réussi.
    """
    global _is_hooked
    hook_ankicollab_suggestions()
    if _is_hooked:
        return True

    mod_name = f"{_ANKICOLLAB_ADDON_ID}.import_manager"
    im = sys.modules.get(mod_name)

    if im is None:
        if mw is None or getattr(mw, "col", None) is None:
            return False
        try:
            im = importlib.import_module(mod_name)
        except (Exception, SystemExit):
            return False

    if not im or not hasattr(im, "_install_deck_op") or not hasattr(im, "_on_deck_installed"):
        return False

    orig_handle_pull = getattr(im, "handle_pull", None)
    orig_install_update = getattr(im, "install_update", None)
    orig_install_deck_op = im._install_deck_op
    orig_on_deck_installed = im._on_deck_installed

    if getattr(orig_install_deck_op, "_import_diff_hooked", False) is True:
        _is_hooked = True
        return True

    # 0. Protection contre les appels concurrents ou multiples à handle_pull (ex: hooks Anki startup + auto-sync)
    if orig_handle_pull and not (getattr(orig_handle_pull, "_import_diff_hooked", False) is True):
        _pull_in_flight = False
        _last_pull_timestamp = 0.0

        def hooked_handle_pull(input_hash=None, silent=False):
            nonlocal _pull_in_flight, _last_pull_timestamp
            import time
            now = time.time()
            if _pull_in_flight or (now - _last_pull_timestamp < 3.0):
                print(f"[anki-diff] AnkiCollab: handle_pull({input_hash}) ignoré (pull déjà actif ou récent).")
                return None
            _pull_in_flight = True
            _last_pull_timestamp = now

            def _on_pull_done():
                nonlocal _pull_in_flight
                _pull_in_flight = False

            try:
                return orig_handle_pull(input_hash, silent)
            finally:
                if mw and hasattr(mw, "taskman"):
                    try:
                        from PyQt6 import QtCore
                        QtCore.QTimer.singleShot(4000, _on_pull_done)
                    except Exception:
                        _pull_in_flight = False
                else:
                    _pull_in_flight = False

        hooked_handle_pull._import_diff_hooked = True
        im.handle_pull = hooked_handle_pull

    # 1. Tracker les nouvelles souscriptions complètes et filtrer les installs concurrentes
    _in_flight_installs: Set[str] = set()
    if orig_install_update and not (getattr(orig_install_update, "_import_diff_hooked", False) is True):
        def hooked_install_update(subscription, input_hash=None, update_timestamp_after=False):
            deck_hash = subscription.get("deck_hash") if isinstance(subscription, dict) else None
            if input_hash and deck_hash:
                _new_deck_subscriptions.add(deck_hash)

            if deck_hash and deck_hash in _in_flight_installs:
                print(f"[anki-diff] AnkiCollab: install_update({deck_hash}) ignoré car déjà en cours.")
                return None

            if deck_hash:
                _in_flight_installs.add(deck_hash)

            try:
                return orig_install_update(subscription, input_hash, update_timestamp_after)
            finally:
                if deck_hash:
                    if mw and hasattr(mw, "taskman"):
                        try:
                            from PyQt6 import QtCore
                            QtCore.QTimer.singleShot(6000, lambda: _in_flight_installs.discard(deck_hash))
                        except Exception:
                            _in_flight_installs.discard(deck_hash)
                    else:
                        _in_flight_installs.discard(deck_hash)

        hooked_install_update._import_diff_hooked = True
        im.install_update = hooked_install_update

    # 2. Intercepter _install_deck_op pour exécuter le dry-run savepoint
    def hooked_install_deck_op(deck, config, map_cache=None, note_type_data=None):
        deck_hash = getattr(config, "deck_hash", None)

        # Si Visual Diff est désactivé ou s'il s'agit d'une nouvelle souscription complète
        if not is_ankicollab_diff_enabled() or (deck_hash and deck_hash in _new_deck_subscriptions):
            return orig_install_deck_op(deck, config, map_cache, note_type_data)

        global _in_collab_dry_run
        _in_collab_dry_run = True
        dry_run_res = None

        # Neutraliser temporairement le snapshot Anki_EDN_Installer pendant le dry-run
        fp_mod = sys.modules.get("Anki_EDN_Installer.field_protection")
        orig_fp_snapshot = None
        if fp_mod and hasattr(fp_mod, "snapshot"):
            orig_fp_snapshot = fp_mod.snapshot
            fp_mod.snapshot = lambda *a, **kw: None

        try:
            def _dry_run_action():
                nonlocal dry_run_res
                dry_run_res = orig_install_deck_op(deck, config, map_cache, note_type_data)
                return dry_run_res

            diffs = capture_import_changes(_dry_run_action)
        except Exception as e:
            print(f"[anki-diff] Erreur dry-run AnkiCollab: {e}, fallback sur l'import standard")
            diffs = None
        finally:
            _in_collab_dry_run = False
            if fp_mod and orig_fp_snapshot is not None:
                fp_mod.snapshot = orig_fp_snapshot

        # Si le dry-run a échoué ou si aucune note n'a été modifiée
        if not diffs:
            print("[anki-diff] AnkiCollab: aucune différence de carte détectée lors du dry-run.")
            # Réexécuter sans savepoint pour persister les métadonnées de deck
            return orig_install_deck_op(deck, config, map_cache, note_type_data)

        # Diffs détectés : renvoyer le paquet de diff au handler de succès
        return {
            "_is_diff_run": True,
            "diffs": diffs,
            "dry_run_res": dry_run_res,
            "deck": deck,
            "config": config,
        }

    hooked_install_deck_op._import_diff_hooked = True
    im._install_deck_op = hooked_install_deck_op

    # 3. Intercepter _on_deck_installed pour ouvrir la fenêtre de diff
    def hooked_on_deck_installed(install_result, deck, subscription, input_hash=None, update_timestamp_after=False):
        deck_hash = subscription.get("deck_hash") if isinstance(subscription, dict) else None
        if deck_hash:
            _new_deck_subscriptions.discard(deck_hash)
            _in_flight_installs.discard(deck_hash)

        if isinstance(install_result, dict) and install_result.get("_is_diff_run"):
            diffs = install_result["diffs"]
            dry_run_res = install_result["dry_run_res"]

            def _on_collab_commit(accepted_diffs):
                try:
                    # Finaliser AnkiCollab : téléchargement des médias manquants, mise à jour du timestamp
                    orig_on_deck_installed(dry_run_res, deck, subscription, input_hash, update_timestamp_after)

                    # Prendre le snapshot du baseline si field_protection est actif
                    fp_mod = sys.modules.get("Anki_EDN_Installer.field_protection")
                    if fp_mod and hasattr(fp_mod, "snapshot") and deck_hash:
                        try:
                            fp_mod.snapshot(deck_hash)
                        except Exception:
                            pass

                    if tooltip:
                        tooltip(f"AnkiCollab : {len(accepted_diffs)} carte(s) mise(s) à jour.")
                except Exception as ex:
                    print(f"[anki-diff] Erreur finalisation AnkiCollab: {ex}")

            open_browser_diff(diffs, _on_collab_commit)
            return

        return orig_on_deck_installed(install_result, deck, subscription, input_hash, update_timestamp_after)

    hooked_on_deck_installed._import_diff_hooked = True
    im._on_deck_installed = hooked_on_deck_installed

    _is_hooked = True
    hook_ankicollab_suggestions()
    print("[anki-diff] Hook AnkiCollab installé avec succès")
    return True


_menu_action_installed = False


def setup_tools_menu() -> None:
    """Ajoute un sous-menu regroupant les options Visual Diff dans le menu Outils d'Anki."""
    global _menu_action_installed
    if not mw or not hasattr(mw, "form") or not hasattr(mw.form, "menuTools"):
        return

    try:
        from PyQt6.QtGui import QAction
        from PyQt6.QtWidgets import QMenu
    except ImportError:
        try:
            from PySide6.QtGui import QAction
            from PySide6.QtWidgets import QMenu
        except ImportError:
            return

    from .hooks import is_visual_diff_enabled

    tools_menu = mw.form.menuTools

    # Nettoyage des anciennes actions orphelines du menu Outils si présentes
    for old_act in list(tools_menu.actions()):
        txt = old_act.text() or ""
        if "Visual Diff lors" in txt or "Diff: Filtrer" in txt or "Diff : Filtrer" in txt:
            tools_menu.removeAction(old_act)
            try:
                old_act.deleteLater()
            except Exception:
                pass

    # Récupérer ou créer le sous-menu Visual Diff
    diff_menu = None
    for act in tools_menu.actions():
        m = act.menu()
        if m and (m.objectName() == "VisualDiffToolsMenu" or m.title() in ("Visual Diff", "Diff")):
            diff_menu = m
            break

    if diff_menu is None:
        diff_menu = QMenu("Visual Diff", tools_menu)
        diff_menu.setObjectName("VisualDiffToolsMenu")
        tools_menu.addMenu(diff_menu)

    diff_menu.clear()

    act = QAction("Activer lors des imports et AnkiCollab", diff_menu)
    act.setObjectName("VisualDiff_Toggle_Import_Collab")
    act.setCheckable(True)
    act.setChecked(is_visual_diff_enabled())

    def on_toggle(checked: bool):
        try:
            from . import hooks
            hooks._visual_diff_enabled = checked
            cfg = mw.addonManager.getConfig("anki_import_diff") or {}
            cfg["visual_diff_enabled"] = checked
            cfg["ankicollab_diff_enabled"] = checked
            mw.addonManager.writeConfig("anki_import_diff", cfg)
            msg = "Visual Diff activé pour les imports et AnkiCollab." if checked else "Visual Diff désactivé."
            if tooltip:
                tooltip(msg, period=2000)
        except Exception as e:
            print(f"[anki-diff] Erreur bascule Visual Diff: {e}")

    act.triggered.connect(on_toggle)
    diff_menu.addAction(act)

    cfg = mw.addonManager.getConfig("anki_import_diff") or {} if hasattr(mw, "addonManager") else {}
    act_learned = QAction("Filtrer sur les cartes déjà apprises par défaut", diff_menu)
    act_learned.setObjectName("VisualDiff_Toggle_Learned_Only")
    act_learned.setCheckable(True)
    act_learned.setChecked(cfg.get("learned_cards_only", False))

    def on_toggle_learned(checked: bool):
        try:
            c = mw.addonManager.getConfig("anki_import_diff") or {}
            c["learned_cards_only"] = checked
            mw.addonManager.writeConfig("anki_import_diff", c)
            msg = "Filtre cartes apprises activé par défaut." if checked else "Filtre cartes apprises désactivé par défaut."
            if tooltip:
                tooltip(msg, period=2000)
        except Exception as e:
            print(f"[anki-diff] Erreur réglage cartes apprises: {e}")

    def _refresh_menu_actions():
        try:
            act.setChecked(is_visual_diff_enabled())
            c = mw.addonManager.getConfig("anki_import_diff") or {} if hasattr(mw, "addonManager") else {}
            act_learned.setChecked(c.get("learned_cards_only", False))
        except Exception:
            pass

    diff_menu.aboutToShow.connect(_refresh_menu_actions)
    act_learned.triggered.connect(on_toggle_learned)
    diff_menu.addAction(act_learned)
    _menu_action_installed = True
