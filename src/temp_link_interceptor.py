"""
temp_link_interceptor.py — Intercepteur et actualisateur de liens temporaires pour Anki Import Diff et Lier les cartes.

Lors d'un import diff (CSV, APKG, AnkiCollab), les nouvelles notes (INSERT) sont pré-insérées
(staged) temporairement dans la collection avec un NID temporaire afin d'apparaître dans l'explorateur Anki.
Si l'utilisateur crée des liens de cartes (via l'addon 'Lier les cartes' ou par copier-coller)
vers ces notes pendant que la page de diff est ouverte, ces liens contiennent le NID temporaire.

Dès que la page de diff est fermée (commit ou fermeture) :
1. Les notes insérées reçoivent leur NID définitif (leur original_id issu de l'import).
2. Tous les liens créés avec le NID temporaire dans l'ensemble de la collection
   sont interceptés et actualisés vers le NID définitif (data-nid, span.edn-nid, tirets, etc.).
"""

import re
import time
import threading
from typing import Dict, List, Optional, Any, Set, Tuple, Iterable

import aqt
from aqt.utils import tooltip


def replace_nid_in_html(html_text: str, old_nid: int, new_nid: int) -> str:
    """
    Remplace de manière exhaustive et chirurgicale toutes les occurrences d'un NID
    dans le HTML d'un champ Anki :
    - data-nid="old" ou data-nid='old'
    - <span class="edn-nid"...>old</span> (y compris masqué)
    - <kbd class="clickable_cards"...>old</kbd>
    - '— old' ou '&mdash; old' ou '– old' (labels de description)
    """
    if not html_text or old_nid == new_nid:
        return html_text

    s_old = str(old_nid)
    s_new = str(new_nid)

    if s_old not in html_text:
        return html_text

    # 1. Attribut data-nid="..." ou data-nid='...'
    res = re.sub(
        rf'data-nid=([\"\']){re.escape(s_old)}\1',
        rf'data-nid=\g<1>{s_new}\g<1>',
        html_text
    )

    # 2. Contenu texte dans <span class="edn-nid"...>old</span>
    res = re.sub(
        rf'(<span\s+[^>]*class=[\"\'][^\"\']*edn-nid[^\"\']*[\"\'][^>]*>)\s*{re.escape(s_old)}\s*(</span>)',
        rf'\g<1>{s_new}\g<2>',
        res,
        flags=re.IGNORECASE
    )

    # 3. Contenu texte direct dans <kbd class="clickable_cards"...>old</kbd>
    res = re.sub(
        rf'(<kbd\s+[^>]*class=[\"\'][^\"\']*clickable_cards[^\"\']*[\"\'][^>]*>)\s*{re.escape(s_old)}\s*(</kbd>)',
        rf'\g<1>{s_new}\g<2>',
        res,
        flags=re.IGNORECASE
    )

    # 4. Tiret de liaison : "— old", "&mdash; old", "– old"
    res = re.sub(
        rf'([—–-]|&mdash;|&ndash;)(\s*|&nbsp;){re.escape(s_old)}\b',
        rf'\g<1>\g<2>{s_new}',
        res
    )

    return res


def remove_dead_link_from_html(html_text: str, dead_nid: int) -> str:
    """
    Supprime proprement un badge ou lien pointant vers une note annulée (dead_nid).
    Nettoie aussi les éventuels <br> résiduels ou tirets orphelins.
    """
    if not html_text:
        return html_text

    s_dead = str(dead_nid)
    if s_dead not in html_text:
        return html_text

    # 1. Supprimer le bloc complet <span class="edn-desc">...</span>&nbsp;—&nbsp;<kbd ... data-nid="dead_nid">...</kbd>
    pattern_full = rf'(?:<br\s*/?>\s*)?<span\s+[^>]*class=[\"\']edn-desc[\"\'][^>]*>.*?</span>(?:&nbsp;|\s)*[—–-](?:&nbsp;|\s)*<kbd\s+[^>]*data-nid=[\"\']{re.escape(s_dead)}[\"\'][^>]*>.*?</kbd>'
    res = re.sub(pattern_full, '', html_text, flags=re.IGNORECASE | re.DOTALL)

    # 2. Supprimer un badge <kbd ... data-nid="dead_nid">...</kbd> isolé (avec éventuel tiret devant)
    pattern_badge = rf'(?:(?:&nbsp;|\s)*[—–-](?:&nbsp;|\s)*)?<kbd\s+[^>]*data-nid=[\"\']{re.escape(s_dead)}[\"\'][^>]*>.*?</kbd>(?:&nbsp;|\s)*(?:<br\s*/?>)?'
    res = re.sub(pattern_badge, '', res, flags=re.IGNORECASE | re.DOTALL)

    # 3. Nettoyer les <br> doubles créés
    res = re.sub(r'(?:<br\s*/?>\s*){2,}', '<br>', res, flags=re.IGNORECASE)
    res = re.sub(r'^(?:<br\s*/?>\s*)+|(?:<br\s*/?>\s*)+$', '', res, flags=re.IGNORECASE)

    return res.strip()


class TempLinkInterceptor:
    """
    Singleton gérant l'interception et la mise à jour des liens temporaires.
    """
    _instance: Optional["TempLinkInterceptor"] = None
    _lock = threading.Lock()

    def __init__(self):
        self._staged_to_real: Dict[int, int] = {}
        self._tracked_modified_notes: Set[int] = set()
        self._copied_staged_nids: Set[int] = set()
        self._is_active: bool = False
        self._session_id: float = 0.0

    @classmethod
    def instance(cls) -> "TempLinkInterceptor":
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = TempLinkInterceptor()
        return cls._instance

    def is_active(self) -> bool:
        return self._is_active

    def start_session(self, staged_to_real: Dict[int, int]) -> None:
        """Démarre une session de capture de liens pour les notes pré-insérées."""
        with self._lock:
            self._staged_to_real = dict(staged_to_real)
            self._tracked_modified_notes.clear()
            self._copied_staged_nids.clear()
            self._is_active = bool(self._staged_to_real)
            self._session_id = time.time()
            print(f"[temp_link_interceptor] Session démarrée avec {len(self._staged_to_real)} note(s) staged.")

    def register_staged_notes(self, staged_to_real: Dict[int, int]) -> None:
        """Ajoute ou met à jour des correspondances staged_nid -> real_nid."""
        with self._lock:
            self._staged_to_real.update(staged_to_real)
            self._is_active = bool(self._staged_to_real)

    def is_staged_nid(self, nid: Any) -> bool:
        """True si nid est un identifiant temporaire (staged)."""
        try:
            return int(nid) in self._staged_to_real
        except (ValueError, TypeError):
            return False

    def get_real_nid(self, nid: Any) -> int:
        """Retourne le NID définitif s'il s'agit d'un staged NID, sinon nid."""
        try:
            int_nid = int(nid)
            return self._staged_to_real.get(int_nid, int_nid)
        except (ValueError, TypeError):
            return nid

    def register_link_creation(self, source_nid: Optional[int], target_nid: Any, note: Optional[Any] = None) -> None:
        """
        Enregistre qu'un lien vers un NID temporaire vient d'être créé.
        Appelé par Lier les cartes (LinkInserter) ou par l'éditeur.
        """
        try:
            target_int = int(target_nid)
        except (ValueError, TypeError):
            return

        with self._lock:
            if not self._is_active:
                return
            if target_int in self._staged_to_real:
                if source_nid:
                    self._tracked_modified_notes.add(int(source_nid))
                print(f"[temp_link_interceptor] Lien temporaire intercepté : source={source_nid} -> target_temp={target_int} (cible définitive={self._staged_to_real[target_int]})")

    def register_copied_nid(self, nid: Any) -> None:
        """Enregistre qu'un NID temporaire a été copié dans le presse-papier."""
        try:
            int_nid = int(nid)
        except (ValueError, TypeError):
            return

        with self._lock:
            if not self._is_active:
                return
            if int_nid in self._staged_to_real:
                self._copied_staged_nids.add(int_nid)
                print(f"[temp_link_interceptor] NID temporaire copié dans le presse-papier : {int_nid}")

    def update_pending_diffs(self, diffs: Iterable[Any]) -> None:
        """Met à jour les NIDs temporaires dans les merged_fields des diffs en cours."""
        with self._lock:
            mapping = dict(self._staged_to_real)

        if not mapping:
            return

        for diff in diffs:
            for staged_nid, real_nid in mapping.items():
                if staged_nid == real_nid:
                    continue
                s_staged = str(staged_nid)
                if hasattr(diff, "merged_fields") and diff.merged_fields:
                    for fname, val in list(diff.merged_fields.items()):
                        if val and s_staged in val:
                            diff.merged_fields[fname] = replace_nid_in_html(val, staged_nid, real_nid)
                if hasattr(diff, "custom_edit_fields") and diff.custom_edit_fields:
                    for fname, val in list(diff.custom_edit_fields.items()):
                        if val and s_staged in val:
                            diff.custom_edit_fields[fname] = replace_nid_in_html(val, staged_nid, real_nid)

    def migrate_staged_note_ids(self, col: Any) -> Dict[int, int]:
        """
        Migre les notes pré-insérées dans la base SQLite vers leur NID définitif (real_nid).
        Retourne le dictionnaire des migrations réussies {staged_nid: real_nid}.
        """
        if col is None or not hasattr(col, "db"):
            return {}

        with self._lock:
            mapping = dict(self._staged_to_real)

        successful_migrations: Dict[int, int] = {}
        now = int(time.time())

        try:
            col.db.execute("PRAGMA foreign_keys = OFF")
            for staged_nid, real_nid in mapping.items():
                if staged_nid == real_nid or not real_nid or real_nid <= 0:
                    continue

                try:
                    # Vérifier si la note staged existe toujours
                    staged_exists = col.db.scalar("SELECT id FROM notes WHERE id = ?", staged_nid)
                    if not staged_exists:
                        continue

                    # Vérifier si le real_nid n'est pas déjà occupé
                    real_occupied = col.db.scalar("SELECT id FROM notes WHERE id = ?", real_nid)
                    if real_occupied and real_occupied != staged_nid:
                        print(f"[temp_link_interceptor] AVERTISSEMENT : real_nid {real_nid} déjà occupé, conservation de staged_nid {staged_nid}")
                        continue

                    col.db.execute(
                        "UPDATE notes SET id = ?, mod = ?, usn = -1 WHERE id = ?",
                        real_nid, now, staged_nid
                    )
                    col.db.execute(
                        "UPDATE cards SET nid = ?, mod = ?, usn = -1 WHERE nid = ?",
                        real_nid, now, staged_nid
                    )
                    successful_migrations[staged_nid] = real_nid
                    print(f"[temp_link_interceptor] Migration note ID réussie : staged {staged_nid} -> définitif {real_nid}")

                except Exception as e:
                    print(f"[temp_link_interceptor] Erreur migration note ID {staged_nid} -> {real_nid} : {e}")

        finally:
            try:
                # Toujours rétablir PRAGMA foreign_keys = ON pour garantir l'intégrité de la base Anki
                col.db.execute("PRAGMA foreign_keys = ON")
            except Exception:
                pass

        if successful_migrations:
            try:
                if hasattr(col, "reset"):
                    col.reset()
            except Exception:
                pass
            try:
                if aqt.mw and hasattr(aqt.mw, "col") and aqt.mw.col and hasattr(aqt.mw.col, "reset") and aqt.mw.col != col:
                    aqt.mw.col.reset()
            except Exception:
                pass

        return successful_migrations

    def update_collection_links(self, col: Any, mapping: Optional[Dict[int, int]] = None, committed: bool = True) -> int:
        """
        Parcourt toutes les notes contenant un NID temporaire pour mettre à jour
        les liens vers le NID définitif (si committed) ou nettoyer les liens morts (si annulé).
        Retourne le nombre total de notes mises à jour.
        """
        if col is None or not hasattr(col, "db"):
            return 0

        if mapping is None:
            with self._lock:
                mapping = dict(self._staged_to_real)

        if not mapping:
            return 0

        staged_ids = [k for k, v in mapping.items() if (k != v or not committed) and k]
        if not staged_ids:
            return 0

        id_pattern = re.compile(r'\b(' + '|'.join(re.escape(str(nid)) for nid in staged_ids) + r')\b')
        candidate_nids: Set[int] = set()

        with self._lock:
            candidate_nids.update(self._tracked_modified_notes)

        # Single-pass SQLite scan matching all staged IDs at once in chunks
        chunk_size = 500
        for i in range(0, len(staged_ids), chunk_size):
            chunk = staged_ids[i:i + chunk_size]
            like_clauses = " OR ".join(["flds LIKE ?" for _ in chunk])
            params = [f"%{nid}%" for nid in chunk]
            found = False
            try:
                rows = col.db.all(f"SELECT id FROM notes WHERE {like_clauses}", *params)
                if rows:
                    found = True
                    for (nid_row,) in rows:
                        candidate_nids.add(nid_row)
            except Exception as e_query:
                print(f"[temp_link_interceptor] Erreur SQL recherche notes : {e_query}")

            if not found:
                # Compatibility fallback for test mocks or backends expecting inline literals
                try:
                    for nid in chunk:
                        mock_rows = col.db.all(f"SELECT id FROM notes WHERE flds LIKE '%{nid}%'")
                        if mock_rows:
                            for (nid_row,) in mock_rows:
                                candidate_nids.add(nid_row)
                except Exception:
                    pass

        updated_notes_count = 0

        for nid_row in sorted(candidate_nids):
            try:
                note = col.get_note(nid_row)
                note_modified = False

                for idx, f_val in enumerate(note.fields):
                    if not f_val or not id_pattern.search(f_val):
                        continue

                    for staged_nid in staged_ids:
                        real_nid = mapping.get(staged_nid, staged_nid)
                        s_staged = str(staged_nid)
                        if s_staged in f_val:
                            if committed:
                                new_f_val = replace_nid_in_html(f_val, staged_nid, real_nid)
                            else:
                                new_f_val = remove_dead_link_from_html(f_val, staged_nid)

                            if new_f_val != f_val:
                                f_val = new_f_val
                                note_modified = True

                    note.fields[idx] = f_val

                if note_modified:
                    col.update_note(note)
                    updated_notes_count += 1
                    action_name = "actualisé" if committed else "nettoyé"
                    print(f"[temp_link_interceptor] Note {nid_row} : liens {action_name}")

            except Exception as e_note:
                print(f"[temp_link_interceptor] Erreur lors de la mise à jour de la note {nid_row} : {e_note}")

        return updated_notes_count

    def on_diff_session_closed(self, col: Optional[Any] = None, browser: Optional[Any] = None, committed: bool = True) -> int:
        """
        Point d'entrée principal appelé dès que la page / panel d'import diff est fermée.
        Exécute la migration des IDs de notes et l'actualisation globale des liens.
        """
        with self._lock:
            if not self._is_active:
                return 0
            mapping = dict(self._staged_to_real)
            # Marquer inactif immédiatement pour éviter les réentrances
            self._is_active = False

        if not mapping:
            return 0

        actual_col = col or getattr(aqt.mw, "col", None)
        if actual_col is None:
            return 0

        total_updated = 0

        try:
            if committed:
                # 1. Migrer les IDs des notes staged vers leurs IDs réels dans la base
                migrations = self.migrate_staged_note_ids(actual_col)
                effective_mapping = dict(mapping)
                # Utiliser les migrations réelles réussies
                for staged_id, real_id in migrations.items():
                    effective_mapping[staged_id] = real_id

                # 2. Actualiser tous les liens dans toute la collection
                total_updated = self.update_collection_links(actual_col, effective_mapping, committed=True)

                if total_updated > 0 or len(migrations) > 0:
                    try:
                        tooltip(
                            f"Liens actualisés : {total_updated} note(s) mise(s) à jour avec les NID définitifs.",
                            period=3500
                        )
                    except Exception:
                        pass
            else:
                # Import rejeté : nettoyer les liens pointant vers les cartes non importées
                total_updated = self.update_collection_links(actual_col, mapping, committed=False)
                if total_updated > 0:
                    try:
                        tooltip(
                            f"Import annulé : {total_updated} note(s) nettoyée(s) des liens temporaires.",
                            period=3500
                        )
                    except Exception:
                        pass

            # 3. Recharger la note courante dans l'éditeur si ouvert
            self._refresh_active_ui(browser)

        except Exception as e:
            print(f"[temp_link_interceptor] Erreur critique lors de la clôture de la session : {e}")
            import traceback
            traceback.print_exc()

        finally:
            with self._lock:
                self._staged_to_real.clear()
                self._tracked_modified_notes.clear()
                self._copied_staged_nids.clear()

        return total_updated

    def _refresh_active_ui(self, browser: Optional[Any] = None) -> None:
        """Recharge les éditeurs et le navigateur pour refléter les liens actualisés."""
        # Recharger dans l'explorateur
        try:
            br = browser or getattr(aqt.dialogs, "_dialogs", {}).get("Browser", [None, None])[1]
            if br:
                if hasattr(br, "loadNote"):
                    br.loadNote()
                if hasattr(br, "on_notes_changed"):
                    br.on_notes_changed()
                elif hasattr(br, "table") and hasattr(br.table, "reset"):
                    br.table.reset()
        except Exception:
            pass

        # Recharger l'éditeur actif de Lier les cartes si le module est déjà chargé
        try:
            import sys
            if "Anki_EDN_Lier_les_cartes.editor_assistant" in sys.modules:
                mod_ea = sys.modules["Anki_EDN_Lier_les_cartes.editor_assistant"]
                if hasattr(mod_ea, "get_active_editor"):
                    ed = mod_ea.get_active_editor()
                    if ed:
                        if "Anki_EDN_Lier_les_cartes.edn_editor_compat" in sys.modules:
                            mod_compat = sys.modules["Anki_EDN_Lier_les_cartes.edn_editor_compat"]
                            if hasattr(mod_compat, "editor_reload_note"):
                                mod_compat.editor_reload_note(ed)
                        elif hasattr(ed, "loadNote"):
                            ed.loadNote()
        except Exception:
            pass


# ── Fonctions d'accès public ──────────────────────────────────────────────────

def get_interceptor() -> TempLinkInterceptor:
    return TempLinkInterceptor.instance()

def start_diff_session(staged_to_real: Dict[int, int]) -> None:
    TempLinkInterceptor.instance().start_session(staged_to_real)

def register_link_creation(source_nid: Optional[int], target_nid: Any, note: Optional[Any] = None) -> None:
    TempLinkInterceptor.instance().register_link_creation(source_nid, target_nid, note)

def register_copied_nid(nid: Any) -> None:
    TempLinkInterceptor.instance().register_copied_nid(nid)

def on_diff_session_closed(col: Optional[Any] = None, browser: Optional[Any] = None, committed: bool = True) -> int:
    return TempLinkInterceptor.instance().on_diff_session_closed(col, browser, committed)
