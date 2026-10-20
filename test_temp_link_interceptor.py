"""
Tests unitaires pour temp_link_interceptor.py.
Vérifie le remplacement HTML chirurgical, l'interception de liens et la clôture de session.
"""

import sqlite3
import pytest
from unittest.mock import MagicMock
from anki_import_diff.src.temp_link_interceptor import (
    replace_nid_in_html,
    remove_dead_link_from_html,
    TempLinkInterceptor,
    start_diff_session,
    on_diff_session_closed,
    register_link_creation
)


def test_replace_nid_in_html_all_formats():
    old_nid = 1775000000050
    new_nid = 1650000000010

    # Format 1 : Badge standard
    f1 = f'<kbd class="clickable_cards" contenteditable="false" tabindex="0" data-nid="{old_nid}"><span class="edn-nid">{old_nid}</span></kbd>'
    r1 = replace_nid_in_html(f1, old_nid, new_nid)
    assert str(old_nid) not in r1
    assert f'data-nid="{new_nid}"' in r1
    assert f'>{new_nid}</span>' in r1

    # Format 2 : Lien avec description
    f2 = f'<span class="edn-desc">Titre</span>&nbsp;—&nbsp;<kbd class="clickable_cards" contenteditable="false" tabindex="0" data-nid="{old_nid}"><span class="edn-nid">{old_nid}</span></kbd>'
    r2 = replace_nid_in_html(f2, old_nid, new_nid)
    assert str(old_nid) not in r2
    assert f'data-nid="{new_nid}"' in r2
    assert "Titre" in r2

    # Format 3 : Hyperlien texte personnalisé (span display:none)
    f3 = f'<kbd class="clickable_cards" contenteditable="false" tabindex="0" data-nid="{old_nid}"><span class="edn-nid" style="display:none;font-size:0;line-height:0">{old_nid}</span>Mon texte</kbd>'
    r3 = replace_nid_in_html(f3, old_nid, new_nid)
    assert str(old_nid) not in r3
    assert f'data-nid="{new_nid}"' in r3
    assert f'>{new_nid}</span>Mon texte' in r3

    # Format 4 : Texte avec tiret cadratin ou tiret simple
    f4 = f'Titre — {old_nid}'
    r4 = replace_nid_in_html(f4, old_nid, new_nid)
    assert r4 == f'Titre — {new_nid}'

    # Format 5 : HTML entité &mdash;
    f5 = f'Titre &mdash; {old_nid}'
    r5 = replace_nid_in_html(f5, old_nid, new_nid)
    assert r5 == f'Titre &mdash; {new_nid}'


def test_remove_dead_link_from_html():
    dead_nid = 1775000000050

    # Lien complet avec description
    html = f'Ligne 1<br><span class="edn-desc">Titre</span>&nbsp;—&nbsp;<kbd class="clickable_cards" contenteditable="false" tabindex="0" data-nid="{dead_nid}"><span class="edn-nid">{dead_nid}</span></kbd><br>Ligne 2'
    cleaned = remove_dead_link_from_html(html, dead_nid)
    assert str(dead_nid) not in cleaned
    assert "Ligne 1" in cleaned
    assert "Ligne 2" in cleaned
    assert "<br><br>" not in cleaned

    # Badge isolé
    html2 = f'<kbd class="clickable_cards" data-nid="{dead_nid}">X</kbd>'
    cleaned2 = remove_dead_link_from_html(html2, dead_nid)
    assert cleaned2 == ""


class MockNote:
    def __init__(self, id, fields):
        self.id = id
        self.fields = list(fields)


class MockCol:
    def __init__(self):
        self.notes = {}
        self.db = MagicMock()
        self.db.scalar = self._scalar
        self.db.all = self._all
        self.db.execute = MagicMock()

    def _scalar(self, sql, *args):
        if "SELECT id FROM notes WHERE id = ?" in sql:
            nid = args[0]
            return nid if nid in self.notes else None
        return None

    def _all(self, sql, *args):
        # Ex: SELECT id FROM notes WHERE flds LIKE '%1775000000050%'
        results = []
        import re
        m = re.search(r"LIKE '%(\d+)%'", sql)
        if m:
            target_str = m.group(1)
            for nid, n in self.notes.items():
                if any(target_str in fld for fld in n.fields):
                    results.append((nid,))
        return results

    def get_note(self, nid):
        return self.notes.get(nid)

    def update_note(self, note):
        self.notes[note.id] = note


def test_interceptor_session_lifecycle_and_commit():
    interceptor = TempLinkInterceptor.instance()
    
    staged_nid = 1775000000050
    real_nid = 1650000000010
    
    col = MockCol()
    # Note pré-insérée staged
    col.notes[staged_nid] = MockNote(staged_nid, ["Front A", "Back A"])
    # Note existante dans laquelle l'utilisateur a inséré un lien vers la staged_nid
    link_html = f'<span class="edn-desc">Carte cible</span>&nbsp;—&nbsp;<kbd class="clickable_cards" data-nid="{staged_nid}"><span class="edn-nid">{staged_nid}</span></kbd>'
    col.notes[999] = MockNote(999, ["Front 999", f"Voir aussi : {link_html}"])

    # 1. Démarrer la session
    interceptor.start_session({staged_nid: real_nid})
    assert interceptor.is_active() is True
    assert interceptor.is_staged_nid(staged_nid) is True
    assert interceptor.get_real_nid(staged_nid) == real_nid

    # 2. Intercepter l'insertion de lien
    interceptor.register_link_creation(source_nid=999, target_nid=staged_nid)
    assert 999 in interceptor._tracked_modified_notes

    # 3. Fermer la session avec commit=True
    total_updated = interceptor.on_diff_session_closed(col=col, committed=True)
    
    assert total_updated >= 1
    assert interceptor.is_active() is False

    # Vérifier que l'instruction SQL de migration de note a été appelée
    assert col.db.execute.called is True
    # Vérifier que le champ de la note 999 a été actualisé avec real_nid
    updated_field = col.notes[999].fields[1]
    assert str(staged_nid) not in updated_field
    assert str(real_nid) in updated_field
    assert f'data-nid="{real_nid}"' in updated_field
    assert f'>{real_nid}</span>' in updated_field


def test_interceptor_session_cancelled():
    interceptor = TempLinkInterceptor.instance()
    
    staged_nid = 1775000000099
    real_nid = 1650000000088
    
    col = MockCol()
    # Note existante avec un lien vers la carte qui va être annulée
    link_html = f'<span class="edn-desc">Carte rejetée</span>&nbsp;—&nbsp;<kbd class="clickable_cards" data-nid="{staged_nid}"><span class="edn-nid">{staged_nid}</span></kbd>'
    col.notes[888] = MockNote(888, ["Front 888", f"Intro<br>{link_html}<br>Fin"])

    interceptor.start_session({staged_nid: real_nid})
    total_updated = interceptor.on_diff_session_closed(col=col, committed=False)

    assert total_updated >= 1
    cleaned_field = col.notes[888].fields[1]
    assert str(staged_nid) not in cleaned_field
    assert "Carte rejetée" not in cleaned_field
    assert "Intro" in cleaned_field
    assert "Fin" in cleaned_field


def test_cross_linking_between_staged_notes():
    interceptor = TempLinkInterceptor.instance()

    staged_a = 10101
    real_a = 20202
    staged_b = 30303
    real_b = 40404

    col = MockCol()
    # Note A has link to Note B (staged_b)
    link_to_b = f'<kbd class="clickable_cards" data-nid="{staged_b}"><span class="edn-nid">{staged_b}</span></kbd>'
    col.notes[staged_a] = MockNote(staged_a, [f"Lien vers B: {link_to_b}", "Back A"])

    # Note B has link to Note A (staged_a)
    link_to_a = f'<kbd class="clickable_cards" data-nid="{staged_a}"><span class="edn-nid">{staged_a}</span></kbd>'
    col.notes[staged_b] = MockNote(staged_b, [f"Lien vers A: {link_to_a}", "Back B"])

    # An existing note C has link to Note A (staged_a) and unrelated normal note (99999)
    normal_nid = 99999
    normal_link = f'<kbd class="clickable_cards" data-nid="{normal_nid}"><span class="edn-nid">{normal_nid}</span></kbd>'
    col.notes[777] = MockNote(777, [f"{link_to_a} et {normal_link}", "Back C"])

    interceptor.start_session({staged_a: real_a, staged_b: real_b})

    total_updated = interceptor.on_diff_session_closed(col=col, committed=True)
    assert total_updated >= 3

    # Note A's field has been updated to real_b
    assert str(staged_b) not in col.notes[staged_a].fields[0]
    assert str(real_b) in col.notes[staged_a].fields[0]

    # Note B's field has been updated to real_a
    assert str(staged_a) not in col.notes[staged_b].fields[0]
    assert str(real_a) in col.notes[staged_b].fields[0]

    # Note C's field has real_a and normal_nid is preserved untouched
    assert str(staged_a) not in col.notes[777].fields[0]
    assert str(real_a) in col.notes[777].fields[0]
    assert str(normal_nid) in col.notes[777].fields[0]


def test_update_pending_diffs_fields():
    interceptor = TempLinkInterceptor.instance()

    staged_nid = 50505
    real_nid = 60606

    class DummyDiff:
        def __init__(self):
            self.merged_fields = {
                "Front": f'Voir <kbd class="clickable_cards" data-nid="{staged_nid}"><span class="edn-nid">{staged_nid}</span></kbd>'
            }
            self.custom_edit_fields = {
                "Front": f'Voir <kbd class="clickable_cards" data-nid="{staged_nid}"><span class="edn-nid">{staged_nid}</span></kbd>'
            }

    diff = DummyDiff()
    interceptor.start_session({staged_nid: real_nid})
    interceptor.update_pending_diffs([diff])

    assert str(staged_nid) not in diff.merged_fields["Front"]
    assert str(real_nid) in diff.merged_fields["Front"]
    assert str(staged_nid) not in diff.custom_edit_fields["Front"]
    assert str(real_nid) in diff.custom_edit_fields["Front"]

    interceptor.on_diff_session_closed(col=None, committed=True)

