import aqt
import threading
import json
import weakref
from typing import Callable, Any, List
from .savepoint import NoteDiff, capture_import_changes
from .ui import show_diff_dialog
from .browser_integration import open_browser_diff, setup_native_integration

_active_import_dialogs = []

# ---------------------------------------------------------------------------
# Debounce pour la capture de snapshots NoteHistoryDB
# ---------------------------------------------------------------------------
# Retarde la sauvegarde dans NoteHistoryDB lors de l'édition interactive
# (frappes clavier). L'écriture Anki native (Collection._original_update_note)
# reste toujours IMMÉDIATE — seul le snapshot historique est différé.
#
# Ne s'applique PAS lors des imports (is_collab, _pending_diffs actif,
# appels batch avec plusieurs notes, ou stack anki_import_diff détecté).
# ---------------------------------------------------------------------------
_DEBOUNCE_HISTORY_MS = 1500  # ms d'inactivité avant de capturer le snapshot (évite le spam de micro-snapshots)
_debounce_timers: dict = {}   # note_id (int) -> _DebounceTimer en attente


class _DebounceTimer:
    """Timer léger basé sur QTimer évitant la prolifération de threads OS."""
    def __init__(self, ms: int, callback: Callable):
        self.callback = callback
        self._qtimer = None
        self._thread_timer = None
        try:
            from aqt.qt import QTimer
            if aqt.mw is not None:
                self._qtimer = QTimer(aqt.mw)
                self._qtimer.setSingleShot(True)
                self._qtimer.timeout.connect(self._fire)
                self._qtimer.start(ms)
        except Exception:
            pass

        if self._qtimer is None:
            self._thread_timer = threading.Timer(ms / 1000.0, self._fire)
            self._thread_timer.daemon = True
            self._thread_timer.start()

    def _fire(self):
        try:
            self.callback()
        except Exception:
            pass

    def cancel(self):
        if self._qtimer is not None:
            try:
                self._qtimer.stop()
            except Exception:
                pass
        if self._thread_timer is not None:
            try:
                self._thread_timer.cancel()
            except Exception:
                pass

    def stop(self):
        self.cancel()

def register_import_dialog(dialog: Any) -> None:
    _active_import_dialogs.append(weakref.ref(dialog))

def close_active_import_dialogs() -> None:
    global _active_import_dialogs
    alive = []
    for ref in _active_import_dialogs:
        dlg = ref()
        if dlg is not None:
            alive.append(dlg)
    _active_import_dialogs.clear()
    for dlg in alive:
        try:
            dlg.reject()
        except Exception:
            try:
                dlg.close()
            except Exception:
                pass

def sanitize_surrogates(text: str) -> str:
    if not isinstance(text, str):
        return text
    try:
        return text.encode('utf-16', 'surrogatepass').decode('utf-16', 'ignore')
    except Exception:
        return text


# Garde-fou : évite le double-wrapping si setup_hooks est appelé plusieurs fois
_handlers_wrapped = False
_visual_diff_enabled = True


def is_visual_diff_enabled() -> bool:
    global _visual_diff_enabled
    try:
        config = aqt.mw.addonManager.getConfig("anki_import_diff")
        if isinstance(config, dict):
            _visual_diff_enabled = config.get("visual_diff_enabled", True)
        else:
            _visual_diff_enabled = True
    except Exception:
        pass
    return _visual_diff_enabled


def install_qt_message_handler() -> None:
    """
    Installs a custom Qt message handler to suppress wildcard disconnect warnings
    from QPushButton (and other widgets) during destruction.
    """
    try:
        try:
            from PyQt6 import QtCore
        except ImportError:
            from PySide6 import QtCore

        import sys

        def qt_message_handler(mode, context, message: str) -> None:
            # Filter out wildcard disconnect warnings
            if "wildcard call disconnects from destroyed signal" in message:
                return

            # Print other messages to stdout (except fatal) to prevent Anki from showing modal warnings
            # Use print() to handle platform-specific encoding safely
            if mode == QtCore.QtMsgType.QtFatalMsg:
                print(f"Qt fatal: {message}", file=sys.stderr)
                sys.exit(1)
            elif mode == QtCore.QtMsgType.QtCriticalMsg:
                print(f"Qt critical: {message}")
            elif mode == QtCore.QtMsgType.QtWarningMsg:
                print(f"Qt warning: {message}")
            elif mode == QtCore.QtMsgType.QtInfoMsg:
                print(f"Qt info: {message}")
            else:
                print(f"Qt debug: {message}")

        QtCore.qInstallMessageHandler(qt_message_handler)
        print("[anki-diff] Custom Qt message handler installed to suppress wildcard disconnect warnings")
    except Exception as e:
        print("[anki-diff] Failed to install custom Qt message handler:", e)


def install_js_console_filter() -> None:
    """
    Monkeypatches AnkiWebPage.javaScriptConsoleMessage to suppress noisy
    MathJax [tex]/mathtools and [tex]/noerrors warnings in the console.
    """
    try:
        import aqt.webview
        original_console_msg = aqt.webview.AnkiWebPage.javaScriptConsoleMessage

        def patched_console_msg(self, level, msg: str | None, line: int, srcID: str | None) -> None:
            if msg and ("[tex]/" in msg or "No version information available" in msg):
                return
            original_console_msg(self, level, msg, line, srcID)

        aqt.webview.AnkiWebPage.javaScriptConsoleMessage = patched_console_msg
        print("[anki-diff] Custom JS console message filter installed to suppress MathJax version warnings")
    except Exception as e:
        print("[anki-diff] Failed to install custom JS console filter:", e)


def setup_hooks() -> None:
    """
    Initializes and registers hooks to intercept the CSV and APKG import panels,
    inject the checkbox, and hijack import execution.

    Le wrapping des post_handlers Flask (getCsvMetadata, importCsv, importAnkiPackage)
    est différé dans profile_did_open car post_handlers n'est entièrement construit
    qu'après l'ouverture complète du profil Anki.
    """
    # Register custom message handlers to suppress warnings
    install_qt_message_handler()
    install_js_console_filter()
    is_visual_diff_enabled()

    # Register editor + browser native integration hooks (always active)
    try:
        setup_native_integration()
    except Exception as e:
        print("[anki_import_diff] native integration setup error:", e)

    # Monkeypatch modern dialog if importable (classe Python, OK à l'import)
    try:
        from aqt.import_export.import_dialog import ImportDialog as ModernDialog
        original_setup_ui = ModernDialog._setup_ui
        def patched_setup_ui(self, *args, **kwargs):
            original_setup_ui(self, *args, **kwargs)
            inject_checkbox(self)
            register_import_dialog(self)
        ModernDialog._setup_ui = patched_setup_ui
    except Exception as e:
        print("MODERN DIALOG PATCH ERROR:", e)

    # Monkeypatch legacy dialog if importable
    try:
        from aqt.importing import ImportDialog as LegacyDialog
        original_init = LegacyDialog.__init__
        def patched_init(self, *args, **kwargs):
            original_init(self, *args, **kwargs)
            inject_checkbox(self)
            register_import_dialog(self)
        LegacyDialog.__init__ = patched_init
    except Exception as e:
        print("LEGACY DIALOG PATCH ERROR:", e)

    # Différer le wrapping des Flask handlers après ouverture complète du profil.
    # À ce stade (chargement de l'add-on), post_handlers peut ne pas encore être
    # entièrement construit ou aqt.mw.col peut ne pas être disponible.
    try:
        from aqt import gui_hooks
        gui_hooks.profile_did_open.append(_wrap_flask_handlers_once)
        gui_hooks.profile_will_close.append(flush_pending_history_snapshots)
        print("[anki-diff] setup_hooks: profile_did_open and profile_will_close hooks registered")
    except Exception as e:
        # Fallback : essayer directement (si le profil est déjà ouvert, e.g. en test)
        print(f"[anki-diff] setup_hooks: hooks registration error ({e}), wrapping immediately")
        _wrap_flask_handlers_once()


def _wrap_flask_handlers_once() -> None:
    """
    Wrape les POST handlers Flask une seule fois après ouverture du profil.
    Appelé par le hook profile_did_open.
    """
    global _handlers_wrapped
    if _handlers_wrapped:
        return
    _handlers_wrapped = True

    try:
        from .history import NoteHistoryDB
        NoteHistoryDB.reset_instance()
        print("[anki-diff] NoteHistoryDB instance reset on profile open")
    except Exception as e:
        print("[anki-diff] Failed to reset NoteHistoryDB instance:", e)

    try:
        from aqt.mediasrv import post_handlers
        keys = list(post_handlers.keys())
        print(f"[anki-diff] _wrap_flask_handlers_once: post_handlers keys sample = {keys[:8]}")

        if "getCsvMetadata" in post_handlers:
            post_handlers["getCsvMetadata"] = _wrap_get_csv_metadata_handler(
                post_handlers["getCsvMetadata"]
            )
            print("[anki-diff] getCsvMetadata handler wrapped OK")
        else:
            print("[anki-diff] WARNING: getCsvMetadata not found in post_handlers!")

        if "getImportAnkiPackagePresets" in post_handlers:
            post_handlers["getImportAnkiPackagePresets"] = _wrap_get_anki_package_presets_handler(
                post_handlers["getImportAnkiPackagePresets"]
            )
            print("[anki-diff] getImportAnkiPackagePresets handler wrapped OK")

        if "importCsv" in post_handlers:
            post_handlers["importCsv"] = wrap_import_handler(post_handlers["importCsv"])
            print("[anki-diff] importCsv handler wrapped OK")

        if "importAnkiPackage" in post_handlers:
            post_handlers["importAnkiPackage"] = wrap_import_handler(post_handlers["importAnkiPackage"])
            print("[anki-diff] importAnkiPackage handler wrapped OK")

    except Exception as e:
        print(f"[anki-diff] FLASK HANDLERS WRAP ERROR: {e}")
        import traceback
        traceback.print_exc()

    try:
        patch_collection_updates()
    except Exception as e:
        print(f"[anki-diff] Failed to execute patch_collection_updates: {e}")



def inject_checkbox(dialog: Any) -> None:
    try:
        from PyQt6 import QtWidgets
    except ImportError:
        from PySide6 import QtWidgets
        
    layout = dialog.layout()
    if layout is None:
        # Try finding a layout or using standard method
        layout = dialog.findChild(QtWidgets.QLayout)
    if layout is None:
        return
        
    if hasattr(dialog, "visual_diff_checkbox"):
        return
        
    try:
        checkbox = QtWidgets.QCheckBox("Visual Diff Import")
        dialog.visual_diff_checkbox = checkbox
        
        # Load config
        is_enabled = is_visual_diff_enabled()
        checkbox.setChecked(is_enabled)
        
        # Forcer le mode "Mettre à jour" au premier affichage si la checkbox est cochée.
        # On ne le force qu'une seule fois : l'user garde la liberté de changer après.
        if is_enabled:
            _force_update_mode_on_dialog(dialog)
        
        # Save config on toggle
        def on_toggled(checked):
            global _visual_diff_enabled
            _visual_diff_enabled = checked
            
            cfg = aqt.mw.addonManager.getConfig("anki_import_diff")
            if not isinstance(cfg, dict):
                cfg = {}
            cfg["visual_diff_enabled"] = checked
            aqt.mw.addonManager.writeConfig("anki_import_diff", cfg)
            # Forcer le mode "Mettre à jour" une seule fois quand on active Visual Diff.
            # L'user reste libre de le rechanger ensuite.
            if checked:
                _force_update_mode_on_dialog(dialog)
            
        checkbox.toggled.connect(on_toggled)
        
        layout.addWidget(checkbox)
    except Exception:
        pass


def _wrap_get_anki_package_presets_handler(original_handler: "Callable[[], bytes]") -> "Callable[[], bytes]":
    """
    Wrapper pour le handler POST getImportAnkiPackagePresets.

    Force l'option update_notes à 1 (ALWAYS) pour que la dialog d'importation
    d'Anki package (.apkg) s'ouvre avec "Mettre à jour" sélectionné par défaut.
    """
    def wrapped() -> bytes:
        raw = original_handler()
        if not raw:
            return raw
        try:
            if not is_visual_diff_enabled():
                return raw
            from anki.import_export_pb2 import ImportAnkiPackageOptions
            opts = ImportAnkiPackageOptions()
            opts.ParseFromString(raw)
            # update_notes: 0=IF_NEWER, 1=ALWAYS, 2=NEVER
            if opts.update_notes != 1:
                print(f"[anki-diff] getImportAnkiPackagePresets: overriding update_notes "
                      f"{opts.update_notes} -> 1 (ALWAYS)")
                opts.update_notes = 1
                return opts.SerializeToString()
        except Exception as e:
            print(f"[anki-diff] getImportAnkiPackagePresets wrapper error (non-fatal): {e}")
            import traceback
            traceback.print_exc()
        return raw
    return wrapped


def _wrap_get_csv_metadata_handler(original_handler: "Callable[[], bytes]") -> "Callable[[], bytes]":
    """
    Wrapper pour le handler POST getCsvMetadata.

    Le backend Rust retourne un CsvMetadata protobuf contenant dupe_resolution
    égal à la dernière valeur utilisée par l'utilisateur (persistée). Si cette
    valeur est DUPLICATE (2), le dialog Svelte affiche "Dupliquer" par défaut.

    Ce wrapper force dupe_resolution=0 (UPDATE) dans chaque réponse, ce qui
    fait que le dialog s'ouvre toujours avec "Mettre à jour" présélectionné.
    L'utilisateur reste libre de changer manuellement après.
    """
    def wrapped() -> bytes:
        raw = original_handler()
        if not raw:
            return raw
        try:
            if not is_visual_diff_enabled():
                return raw
            from anki.import_export_pb2 import CsvMetadata
            meta = CsvMetadata()
            meta.ParseFromString(raw)
            # dupe_resolution: 0=UPDATE, 1=PRESERVE, 2=DUPLICATE
            if meta.dupe_resolution != 0:
                print(f"[anki-diff] getCsvMetadata: overriding dupe_resolution "
                      f"{meta.dupe_resolution} -> 0 (UPDATE)")
                meta.dupe_resolution = 0
                return meta.SerializeToString()
            else:
                print("[anki-diff] getCsvMetadata: dupe_resolution already 0 (UPDATE), no override needed")
        except Exception as e:
            print(f"[anki-diff] getCsvMetadata wrapper error (non-fatal): {e}")
            import traceback
            traceback.print_exc()
        return raw
    return wrapped


def _force_update_mode_on_dialog(dialog: Any) -> None:
    """
    Force le mode d'import sur "Mettre à jour les notes existantes" dans la
    dialog d'import d'Anki, une seule fois, au premier affichage.

    L'user garde la liberté de rechanger le mode manuellement après.
    Stratégies (en ordre de priorité) :
      1. WebView JS — dialog moderne Anki >= 2.1.55 (Svelte / HTML)
      2. QComboBox Qt — dialog ancienne version
      3. QRadioButton Qt — version encore plus ancienne
    """
    # Stratégie 1 : WebView (dialog moderne Svelte)
    try:
        _force_update_mode_webview(dialog)
    except Exception:
        pass
    # Stratégie 2 & 3 : widgets Qt natifs (legacy)
    try:
        _force_update_mode_qt(dialog)
    except Exception:
        pass


def _force_update_mode_webview(dialog: Any) -> None:
    """
    Anki >= 2.1.55 : la dialog est rendue dans une AnkiWebView (Svelte).
    On injecte du JS pour sélectionner l'option "Mettre à jour".

    Le combobox Svelte utilise [role="combobox"] avec un .label interne.
    Le popover est référencé via aria-controls sur le combobox.
    On ouvre le combobox, puis on clique dans le popover sur l'option UPDATE.
    """
    web = getattr(dialog, "web", None)
    if web is None:
        try:
            from aqt.webview import AnkiWebView
            web = dialog.findChild(AnkiWebView)
        except Exception:
            pass
    if web is None:
        return

    # Mots-clés traduits dans le JS lui-même pour éviter les problèmes d'encodage Python.
    # "Mettre \u00e0 jour" = "Mettre à jour" en Unicode escape.
    js = r"""
(function() {
    var MAX_TRIES = 50;
    var INTERVAL  = 300;

    // Préfixes/valeurs indiquant que le mode actuel est "dupliquer" (à corriger)
    var DUPE_KW = ['dupliquer','duplicate','duplizieren','duplicar','duplicare',
                   'ignorar','ignore','keep existing'];

    // Mots-clés de l'option UPDATE (valeur cible)
    var UPDATE_KW = ['mettre \u00e0 jour','mettre a jour',
                     'update note','update notes','update existing',
                     'aktualisieren','actualizar','aggiornare','atualizar'];

    function matchesAny(text, keywords) {
        var t = (text || '').toLowerCase().trim();
        if (!t) return false;
        for (var i = 0; i < keywords.length; i++) {
            if (t === keywords[i] || t.indexOf(keywords[i]) !== -1) return true;
        }
        return false;
    }

    var tries = 0;
    var iv = setInterval(function() {
        var done = false;

        // --- 1. <select> natif (Anki legacy) ---
        var selects = Array.from(document.querySelectorAll('select'));
        outer: for (var si = 0; si < selects.length; si++) {
            var sel = selects[si];
            for (var oi = 0; oi < sel.options.length; oi++) {
                if (matchesAny(sel.options[oi].text, UPDATE_KW)) {
                    if (sel.selectedIndex !== oi) {
                        sel.selectedIndex = oi;
                        sel.dispatchEvent(new Event('change', {bubbles: true}));
                        console.log('[anki-diff] <select> mode set to UPDATE');
                    }
                    done = true;
                    break outer;
                }
            }
        }

        // --- 2. Combobox Svelte [role="combobox"] (Anki >= 2.1.55) ---
        if (!done) {
            var combos = Array.from(document.querySelectorAll('[role="combobox"]'));
            var target = null;
            for (var ci = 0; ci < combos.length; ci++) {
                // Le texte visible est dans .label (classe Svelte interne)
                var lbl = combos[ci].querySelector('.label');
                var txt = lbl ? lbl.textContent : combos[ci].textContent;
                if (matchesAny(txt, DUPE_KW)) {
                    target = combos[ci];
                    break;
                }
            }
            if (target) {
                done = true;
                clearInterval(iv);
                console.log('[anki-diff] Svelte combobox found:', target.textContent.trim().slice(0, 40));

                // Récupérer l'id du popover via aria-controls
                var popoverId = target.getAttribute('aria-controls');
                console.log('[anki-diff] popover id:', popoverId);

                // Ouvrir le combobox
                target.focus();
                ['pointerdown', 'mousedown', 'pointerup', 'mouseup', 'click'].forEach(function(evType) {
                    var ev = new MouseEvent(evType, {
                        bubbles: true,
                        cancelable: true,
                        view: window
                    });
                    target.dispatchEvent(ev);
                });

                // Attendre que le popover soit visible, puis cliquer sur UPDATE
                var pop = 0;
                var popIv = setInterval(function() {
                    // Chercher d'abord dans le popover ciblé par aria-controls
                    var searchRoot = popoverId ? document.getElementById(popoverId) : null;
                    var candidates = searchRoot
                        ? Array.from(searchRoot.querySelectorAll('*'))
                        : Array.from(document.querySelectorAll('*'));

                    var upEl = null;
                    for (var i = 0; i < candidates.length; i++) {
                        var el = candidates[i];
                        // Ignorer les conteneurs avec beaucoup d'enfants
                        if (el.children.length > 5) continue;
                        // Ne prendre que le texte du nœud lui-même (pas ses descendants)
                        var ownText = '';
                        for (var n = 0; n < el.childNodes.length; n++) {
                            if (el.childNodes[n].nodeType === 3) { // TEXT_NODE
                                ownText += el.childNodes[n].textContent;
                            }
                        }
                        if (!ownText) ownText = el.textContent || '';
                        ownText = ownText.trim();
                        if (ownText.length > 0 && ownText.length < 80 && matchesAny(ownText, UPDATE_KW)) {
                            upEl = el;
                            break;
                        }
                    }

                    if (upEl) {
                        console.log('[anki-diff] clicking UPDATE option:', upEl.textContent.trim());
                        
                        // Remonter vers l'ancêtre avec role="option"
                        var opt = upEl;
                        while (opt && opt.getAttribute('role') !== 'option') {
                            opt = opt.parentElement;
                        }
                        var clickTarget = opt || upEl;
                        
                        // Simuler la séquence de clic sur upEl et clickTarget
                        [upEl, clickTarget].forEach(function(targetEl) {
                            if (!targetEl) return;
                            ['pointerdown', 'mousedown', 'pointerup', 'mouseup', 'click'].forEach(function(evType) {
                                var ev = new MouseEvent(evType, {
                                    bubbles: true,
                                    cancelable: true,
                                    view: window
                                });
                                targetEl.dispatchEvent(ev);
                            });
                        });
                        
                        clearInterval(popIv);
                    } else if (pop++ > 40) {
                        console.log('[anki-diff] UPDATE option not found after waiting');
                        clearInterval(popIv);
                    }
                }, 100);
            }
        }

        if (done || tries++ >= MAX_TRIES) clearInterval(iv);
    }, INTERVAL);
})();
"""
    try:
        from PyQt6 import QtCore as _QtCore
    except ImportError:
        from PySide6 import QtCore as _QtCore

    def _inject():
        try:
            web.eval(js)
        except Exception:
            pass

    _QtCore.QTimer.singleShot(800, _inject)


def _force_update_mode_qt(dialog: Any) -> None:
    """
    Anki legacy : widgets Qt natifs (QComboBox ou QRadioButton).
    """
    try:
        from PyQt6 import QtWidgets
    except ImportError:
        from PySide6 import QtWidgets

    # QComboBox : chercher par nom d'attribut connu, puis par texte d'options
    for attr_name in ("existingNotes", "existingNotesMode", "updateMode", "importMode"):
        combo = getattr(dialog, attr_name, None)
        if combo is None:
            combo = dialog.findChild(QtWidgets.QComboBox, attr_name)
        if combo is not None and isinstance(combo, QtWidgets.QComboBox):
            for i in range(combo.count()):
                t = combo.itemText(i).lower()
                if "mettr" in t or "updat" in t:
                    combo.setCurrentIndex(i)
                    return

    # Fallback QComboBox générique
    for combo in dialog.findChildren(QtWidgets.QComboBox):
        for i in range(combo.count()):
            t = combo.itemText(i).lower()
            if "mettr" in t or "updat" in t:
                combo.setCurrentIndex(i)
                return

    # QRadioButton (versions très anciennes)
    for radio in dialog.findChildren(QtWidgets.QRadioButton):
        t = radio.text().lower()
        if "mettr" in t or "updat" in t:
            radio.setChecked(True)
            return

    return


def commit_changes(accepted_diffs: List[NoteDiff]) -> None:
    flush_pending_history_snapshots()
    mw = aqt.mw
    col = mw.col

    # ── Snapshot AVANT commit (état original) ────────────────────────────
    try:
        from .history import NoteHistoryDB
        _hist_db = NoteHistoryDB.instance()
        for diff in accepted_diffs:
            if diff.excluded:
                continue
            if diff.original_fields:
                _hist_db.store_snapshot(
                    diff.note_id,
                    diff.original_fields,
                    source=f"import_{diff.action.lower()}",
                    comment=f"Avant import — {diff.action}",
                    tags=diff.original_tags if diff.original_tags else diff.tags,
                )
    except Exception:
        pass

    # ── Appliquer les modifications note par note ─────────────────────────
    # IMPORTANT: Ne pas envelopper dans col.db.transact() — les méthodes
    # Collection (add_note, update_note, remove_notes) gèrent chacune leur
    # propre transaction via le backend Rust. Un transact() externe crée une
    # transaction SQL brute qui entre en conflit avec le backend et provoque
    # un rollback silencieux (les données semblent sauvegardées mais ne le
    # sont pas réellement).
    updated_count = 0
    inserted_count = 0
    deleted_count = 0
    errors = []

    for diff in accepted_diffs:
        if diff.excluded:
            if diff.action == "INSERT" and diff.staged_note_id:
                try:
                    col.remove_notes([diff.staged_note_id])
                except Exception:
                    pass
            continue
        try:
            if diff.action == "UPDATE":
                try:
                    note = col.get_note(diff.note_id)
                except Exception as e:
                    print(f"[anki-diff] commit UPDATE: note {diff.note_id} not found: {e}")
                    errors.append(f"Note {diff.note_id} introuvable")
                    continue

                model = col.models.get(note.mid)
                if not model:
                    print(f"[anki-diff] commit UPDATE: model not found for note {diff.note_id}")
                    continue
                field_indices = {f["name"]: idx for idx, f in enumerate(model.get("flds", []))}

                changed = False
                for name, merged_val in diff.merged_fields.items():
                    idx = field_indices.get(name)
                    if idx is not None and idx < len(note.fields):
                        orig_val = diff.original_fields.get(name)
                        if merged_val != orig_val:
                            note.fields[idx] = sanitize_surrogates(merged_val)
                            changed = True
                note.tags = [sanitize_surrogates(t) for t in diff.tags]
                col.update_note(note)
                updated_count += 1
                print(f"[anki-diff] commit UPDATE OK: note {diff.note_id} (changed={changed})")

            elif diff.action == "INSERT":
                if diff.staged_note_id:
                    # La note a été pré-insérée (staged) lors de l'ouverture du browser.
                    # On met simplement à jour ses champs avec les merged_fields.
                    try:
                        note = col.get_note(diff.staged_note_id)
                        model = col.models.get(note.mid)
                        if model:
                            field_indices = {f["name"]: idx for idx, f in enumerate(model.get("flds", []))}
                            for fname, val in diff.merged_fields.items():
                                fidx = field_indices.get(fname)
                                if fidx is not None and fidx < len(note.fields):
                                    if not val and fname not in diff.custom_edit_fields:
                                        val = diff.updated_fields.get(fname, "")
                                    note.fields[fidx] = sanitize_surrogates(val)
                            tags_to_apply = diff.tags if diff.tags else diff.updated_tags
                            note.tags = [sanitize_surrogates(t) for t in tags_to_apply]
                            col.update_note(note)
                        inserted_count += 1
                        print(f"[anki-diff] commit INSERT (staged) OK: staged_id={diff.staged_note_id}")
                    except Exception as e:
                        print(f"[anki-diff] commit INSERT (staged) ERROR: {e}")
                        errors.append(f"Note staged {diff.staged_note_id}: {e}")
                else:
                    # Insertion classique (pas de staging préalable)
                    fields = []
                    for name in diff.field_names:
                        val = diff.merged_fields.get(name, "")
                        if not val and name not in diff.custom_edit_fields:
                            val = diff.updated_fields.get(name, "")
                        fields.append(sanitize_surrogates(val))
                    tags_to_apply = diff.tags if diff.tags else diff.updated_tags
                    tags = [sanitize_surrogates(t) for t in tags_to_apply]

                    # Chercher le modèle : 1) par model_id (fiable), 2) par nom, 3) modèle courant
                    model = None
                    if diff.model_id and hasattr(col, "models"):
                        try:
                            model = col.models.get(diff.model_id)
                        except Exception:
                            pass
                    if not model and hasattr(col, "models") and hasattr(col.models, "by_name"):
                        model = col.models.by_name(diff.model_name)
                    if not model and hasattr(col, "models") and hasattr(col.models, "current"):
                        try:
                            model = col.models.current()
                        except Exception:
                            pass
                    if not model:
                        print(f"[anki-diff] commit INSERT: model '{diff.model_name}' (id={diff.model_id}) not found")
                        errors.append(f"Modèle '{diff.model_name}' introuvable")
                        continue

                    note = col.new_note(model)
                    if diff.note_id and diff.note_id > 0:
                        note.id = diff.note_id
                    for idx, val in enumerate(fields):
                        if idx < len(note.fields):
                            note.fields[idx] = val
                    note.tags = tags

                    deck_id = diff.deck_id or 1
                    col.add_note(note, deck_id)
                    inserted_count += 1
                    print(f"[anki-diff] commit INSERT OK: note {diff.note_id} → new id {note.id} in deck {deck_id}")

            elif diff.action == "DELETE":
                col.remove_notes([diff.note_id])
                deleted_count += 1
                print(f"[anki-diff] commit DELETE OK: note {diff.note_id}")

        except Exception as e:
            import traceback
            print(f"[anki-diff] commit ERROR on note {diff.note_id} ({diff.action}): {e}")
            traceback.print_exc()
            errors.append(f"Note {diff.note_id}: {e}")

    print(f"[anki-diff] commit_changes done: {updated_count} updated, {inserted_count} inserted, {deleted_count} deleted, {len(errors)} errors")

    # ── Snapshot APRÈS commit (état mergé) ───────────────────────────────
    try:
        from .history import NoteHistoryDB
        _hist_db = NoteHistoryDB.instance()
        for diff in accepted_diffs:
            if diff.excluded:
                continue
            if diff.merged_fields:
                _hist_db.store_snapshot(
                    diff.note_id,
                    diff.merged_fields,
                    source=f"import_{diff.action.lower()}",
                    comment=f"Après import — {diff.action}",
                    tags=diff.tags,
                )
    except Exception:
        pass

    # Afficher le message d'importation réussie
    try:
        msg_parts = []
        if updated_count:
            msg_parts.append(f"{updated_count} note(s) mise(s) à jour")
        if inserted_count:
            msg_parts.append(f"{inserted_count} note(s) ajoutée(s)")
        if deleted_count:
            msg_parts.append(f"{deleted_count} note(s) supprimée(s)")
        if errors:
            msg_parts.append(f"{len(errors)} erreur(s)")

        if msg_parts:
            aqt.utils.tooltip("Importation terminée : " + ", ".join(msg_parts) + ".")
        else:
            aqt.utils.tooltip("Importation terminée (aucune modification).")

        if errors:
            print(f"[anki-diff] Errors during commit: {errors}")
    except Exception as e:
        print(f"[anki-diff] Error showing import completion tooltip: {e}")

def intercept_import(import_fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    if not is_visual_diff_enabled():
        return import_fn(*args, **kwargs)
        
    diffs = capture_import_changes(import_fn, *args, **kwargs)
    if not diffs:
        return None
        
    accepted_diffs = show_diff_dialog(diffs)
    if accepted_diffs is None:
        return None
        
    commit_changes(accepted_diffs)
    return len(accepted_diffs)


def wrap_import_handler(original_handler: Callable[..., Any]) -> Callable[..., Any]:
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        if not is_visual_diff_enabled():
            return original_handler(*args, **kwargs)

        import flask
        import io

        # Sauvegarder les données originales de la requête
        original_data = flask.request.get_data()

        # ── Construire les bytes patchés (mode UPDATE forcé) ─────────────
        patched_data = original_data
        try:
            from anki.import_export_pb2 import ImportCsvRequest, ImportAnkiPackageRequest
            path_lower = flask.request.path.lower()
            print(f"[anki-diff] Intercepted import request path: {flask.request.path}")

            if "importcsv" in path_lower or "import_csv" in path_lower:
                req = ImportCsvRequest()
                req.ParseFromString(original_data)
                print(f"[anki-diff] CSV import request, original dupe_resolution: {req.metadata.dupe_resolution}")
                if req.HasField("metadata"):
                    req.metadata.dupe_resolution = 0  # UPDATE
                    patched_data = req.SerializeToString()
                    print(f"[anki-diff] CSV import request patched to UPDATE mode")
            elif "importankipackage" in path_lower or "import_anki_package" in path_lower:
                req = ImportAnkiPackageRequest()
                req.ParseFromString(original_data)
                print(f"[anki-diff] APKG import request, original update_notes: {req.options.update_notes}")
                req.options.update_notes = 1  # ALWAYS
                patched_data = req.SerializeToString()
                print(f"[anki-diff] APKG import request patched to ALWAYS mode")
        except Exception as e:
            print(f"[anki-diff] protobuf patch error (non-fatal): {e}")

        def _set_request_data(data: bytes) -> None:
            """Remplace les données de la requête Flask courante en contournant le proxy."""
            req = flask.request
            if hasattr(req, "_get_current_object"):
                req = req._get_current_object()
            req._cached_data = data
            req._cached_stream = io.BytesIO(data)
            req.environ["wsgi.input"] = io.BytesIO(data)
            req.environ["CONTENT_LENGTH"] = str(len(data))

        # ── Dry-run avec données patchées (mode UPDATE) ──────────────────
        import_output = None

        def import_fn_wrapper():
            nonlocal import_output
            _set_request_data(patched_data)
            import_output = original_handler(*args, **kwargs)

        # Temporarily mock on_op_finished to prevent UI reset during dry run
        import aqt.mediasrv as _mediasrv
        original_on_op_finished = _mediasrv.on_op_finished
        _mediasrv.on_op_finished = lambda *a, **kw: None

        try:
            diffs = capture_import_changes(import_fn_wrapper)
        finally:
            _mediasrv.on_op_finished = original_on_op_finished
            # Restaurer les données originales pour l'import final
            _set_request_data(original_data)

        if not diffs:
            print("[anki-diff] No differences detected during dry-run import.")
            # Aucun diff → lancer l'import normalement avec données originales
            _set_request_data(original_data)
            return original_handler(*args, **kwargs)

        print(f"[anki-diff] Detected {len(diffs)} differences during dry-run import. Opening browser diff panel.")

        event = threading.Event()
        accepted_diffs = None

        def show_dialog_on_main():
            nonlocal accepted_diffs
            try:
                # Mode browser natif (défaut) : ouvrir l'explorateur Anki
                # avec le dock DiffBrowserPanel. Le commit est géré de façon
                # asynchrone via le bouton "Commit Changes" du dock.
                def _on_native_commit(acc):
                    try:
                        commit_changes(acc)
                        try:
                            from aqt.gui_hooks import state_did_reset
                            state_did_reset()
                        except Exception:
                            pass
                    except Exception as ex:
                        print("[anki_import_diff] native commit error:", ex)

                close_active_import_dialogs()
                open_browser_diff(diffs, _on_native_commit)
                # Le workflow est non-bloquant — signaler immédiatement
                accepted_diffs = []  # non-None = pas annulé
            except Exception as ex:
                print("Error showing diff dialog:", ex)
            finally:
                event.set()

        aqt.mw.taskman.run_on_main(show_dialog_on_main)
        event.wait(300.0)

        if accepted_diffs is None:
            # Cancelled
            import flask
            import json
            try:
                is_json = flask.request and (flask.request.is_json or "application/json" in flask.request.headers.get("Accept", ""))
            except Exception:
                is_json = False
            if is_json:
                return json.dumps({"success": False, "cancelled": True}).encode("utf-8")
            else:
                from anki.collection import OpChangesOnly
                return OpChangesOnly().SerializeToString()

        return import_output

    return wrapper


def patch_collection_updates() -> None:
    """
    Monkey-patches Collection.update_note and Collection.update_notes to automatically
    capture note history snapshots before any update is applied.
    This guarantees that modifications from imports (AnkiCollab, CSV, APKG), other addons,
    or manual editing are captured in the local history.
    """
    try:
        import aqt
        from anki.collection import Collection
    except ImportError:
        return

    # Avoid double patching
    if hasattr(Collection, "_original_update_note"):
        return

    Collection._original_update_note = Collection.update_note
    Collection._original_update_notes = Collection.update_notes
    _in_update = threading.local()

    def patched_update_note(self, note, *args, **kwargs):
        # Ignorer les mises à jour transitoires de "Lier les cartes" (marqueur de curseur temporaire)
        # Ce marqueur est inséré dans le DOM JS pour localiser le curseur pendant le collage de NID,
        # et peut être sauvegardé prématurément avant que le vrai badge soit inséré.
        try:
            if note and hasattr(note, "fields") and any(
                "edn-cursor-marker" in (f or "") for f in note.fields
            ):
                return Collection._original_update_note(self, note, *args, **kwargs)
        except Exception:
            pass

        if getattr(_in_update, "active", False):
            return Collection._original_update_note(self, note, *args, **kwargs)
        _in_update.active = True
        try:
            _save_history_before_bulk(self, [note])
        except Exception as e:
            print(f"[anki-diff] Error in patched_update_note: {e}")
        finally:
            _in_update.active = False
        return Collection._original_update_note(self, note, *args, **kwargs)

    def patched_update_notes(self, notes, *args, **kwargs):
        # Ignorer les mises à jour transitoires de "Lier les cartes" (marqueur de curseur temporaire)
        try:
            notes_list_check = list(notes)
            if any(
                hasattr(n, "fields") and any("edn-cursor-marker" in (f or "") for f in n.fields)
                for n in notes_list_check
            ):
                return Collection._original_update_notes(self, notes_list_check, *args, **kwargs)
            notes = notes_list_check  # réutiliser la liste déjà consommée
        except Exception:
            pass

        if getattr(_in_update, "active", False):
            return Collection._original_update_notes(self, notes, *args, **kwargs)
        _in_update.active = True
        try:
            _save_history_before_bulk(self, notes)
        except Exception as e:
            print(f"[anki-diff] Error in patched_update_notes: {e}")
        finally:
            _in_update.active = False
        return Collection._original_update_notes(self, notes, *args, **kwargs)

    Collection.update_note = patched_update_note
    Collection.update_notes = patched_update_notes
    print("[anki-diff] Collection.update_note and Collection.update_notes monkey-patched successfully")




def flush_pending_history_snapshots() -> None:
    """
    Vide et persiste immédiatement tous les snapshots de notes en attente de debounce.
    Appelé automatiquement lors de la fermeture du profil Anki ou avant un commit d'import.
    """
    global _debounce_timers
    if not _debounce_timers:
        try:
            from .history import NoteHistoryDB
            NoteHistoryDB.instance().flush(timeout=2.0)
        except Exception:
            pass
        return
    try:
        from .history import NoteHistoryDB
        db = NoteHistoryDB.instance()
    except Exception:
        db = None

    timers_to_flush = list(_debounce_timers.items())
    _debounce_timers.clear()

    for note_id, entry in timers_to_flush:
        if isinstance(entry, tuple) and len(entry) == 3:
            timer, flds, tgs = entry
            try:
                timer.cancel()
            except Exception:
                pass
            if db:
                try:
                    db.store_snapshot_async(
                        note_id=note_id,
                        fields=flds,
                        source="manual",
                        comment="Sauvegarde automatique (modification)",
                        tags=tgs,
                    )
                except Exception:
                    pass

    if db:
        try:
            db.flush(timeout=3.0)
        except Exception:
            pass


def _save_history_before_bulk(col, notes) -> None:
    """
    Sauvegarde l'état des notes AVANT leur modification (pré-update snapshot).
    - Inspection ultra-rapide de la call stack (0ms, pas de traceback.extract_stack)
    - Lecture SQL rapide col.db.first sur le thread principal
    - Écriture asynchrone non-bloquante via le worker NoteHistoryDB (0 freeze UI)

    DEBOUNCE : lors de l'édition interactive (frappe clavier, une note à la fois,
    source "manual"), la capture de snapshot est différée de _DEBOUNCE_HISTORY_MS ms.
    L'écriture Anki native reste TOUJOURS immédiate — seul le snapshot est différé.
    Si plusieurs frappes se succèdent rapidement, l'état INITIAL (avant le début de
    la frappe) est conservé et persisté une seule fois, éliminant les alertes de debounce.

    Le debounce est DÉSACTIVÉ dans les cas suivants (capture immédiate) :
      - Contexte AnkiCollab / CrowdAnki (is_collab)
      - _pending_diffs actif (diff import en cours de revue)
      - Appel batch (len(notes) > 1)
      - Stack contenant anki_import_diff (import diff lui-même écrit)
    """
    import sys

    try:
        from .history import NoteHistoryDB
    except Exception:
        return

    # ── Détection ultra-rapide du contexte ─────────────────────────────────
    is_collab = False
    is_self_write = False  # L'addon anki_import_diff écrit lui-même
    try:
        f = sys._getframe(1)
        for _ in range(10):
            if f is None:
                break
            fn = getattr(f.f_code, "co_filename", "").lower()
            if "1957538407" in fn or "crowd_anki" in fn or "ankicollab" in fn:
                is_collab = True
                break
            if "anki_import_diff" in fn:
                is_self_write = True
                break
            f = f.f_back
    except Exception:
        pass

    source = "ankicollab" if is_collab else "manual"
    comment = "Mise à jour via AnkiCollab" if is_collab else "Sauvegarde automatique (modification)"

    # Garde-fou révision : en session de révision active, les cartes sont révisées mais les
    # champs des notes ne sont pas édités par l'utilisateur. Éviter d'interroger la DB inutilement.
    if not is_collab and not _pending_diffs and aqt.mw and getattr(aqt.mw, "state", None) == "review":
        return

    # ── Garde-fous diff actif ───────────────────────────────────────────────
    try:
        from .browser_integration import _pending_diffs, _committed
    except Exception:
        _pending_diffs, _committed = {}, False

    # ── Décider si on peut appliquer le debounce ────────────────────────────
    # Reconstruire notes en liste (itérable consommable plusieurs fois)
    notes_list = list(notes)

    # Debounce uniquement pour : écriture manuelle unitaire, hors import diff
    use_debounce = (
        source == "manual"
        and not is_self_write
        and not is_collab
        and len(notes_list) == 1  # appel unitaire (frappe clavier)
    )

    db = NoteHistoryDB.instance()

    for note in notes_list:
        if not note or not getattr(note, "id", None):
            continue
        # Ne pas enregistrer de faux snapshot si la note est en cours de revue de diff
        if not _committed and _pending_diffs and note.id in _pending_diffs:
            continue

        note_id = note.id

        if use_debounce:
            existing_info = _debounce_timers.get(note_id)
            if existing_info is not None and isinstance(existing_info, tuple):
                existing_timer, initial_fields, initial_tags = existing_info
                try:
                    existing_timer.cancel()
                except Exception:
                    pass

                def _rearm_flush(nid=note_id, flds=initial_fields,
                                 tgs=initial_tags, src=source,
                                 cmt=comment):
                    _debounce_timers.pop(nid, None)
                    try:
                        db.store_snapshot_async(
                            note_id=nid,
                            fields=flds,
                            source=src,
                            comment=cmt,
                            tags=tgs,
                        )
                    except Exception:
                        pass

                t = _DebounceTimer(_DEBOUNCE_HISTORY_MS, _rearm_flush)
                _debounce_timers[note_id] = (t, initial_fields, initial_tags)
                continue

        # Lire l'état actuel EN AVANCE (sur le thread principal, avant le commit)
        try:
            row = col.db.first(
                "SELECT flds, tags FROM notes WHERE id = ?", note_id
            )
            if not row:
                continue
            flds_str, tags_str = row
            new_fields = dict(note.items())
            field_names = list(new_fields.keys())
            field_values = flds_str.split("\x1f")
            if len(field_values) != len(field_names):
                continue
            old_fields = dict(zip(field_names, field_values))
            if old_fields == new_fields:
                continue  # aucun changement réel
            old_tags = [t for t in tags_str.strip().split(" ") if t]
        except Exception:
            continue

        if use_debounce:
            # ── Debounce interactif ─────────────────────────────────────────
            captured_fields = dict(old_fields)
            captured_tags = list(old_tags)
            captured_source = source
            captured_comment = comment
            captured_note_id = note_id

            def _flush(nid=captured_note_id, flds=captured_fields,
                       tgs=captured_tags, src=captured_source,
                       cmt=captured_comment):
                _debounce_timers.pop(nid, None)
                try:
                    db.store_snapshot_async(
                        note_id=nid,
                        fields=flds,
                        source=src,
                        comment=cmt,
                        tags=tgs,
                    )
                except Exception:
                    pass

            t = _DebounceTimer(_DEBOUNCE_HISTORY_MS, _flush)
            _debounce_timers[note_id] = (t, captured_fields, captured_tags)
        else:
            # ── Écriture immédiate (import, collab, batch, diff actif) ──────
            try:
                db.store_snapshot_async(
                    note_id=note_id,
                    fields=old_fields,
                    source=source,
                    comment=comment,
                    tags=old_tags,
                )
            except Exception:
                pass

