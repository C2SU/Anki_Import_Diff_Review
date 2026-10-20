"""
editor_sync.py — Synchronisation bidirectionnelle entre l'éditeur natif Anki et les hunks de diff.

Deux responsabilités :
  1. sync_editor_from_hunks(editor, diff) — Injecte les valeurs mergées (selon les hunks acceptés)
     dans les champs de l'éditeur natif, via JavaScript, sans rechargement complet de la note.
  2. hook editor_did_unfocus_field — Capture les modifications manuelles de l'utilisateur et les
     stocke dans diff.merged_fields, en marquant le champ comme "custom edited".
"""
from __future__ import annotations

import json
import html as _html
from typing import TYPE_CHECKING, Optional

# Mettre à True pour activer les logs verbeux de sync (un message par champ et par callback JS)
_DEBUG_SYNC = False

if TYPE_CHECKING:
    from .savepoint import NoteDiff

try:
    from aqt.editor import Editor  # type: ignore
except ImportError:
    Editor = None  # tests sans Anki

try:
    import aqt
except ImportError:
    aqt = None


def _get_editor_note(editor):
    """Extrait la note de l'editeur de facon securisee (Editor ou NewEditor)."""
    if not editor:
        return None
    note = getattr(editor, "note", None)
    if note is not None:
        return note
    nid = getattr(editor, "nid", None)
    if nid and aqt and getattr(aqt, "mw", None) and aqt.mw.col:
        try:
            return aqt.mw.col.get_note(nid)
        except Exception:
            pass
    return None

# ---------------------------------------------------------------------------
# JS template — mise à jour d'un champ sans rechargement complet
# ---------------------------------------------------------------------------

_SET_FIELD_JS = """
(function(fieldIdx, htmlValue) {
    try {
        // Tentative 1 : NoteEditor moderne (Anki >= 2.1.55)
        try {
            var ni = require("anki/NoteEditor").instances;
            if (ni && ni[0]) {
                var fields = ni[0].fields;
                if (fields && fields[fieldIdx]) {
                    var area = fields[fieldIdx].editingArea;
                    if (area) {
                        if (area.content && typeof area.content.set === 'function') {
                            area.content.set(htmlValue);
                            return 'ok:NoteEditor.content';
                        } else if (typeof area.content !== 'undefined') {
                            area.content = htmlValue;
                            return 'ok:NoteEditor.content';
                        }
                        var ce = area.element || area.$el;
                        if (ce) {
                            ce.innerHTML = htmlValue;
                            ce.dispatchEvent(new Event('input', { bubbles: true }));
                            return 'ok:NoteEditor.element';
                        }
                    }
                }
            }
        } catch(e) { console.log('anki-diff NoteEditor err:', e); }

        // Tentative 2 : anki-editable (Svelte Web Components)
        try {
            var editables = document.querySelectorAll('anki-editable');
            if (editables && editables.length > fieldIdx) {
                var target = editables[fieldIdx];
                var sr = target.shadowRoot;
                var editableEl = sr ? (sr.querySelector('[contenteditable="true"]') || sr.firstElementChild) : target;
                if (editableEl) {
                    editableEl.innerHTML = htmlValue;
                    editableEl.dispatchEvent(new Event('input', { bubbles: true, composed: true }));
                    target.dispatchEvent(new Event('input', { bubbles: true, composed: true }));
                    return 'ok:anki-editable';
                }
            }
        } catch(e) {}

        // Tentative 3 : rich-text-editable (Anki 2.1.50-2.1.54)
        try {
            var rte = document.querySelectorAll('.rich-text-editable');
            if (rte.length > fieldIdx) {
                rte[fieldIdx].innerHTML = htmlValue;
                rte[fieldIdx].dispatchEvent(new Event('input', { bubbles: true }));
                return 'ok:rich-text-editable';
            }
        } catch(e) {}

        // Tentative 4 : tout div[contenteditable] dans les champs
        try {
            var ces = document.querySelectorAll('[contenteditable="true"]');
            var fieldEls = Array.from(ces).filter(function(el) {
                return el.tagName !== 'INPUT' && el.tagName !== 'TEXTAREA';
            });
            if (fieldEls.length > fieldIdx) {
                fieldEls[fieldIdx].innerHTML = htmlValue;
                fieldEls[fieldIdx].dispatchEvent(new Event('input', { bubbles: true }));
                return 'ok:contenteditable-fallback';
            }
        } catch(e) {}

        return 'error:not_found';
    } catch(err) {
        return 'error:' + err;
    }
})(%d, %s);
"""

_SET_FIELDS_BATCH_JS = """
(function(fieldsMap) {
    var results = {};

    function updateSingleField(fieldIdx, htmlValue) {
        // Tentative 1 : NoteEditor moderne (Anki >= 2.1.55)
        try {
            var ni = require("anki/NoteEditor").instances;
            if (ni && ni[0] && ni[0].fields && ni[0].fields[fieldIdx]) {
                var area = ni[0].fields[fieldIdx].editingArea;
                if (area) {
                    if (area.content && typeof area.content.set === 'function') {
                        area.content.set(htmlValue);
                        return 'ok:NoteEditor.content';
                    } else if (typeof area.content !== 'undefined') {
                        area.content = htmlValue;
                        return 'ok:NoteEditor.content';
                    }
                    var ce = area.element || area.$el;
                    if (ce) {
                        ce.innerHTML = htmlValue;
                        ce.dispatchEvent(new Event('input', { bubbles: true }));
                        return 'ok:NoteEditor.element';
                    }
                }
            }
        } catch(e) {}

        // Tentative 2 : anki-editable (Svelte Web Components)
        try {
            var editables = document.querySelectorAll('anki-editable');
            if (editables && editables.length > fieldIdx) {
                var target = editables[fieldIdx];
                var sr = target.shadowRoot;
                var editableEl = (sr ? sr.querySelector('[contenteditable="true"]') : null)
                              || target.querySelector('[contenteditable="true"]')
                              || (sr ? sr.firstElementChild : null)
                              || target;
                if (editableEl) {
                    editableEl.innerHTML = htmlValue;
                    editableEl.dispatchEvent(new Event('input', { bubbles: true, composed: true }));
                    target.dispatchEvent(new Event('input', { bubbles: true, composed: true }));
                    return 'ok:anki-editable';
                }
            }
        } catch(e) {}

        // Tentative 3 : rich-text-editable (Anki 2.1.50-2.1.54)
        try {
            var rte = document.querySelectorAll('.rich-text-editable');
            if (rte && rte.length > fieldIdx) {
                rte[fieldIdx].innerHTML = htmlValue;
                rte[fieldIdx].dispatchEvent(new Event('input', { bubbles: true }));
                return 'ok:rich-text-editable';
            }
        } catch(e) {}

        // Tentative 4 : tout div[contenteditable] dans les champs
        try {
            var ces = Array.from(document.querySelectorAll('[contenteditable="true"]')).filter(function(el) {
                return el.tagName !== 'INPUT' && el.tagName !== 'TEXTAREA';
            });
            if (ces.length > fieldIdx) {
                ces[fieldIdx].innerHTML = htmlValue;
                ces[fieldIdx].dispatchEvent(new Event('input', { bubbles: true }));
                return 'ok:contenteditable-fallback';
            }
        } catch(e) {}

        return 'error:not_found';
    }

    for (var idxStr in fieldsMap) {
        if (!Object.prototype.hasOwnProperty.call(fieldsMap, idxStr)) continue;
        var fieldIdx = parseInt(idxStr, 10);
        var htmlValue = fieldsMap[idxStr];
        results[fieldIdx] = updateSingleField(fieldIdx, htmlValue);
    }

    return results;
})(%s);
"""

_HIGHLIGHT_MODIFIED_FIELDS_JS = """
(function(modifiedIndices) {
    function getFieldContainers() {
        var aes = document.querySelectorAll('anki-editable');
        if (aes && aes.length > 0) {
            return Array.from(aes);
        }
        var richFields = document.querySelectorAll('.field .rich-text-editable');
        if (richFields && richFields.length > 0) {
            return Array.from(richFields);
        }
        return Array.from(document.querySelectorAll('.field, [contenteditable="true"]'));
    }

    function apply() {
        var fields = getFieldContainers();
        fields.forEach(function(el, idx) {
            el.style.outline = '';
            el.style.background = '';
            var container = el.closest ? (el.closest('.field') || el.parentElement) : el.parentElement;
            if (container) {
                var badge = container.querySelector('.anki-diff-badge');
                if (badge) badge.remove();
                container.style.position = '';
            }

            if (modifiedIndices.indexOf(idx) === -1) return;

            el.style.outline = '2px solid #ffc107';
            el.style.background = 'rgba(255,193,7,0.06)';
            el.style.transition = 'background 0.3s, outline 0.3s';

            if (container) {
                container.style.position = 'relative';
                var badge = document.createElement('div');
                badge.className = 'anki-diff-badge';
                badge.textContent = 'DIFF';
                badge.style.cssText = [
                    'position:absolute', 'top:4px', 'right:8px',
                    'font-size:9px', 'font-weight:700',
                    'padding:1px 6px', 'border-radius:8px',
                    'pointer-events:none', 'z-index:99',
                    'font-family:monospace', 'letter-spacing:0.5px',
                    'background:#ffc107', 'color:#000'
                ].join(';');
                container.appendChild(badge);
            }
        });
    }

    if (window._anki_diff_highlight_iv) {
        clearInterval(window._anki_diff_highlight_iv);
        window._anki_diff_highlight_iv = null;
    }

    var ok = (document.querySelectorAll('anki-editable').length > 0) || (document.querySelectorAll('.field').length > 0);
    if (ok) {
        apply();
    } else {
        var tries = 0;
        window._anki_diff_highlight_iv = setInterval(function() {
            var ready = (document.querySelectorAll('anki-editable').length > 0) || (document.querySelectorAll('.field').length > 0);
            if (ready || tries++ > 15) {
                clearInterval(window._anki_diff_highlight_iv);
                window._anki_diff_highlight_iv = null;
                if (ready) apply();
            }
        }, 50);
    }
})(%s);
"""

_CLEAR_FIELD_HIGHLIGHTS_JS = """
(function() {
    var els = document.querySelectorAll('anki-editable, .field .rich-text-editable, [contenteditable="true"]');
    els.forEach(function(el) {
        el.style.outline = '';
        el.style.background = '';
        var container = el.closest ? (el.closest('.field') || el.parentElement) : el.parentElement;
        if (container) {
            var badge = container.querySelector('.anki-diff-badge');
            if (badge) badge.remove();
        }
    });
})();
"""

_ACCEPT_FIELD_JS = """
(function(fieldIdx) {
    var els = document.querySelectorAll('anki-editable');
    if (!els || els.length === 0) {
        els = document.querySelectorAll('.field .rich-text-editable');
    }
    if (els && els[fieldIdx]) {
        var el = els[fieldIdx];
        el.style.outline = '2px solid #28a745';
        el.style.background = 'rgba(40,167,69,0.08)';
        var container = el.closest ? (el.closest('.field') || el.parentElement) : el.parentElement;
        if (container) {
            var badge = container.querySelector('.anki-diff-badge');
            if (badge) {
                badge.textContent = 'ACCEPTED';
                badge.style.background = '#28a745';
                badge.style.color = '#fff';
            }
        }
    }
})(%d);
"""

_REJECT_FIELD_JS = """
(function(fieldIdx) {
    var els = document.querySelectorAll('anki-editable');
    if (!els || els.length === 0) {
        els = document.querySelectorAll('.field .rich-text-editable');
    }
    if (els && els[fieldIdx]) {
        var el = els[fieldIdx];
        el.style.outline = '2px solid #dc3545';
        el.style.background = 'rgba(220,53,69,0.06)';
        var container = el.closest ? (el.closest('.field') || el.parentElement) : el.parentElement;
        if (container) {
            var badge = container.querySelector('.anki-diff-badge');
            if (badge) {
                badge.textContent = 'REJECTED';
                badge.style.background = '#dc3545';
                badge.style.color = '#fff';
            }
        }
    }
})(%d);
"""


# ---------------------------------------------------------------------------
# Synchronisation hunks → éditeur
# ---------------------------------------------------------------------------

def sync_tags_to_editor(editor, tags: list[str]) -> None:
    """
    Synchronise les tags avec l'editeur Anki de facon exhaustive :
      1. Met a jour note.tags en memoire (ed_note et editor.note)
      2. Met a jour le widget Qt TagEdit (editor.tags.setText)
      3. Appelle editor.updateTags() si disponible
      4. Evalue setTags(...) dans le webview JS (Svelte Anki)
      5. Notifie gui_hooks.editor_did_update_tags si disponible
    """
    if editor is None:
        return

    tags_list = list(tags) if tags is not None else []
    ed_note = _get_editor_note(editor)
    if ed_note is not None:
        ed_note.tags = list(tags_list)
    note = getattr(editor, "note", None)
    if note is not None and note is not ed_note:
        note.tags = list(tags_list)

    target_note = ed_note or note
    tag_str = ""
    if target_note and hasattr(target_note, "string_tags"):
        try:
            tag_str = target_note.string_tags().strip()
        except Exception:
            tag_str = " ".join(tags_list)
    else:
        tag_str = " ".join(tags_list)

    # 1. Widget Qt TagEdit (QLineEdit)
    if hasattr(editor, "tags") and editor.tags is not None:
        try:
            if hasattr(editor.tags, "setText"):
                editor.tags.setText(tag_str)
        except Exception:
            pass

    # 2. Methode native Anki updateTags
    if hasattr(editor, "updateTags"):
        try:
            editor.updateTags()
        except Exception:
            pass

    # 3. Webview JavaScript (Anki moderne Svelte / NoteEditor)
    if hasattr(editor, "web") and editor.web is not None:
        try:
            js = f"""
            (function(tags) {{
                try {{
                    if (typeof setTags === 'function') {{
                        setTags(tags);
                    }}
                }} catch(e) {{}}
            }})({json.dumps(tags_list)});
            """
            _eval_js(editor, js)
        except Exception:
            pass

    # 4. Hook Anki
    try:
        from aqt import gui_hooks
        if target_note is not None and hasattr(gui_hooks, "editor_did_update_tags"):
            gui_hooks.editor_did_update_tags(target_note)
    except Exception:
        pass


def sync_editor_from_hunks(editor, diff: "NoteDiff") -> None:
    """
    Injecte les valeurs mergées (selon l'état courant des hunks) dans les champs
    de l'éditeur natif via JavaScript, sans rechargement complet de la note.

    Met à jour simultanément tous les champs modifiés (batch) pour garantir
    que le recto ET le verso s'actualisent toujours ensemble sans être ignorés.
    """
    import sys
    if editor is None or not hasattr(editor, "web"):
        return

    diff._syncing_to_editor = True
    try:
        note = _get_editor_note(editor)
        if note is None:
            note = getattr(editor, "note", None)
        if _DEBUG_SYNC:
            print(f"[anki-diff] sync_editor_from_hunks: editor ok, note={getattr(note,'id',None)}, diff.note_id={diff.note_id}")

        if diff.excluded:
            if getattr(diff, "action", "") != "INSERT":
                fields_to_sync = {}
                for idx, field_name in enumerate(diff.field_names):
                    old_val = diff.original_fields.get(field_name, "")
                    fields_to_sync[idx] = old_val
                    if note and hasattr(note, "fields") and idx < len(note.fields):
                        note.fields[idx] = old_val
                if fields_to_sync:
                    js = _SET_FIELDS_BATCH_JS % json.dumps(fields_to_sync)
                    _eval_js(editor, js)
                clear_field_highlights(editor)
                tags_to_sync = diff.original_tags if diff.original_tags else diff.tags
                sync_tags_to_editor(editor, tags_to_sync)
            return

        # Index des champs modifiés (pour le surlignage)
        modified_indices = []
        for idx, field_name in enumerate(diff.field_names):
            old_val = diff.original_fields.get(field_name, "")
            new_val = diff.updated_fields.get(field_name, "")
            if old_val != new_val:
                modified_indices.append(idx)

        # D'abord, surligner les champs modifiés (jaune)
        highlight_js = _HIGHLIGHT_MODIFIED_FIELDS_JS % json.dumps(modified_indices)
        _eval_js(editor, highlight_js)

        # Collecter tous les champs modifiés à synchroniser en un seul appel JS batch
        fields_to_sync = {}
        for idx, field_name in enumerate(diff.field_names):
            old_val = diff.original_fields.get(field_name, "")
            new_val = diff.updated_fields.get(field_name, "")
            merged_val = diff.merged_fields.get(field_name, "")

            has_diff = (old_val != new_val)
            is_custom = field_name in diff.custom_edit_fields
            if is_custom:
                continue

            if not has_diff:
                continue

            if note and hasattr(note, "fields") and idx < len(note.fields):
                note.fields[idx] = merged_val

            fields_to_sync[idx] = merged_val

        if fields_to_sync:
            js = _SET_FIELDS_BATCH_JS % json.dumps(fields_to_sync)
            _eval_js(editor, js)

        # Synchroniser les tags dans l'éditeur si modifiés
        tags_to_sync = diff.tags if (hasattr(diff, "tags") and diff.tags is not None) else []
        sync_tags_to_editor(editor, tags_to_sync)

        # Mettre à jour la collection SQLite directement (synchrone 0.1ms, sans background Taskman spinner)
        if note and hasattr(note, "id") and note.id:
            try:
                col = getattr(editor, "mw", None) and getattr(editor.mw, "col", None)
                if col:
                    col.update_note(note)
            except Exception:
                pass

        # Rafraîchir l'aperçu de la carte (Card Previewer) du navigateur si ouvert
        try:
            parent_win = getattr(editor, "parentWindow", None)
            browser = parent_win if hasattr(parent_win, "_previewer") else None
            if browser and hasattr(browser, "_previewer") and browser._previewer:
                browser._previewer._last_state = None
                browser._renderPreview()
        except Exception:
            pass

    finally:
        diff._syncing_to_editor = False


def highlight_field_accepted(editor, field_idx: int) -> None:
    """Surligne un champ comme 'accepté' (vert) dans l'éditeur."""
    if editor and hasattr(editor, "web"):
        _eval_js(editor, _ACCEPT_FIELD_JS % field_idx)


def highlight_field_rejected(editor, field_idx: int) -> None:
    """Surligne un champ comme 'rejeté' (rouge) dans l'éditeur."""
    if editor and hasattr(editor, "web"):
        _eval_js(editor, _REJECT_FIELD_JS % field_idx)


def clear_field_highlights(editor) -> None:
    """Supprime tous les surlignages de champs dans l'éditeur."""
    if editor and hasattr(editor, "web"):
        _eval_js(editor, _CLEAR_FIELD_HIGHLIGHTS_JS)


def _eval_js(editor, js: str) -> None:
    """Evalue du JS dans le webview de l'éditeur, avec logging du résultat."""
    try:
        # Logger le retour pour diagnostiquer les problèmes
        def _log_result(result):
            import sys
            if result and 'error' in str(result):
                print(f"[anki-diff] JS sync failed: {result}")
            elif result and _DEBUG_SYNC:
                print(f"[anki-diff] JS sync ok: {result}")
        editor.web.evalWithCallback(js, _log_result)
    except AttributeError:
        # evalWithCallback pas dispo — fallback eval()
        try:
            editor.web.eval(js)
        except Exception:
            pass
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Capture des modifications manuelles de l'utilisateur
# ---------------------------------------------------------------------------

def make_field_edit_hook(get_diff_for_note, on_field_edited_cb=None):
    """
    Retourne un hook compatible avec gui_hooks.editor_did_unfocus_field.

    get_diff_for_note(note_id) -> Optional[NoteDiff]
    on_field_edited_cb(diff, field_name, new_val) -> Optional callback

    Quand l'utilisateur édite manuellement un champ dans l'éditeur natif,
    la nouvelle valeur est capturée et stockée dans prop_mod ou old_mod
    selon le mode actif, et merged_fields est mis à jour.
    """
    def _on_field_unfocus(changed: bool, editor, current_field_idx: int) -> bool:
        """
        Appelé par Anki quand un champ perd le focus.
        Retourne True si on a modifié le champ (pour que Anki sache).
        """
        note = _get_editor_note(editor)
        if note is None:
            note = editor
        if note is None or not hasattr(note, "id") or not note.id:
            return changed
        diff = get_diff_for_note(note.id)
        if diff is None:
            return changed
        if getattr(diff, "_syncing_to_editor", False):
            return changed

        # Vérifier les champs de la note pour détecter les modifications utilisateur
        for idx, field_name in enumerate(diff.field_names):
            if idx < len(note.fields):
                new_val = note.fields[idx]

                # Ignorer les états transitoires de "Lier les cartes" :
                # le marqueur edn-cursor-marker est inséré temporairement dans le DOM
                # pendant le traitement d'un collage de NID. Capturer cet état
                # contaminerait merged_fields et bloquerait la resync du champ.
                if new_val and "edn-cursor-marker" in new_val:
                    continue

                target = getattr(diff, "field_active_target", {}).get(field_name, "PROP")
                curr_merged = diff.merged_fields.get(field_name, "")

                if new_val != curr_merged or field_name in diff.custom_edit_fields:
                    diff.merged_fields[field_name] = new_val
                    if target == "PROP":
                        diff.prop_mod_fields[field_name] = new_val
                        if new_val != diff.updated_fields.get(field_name, ""):
                            diff.custom_edit_fields[field_name] = new_val
                        else:
                            diff.custom_edit_fields.pop(field_name, None)
                    else:  # target == "OLD"
                        diff.old_mod_fields[field_name] = new_val
                        if new_val != diff.original_fields.get(field_name, ""):
                            diff.custom_edit_fields[field_name] = new_val
                        else:
                            diff.custom_edit_fields.pop(field_name, None)

                    if on_field_edited_cb is not None:
                        try:
                            on_field_edited_cb(diff, field_name, new_val)
                        except Exception as e:
                            print(f"[anki-diff] on_field_edited_cb error: {e}")

        return changed  # Ne pas altérer l'état changed d'Anki

    return _on_field_unfocus
