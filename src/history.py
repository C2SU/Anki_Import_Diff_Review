"""
history.py — Historique local des versions de notes et protection de champs.

Stocke un snapshot complet de chaque note à chaque commit Diff Hunk dans une
base SQLite dédiée, distincte de la collection Anki.

Architecture :
  NoteHistoryDB     — gestion des snapshots (CRUD + pruning)
  VersionEntry      — un snapshot horodaté de tous les champs d'une note
  ProtectedFieldsDB — champs marqués comme protégés contre les modifications futures

La DB est stockée dans le répertoire user data d'Anki :
  %APPDATA%/Anki2/<profile>/diff_hunk_history.db
"""

from __future__ import annotations

import json
import os
import queue
import sqlite3
import threading
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional





# ---------------------------------------------------------------------------
# Localisation de la DB
# ---------------------------------------------------------------------------

def _get_mw():
    """Lazy accessor for aqt.mw — avoids capturing None at import time."""
    try:
        import aqt
        return aqt.mw
    except ImportError:
        return None


def _get_db_path() -> str:
    """Retourne le chemin de la DB SQLite dans le répertoire du profil courant."""
    mw = _get_mw()
    try:
        if mw and hasattr(mw, "pm") and mw.pm and hasattr(mw.pm, "profileFolder") and mw.pm.profileFolder():
            base = mw.pm.profileFolder()
        elif mw and hasattr(mw, "col") and mw.col and hasattr(mw.col, "path"):
            base = os.path.dirname(mw.col.path)
        else:
            base = os.path.expanduser("~")
    except Exception:
        base = os.path.expanduser("~")
    return os.path.join(base, "diff_hunk_history.db")


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------

@dataclass
class VersionEntry:
    """Un snapshot complet d'une note à un instant donné."""
    version_id: str
    note_id: int
    timestamp: datetime
    source: str          # "import_csv", "import_apkg", "manual", "baseline", "backup", "restore", etc.
    comment: str         # Message descriptif libre
    fields: Dict[str, str]
    tags: List[str] = field(default_factory=list)

    @property
    def timestamp_str(self) -> str:
        return self.timestamp.strftime("%Y-%m-%d %H:%M")

    @property
    def source_icon(self) -> str:
        labels = {
            "baseline":      "[Initial]",
            "backup":        "[Sauvegarde]",
            "manual":        "[Manuel]",
            "import":        "[Import]",
            "import_csv":    "[CSV]",
            "import_apkg":   "[APKG]",
            "import_update": "[Import]",
            "import_insert": "[Ajout]",
            "restore":       "[Restauration]",
            "ankicollab":    "[Collab]",
        }
        return labels.get(self.source, "[Version]")


@dataclass
class FieldDelta:
    """Différence pour un champ spécifique entre deux versions."""
    field_name: str
    old_value: str
    new_value: str
    has_changed: bool


@dataclass
class VersionDelta:
    """Version avec calcul explicite des deltas par rapport à la version précédente."""
    version: VersionEntry
    version_number: int  # 1 pour la plus ancienne, incrémenté jusqu'à N
    previous_version: Optional[VersionEntry]
    changed_fields: Dict[str, FieldDelta]
    all_fields: Dict[str, str]
    tags_changed: bool
    old_tags: List[str]
    new_tags: List[str]
    is_current: bool = False


# ---------------------------------------------------------------------------
# NoteHistoryDB
# ---------------------------------------------------------------------------

_MAX_VERSIONS_PER_NOTE = 50  # garde les N versions les plus récentes par note


class NoteHistoryDB:
    """
    Gestion des snapshots horodatés de notes.

    Usage (singleton recommandé) :
        db = NoteHistoryDB.instance()
        db.store_snapshot(note_id, {"Recto": "...", "Verso": "..."}, source="import_csv")
        history = db.get_history(note_id)
    """

    _instance: Optional["NoteHistoryDB"] = None
    _lock = threading.Lock()

    def __init__(self, db_path: Optional[str] = None):
        self._path = db_path or _get_db_path()
        self._local = threading.local()
        self._write_queue: queue.Queue = queue.Queue()
        self._worker_thread: Optional[threading.Thread] = None
        self._worker_lock = threading.Lock()
        self._connections: List[sqlite3.Connection] = []
        self._connections_lock = threading.Lock()
        self._closed = False
        self._init_schema()

    @classmethod
    def instance(cls) -> "NoteHistoryDB":
        with cls._lock:
            if cls._instance is None:
                cls._instance = cls()
            return cls._instance

    @classmethod
    def reset_instance(cls) -> None:
        """Force la fermeture et recréation du singleton (utile après fermeture ou changement de profil)."""
        with cls._lock:
            if cls._instance is not None:
                try:
                    cls._instance.close()
                except Exception as e:
                    print(f"[anki-diff] Error closing NoteHistoryDB: {e}")
                cls._instance = None

    # ── Connexion thread-safe ────────────────────────────────────────────

    def _conn(self) -> sqlite3.Connection:
        """Retourne une connexion SQLite par thread (thread-local)."""
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self._path, check_same_thread=False, timeout=10.0)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA busy_timeout=5000")
            conn.execute("PRAGMA foreign_keys=ON")
            self._local.conn = conn
            with self._connections_lock:
                self._connections.append(conn)
        return conn

    def close(self) -> None:
        """Ferme proprement le worker et toutes les connexions SQLite ouvertes."""
        self._closed = True
        try:
            self.flush(timeout=2.0)
        except Exception:
            pass
        with self._worker_lock:
            if self._worker_thread and self._worker_thread.is_alive():
                try:
                    self._write_queue.put(None)
                    self._worker_thread.join(timeout=1.5)
                except Exception:
                    pass
                self._worker_thread = None
        with self._connections_lock:
            for conn in self._connections:
                try:
                    conn.close()
                except Exception:
                    pass
            self._connections.clear()
        self._local = threading.local()

    # ── Background Worker ────────────────────────────────────────────────

    def _ensure_worker(self) -> None:
        with self._worker_lock:
            if self._worker_thread is None or not self._worker_thread.is_alive():
                self._worker_thread = threading.Thread(
                    target=self._worker_loop, name="DiffHunkHistoryWorker", daemon=True
                )
                self._worker_thread.start()

    def _worker_loop(self) -> None:
        while True:
            try:
                task = self._write_queue.get()
                if task is None:
                    break
                action = task[0]
                if action == "store":
                    _, vid, note_id, fields, source, comment, tags = task
                    self._store_snapshot_sync(vid, note_id, fields, source, comment, tags)
                elif action == "update":
                    _, vid, fields, comment, tags = task
                    self._update_snapshot_fields_sync(vid, fields, comment, tags)
                self._write_queue.task_done()
            except Exception as e:
                try:
                    print(f"[anki-diff] history worker error: {e}")
                except Exception:
                    pass

    def flush(self, timeout: float = 2.0) -> None:
        """Attend que toutes les écritures en attente soient exécutées (utile pour tests)."""
        try:
            self._write_queue.join()
        except Exception:
            pass

    # ── Schéma ──────────────────────────────────────────────────────────

    def _init_schema(self) -> None:
        conn = self._conn()
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS note_versions (
                version_id  TEXT PRIMARY KEY,
                note_id     INTEGER NOT NULL,
                timestamp   TEXT    NOT NULL,
                source      TEXT    NOT NULL DEFAULT 'import',
                comment     TEXT    NOT NULL DEFAULT '',
                fields_json TEXT    NOT NULL DEFAULT '{}'
            );
            CREATE INDEX IF NOT EXISTS idx_nv_note_id ON note_versions(note_id, timestamp DESC);

            CREATE TABLE IF NOT EXISTS protected_fields (
                note_id     INTEGER NOT NULL,
                field_name  TEXT    NOT NULL,
                locked_value TEXT   NOT NULL DEFAULT '',
                locked_at   TEXT    NOT NULL,
                PRIMARY KEY (note_id, field_name)
            );
        """)
        conn.commit()
        try:
            self.cleanup_redundant_duplicates()
            self.consolidate_intermediate_versions()
        except Exception:
            pass

    # ── Snapshots ────────────────────────────────────────────────────────

    def store_snapshot_async(
        self,
        note_id: int,
        fields: Dict[str, str],
        source: str = "import",
        comment: str = "",
        tags: Optional[List[str]] = None,
        version_id: Optional[str] = None,
    ) -> str:
        """
        Enregistre un snapshot en tâche de fond (immédiat, 0ms de freeze UI).
        Retourne le version_id créé.
        """
        import uuid
        vid = version_id or uuid.uuid4().hex
        self._ensure_worker()
        self._write_queue.put(
            ("store", vid, note_id, dict(fields), source, comment, list(tags) if tags else [])
        )
        return vid

    def update_snapshot_fields_async(
        self,
        version_id: str,
        fields: Dict[str, str],
        comment: str = "",
        tags: Optional[List[str]] = None,
    ) -> None:
        """
        Met à jour un snapshot existant en tâche de fond (immédiat, 0ms de freeze UI).
        """
        self._ensure_worker()
        self._write_queue.put(
            ("update", version_id, dict(fields), comment, list(tags) if tags is not None else None)
        )

    def store_snapshot(
        self,
        note_id: int,
        fields: Dict[str, str],
        source: str = "import",
        comment: str = "",
        tags: Optional[List[str]] = None,
    ) -> str:
        """
        Enregistre un snapshot complet de la note de manière synchrone.
        Retourne le version_id créé.
        """
        import uuid
        vid = uuid.uuid4().hex
        self._store_snapshot_sync(vid, note_id, fields, source, comment, tags or [])
        return vid

    def _store_snapshot_sync(
        self,
        vid: str,
        note_id: int,
        fields: Dict[str, str],
        source: str,
        comment: str,
        tags: List[str],
    ) -> None:
        stored_fields = dict(fields)
        stored_fields["__tags__"] = " ".join(tags) if tags else ""
        json_to_store = json.dumps(stored_fields, ensure_ascii=False)
        conn = self._conn()

        # 1. Vérifier la dernière version enregistrée pour cette note
        last_row = conn.execute(
            """SELECT version_id, timestamp, source, fields_json
               FROM note_versions
               WHERE note_id = ?
               ORDER BY timestamp DESC
               LIMIT 1""",
            (note_id,),
        ).fetchone()

        if last_row:
            last_vid, last_ts_str, last_src, last_json = last_row
            # A. Dédoublonnage strict : si le contenu est strictement identique, ne pas dupliquer !
            if last_json == json_to_store:
                return

            # B. Coalescing de session : si la dernière version est de type 'manual' et date de moins de 15 minutes,
            # et que la nouvelle sauvegarde est également une édition manuelle en cours,
            # on met à jour la version de la session active pour préserver l'historique sans saturation.
            # RÈGLE D'OR : une version 'baseline' (état initial pré-session) n'est JAMAIS écrasée !
            if source == "manual" and last_src == "manual":
                try:
                    last_dt = datetime.fromisoformat(last_ts_str)
                    elapsed = (datetime.now() - last_dt).total_seconds()
                    if elapsed < 900:  # 15 minutes
                        now = datetime.now().isoformat()
                        conn.execute(
                            """UPDATE note_versions
                               SET timestamp = ?, comment = ?, fields_json = ?
                               WHERE version_id = ?""",
                            (now, comment or "Sauvegarde automatique (édition)", json_to_store, last_vid),
                        )
                        conn.commit()
                        return
                except Exception:
                    pass

        now = datetime.now().isoformat()
        conn.execute(
            """INSERT INTO note_versions (version_id, note_id, timestamp, source, comment, fields_json)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (vid, note_id, now, source, comment, json_to_store),
        )
        conn.commit()
        self.prune_old_versions(note_id)

    def update_snapshot_fields(
        self,
        version_id: str,
        fields: Dict[str, str],
        comment: str = "",
        tags: Optional[List[str]] = None,
    ) -> None:
        """Met à jour un snapshot existant de manière synchrone."""
        self._update_snapshot_fields_sync(version_id, fields, comment, tags)

    def _update_snapshot_fields_sync(
        self,
        version_id: str,
        fields: Dict[str, str],
        comment: str,
        tags: Optional[List[str]],
    ) -> None:
        if tags is None:
            version = self.get_version(version_id)
            tags = version.tags if version else []

        stored_fields = dict(fields)
        stored_fields["__tags__"] = " ".join(tags)
        now = datetime.now().isoformat()
        conn = self._conn()
        conn.execute(
            """UPDATE note_versions 
               SET timestamp = ?, comment = ?, fields_json = ?
               WHERE version_id = ?""",
            (now, comment, json.dumps(stored_fields, ensure_ascii=False), version_id),
        )
        conn.commit()

    def get_history(self, note_id: int) -> List[VersionEntry]:
        """
        Retourne la liste des versions pour une note, du plus récent au plus ancien.
        """
        conn = self._conn()
        rows = conn.execute(
            """SELECT version_id, note_id, timestamp, source, comment, fields_json
               FROM note_versions
               WHERE note_id = ?
               ORDER BY timestamp DESC""",
            (note_id,),
        ).fetchall()

        entries = []
        for row in rows:
            try:
                fields = json.loads(row["fields_json"])
            except (json.JSONDecodeError, TypeError):
                fields = {}
            tags_str = fields.pop("__tags__", "")
            tags = [t for t in tags_str.strip().split(" ") if t]
            entries.append(VersionEntry(
                version_id=row["version_id"],
                note_id=row["note_id"],
                timestamp=datetime.fromisoformat(row["timestamp"]),
                source=row["source"],
                comment=row["comment"],
                fields=fields,
                tags=tags,
            ))
        return entries

    def get_version(self, version_id: str) -> Optional[VersionEntry]:
        """Retourne un snapshot précis par version_id."""
        conn = self._conn()
        row = conn.execute(
            """SELECT version_id, note_id, timestamp, source, comment, fields_json
               FROM note_versions WHERE version_id = ?""",
            (version_id,),
        ).fetchone()
        if row is None:
            return None
        try:
            fields = json.loads(row["fields_json"])
        except (json.JSONDecodeError, TypeError):
            fields = {}
        tags_str = fields.pop("__tags__", "")
        tags = [t for t in tags_str.strip().split(" ") if t]
        return VersionEntry(
            version_id=row["version_id"],
            note_id=row["note_id"],
            timestamp=datetime.fromisoformat(row["timestamp"]),
            source=row["source"],
            comment=row["comment"],
            fields=fields,
            tags=tags,
        )

    def prune_old_versions(self, note_id: int, keep: int = _MAX_VERSIONS_PER_NOTE) -> None:
        """Supprime les versions excédentaires (garde les `keep` plus récentes)."""
        conn = self._conn()
        total_row = conn.execute("SELECT count(*) FROM note_versions WHERE note_id = ?", (note_id,)).fetchone()
        if not total_row or total_row[0] <= keep:
            return

        conn.execute(
            """DELETE FROM note_versions
               WHERE note_id = ?
               AND version_id NOT IN (
                   SELECT version_id FROM note_versions
                   WHERE note_id = ?
                   ORDER BY timestamp DESC
                   LIMIT ?
               )""",
            (note_id, note_id, keep),
        )
        conn.commit()

    def ensure_baseline_snapshot(
        self,
        note_id: int,
        fields: Dict[str, str],
        tags: Optional[List[str]] = None,
        mod_time: Optional[float] = None,
    ) -> Optional[str]:
        """
        Garantit que l'état initial d'une note (avant toute modification de la session)
        est enregistré comme snapshot de référence immuable dans l'historique.
        Si la dernière version enregistrée est déjà identique, ne fait rien.
        Retourne le version_id si un snapshot a été créé, None sinon.
        """
        stored_fields = dict(fields)
        stored_fields["__tags__"] = " ".join(tags) if tags else ""
        json_to_store = json.dumps(stored_fields, ensure_ascii=False)
        conn = self._conn()

        last_row = conn.execute(
            """SELECT version_id, fields_json FROM note_versions
               WHERE note_id = ?
               ORDER BY timestamp DESC, rowid DESC
               LIMIT 1""",
            (note_id,),
        ).fetchone()

        if last_row and last_row["fields_json"] == json_to_store:
            return None

        import uuid
        vid = uuid.uuid4().hex
        if mod_time:
            try:
                ts = datetime.fromtimestamp(mod_time).isoformat()
            except Exception:
                ts = datetime.now().isoformat()
        else:
            ts = datetime.now().isoformat()

        conn.execute(
            """INSERT INTO note_versions (version_id, note_id, timestamp, source, comment, fields_json)
               VALUES (?, ?, ?, 'baseline', 'État initial (avant modification)', ?)""",
            (vid, note_id, ts, json_to_store),
        )
        conn.commit()
        return vid

    def cleanup_redundant_duplicates(self) -> int:
        """Nettoie les doublons consécutifs identiques accumulés dans la base pour toutes les sources."""
        conn = self._conn()
        try:
            cursor = conn.cursor()
            cursor.execute("""
                DELETE FROM note_versions
                WHERE rowid IN (
                    SELECT nv1.rowid
                    FROM note_versions nv1
                    JOIN note_versions nv2
                      ON nv1.note_id = nv2.note_id
                     AND nv1.fields_json = nv2.fields_json
                     AND nv1.timestamp > nv2.timestamp
                     AND NOT EXISTS (
                         SELECT 1 FROM note_versions mid
                         WHERE mid.note_id = nv1.note_id
                           AND mid.timestamp > nv2.timestamp
                           AND mid.timestamp < nv1.timestamp
                           AND mid.fields_json != nv1.fields_json
                     )
                )
            """)
            deleted = cursor.rowcount
            conn.commit()
            return deleted
        except Exception as e:
            print(f"[anki-diff] Error in cleanup_redundant_duplicates: {e}")
            return 0

    def consolidate_intermediate_versions(self, note_id: Optional[int] = None) -> int:
        """
        Consolide les rafales de versions intermédiaires générées lors de la frappe
        au clavier (modifications manuelles successives espacées de moins de 3 minutes).
        Conserve uniquement la première version du cluster (état initial de la frappe)
        et la dernière version (état final stabilisé).
        """
        conn = self._conn()
        query = "SELECT rowid, version_id, note_id, timestamp, source, fields_json FROM note_versions"
        if note_id:
            query += f" WHERE note_id = {int(note_id)}"
        query += " ORDER BY note_id, timestamp ASC, rowid ASC"

        rows = conn.execute(query).fetchall()
        from collections import defaultdict
        by_note = defaultdict(list)
        for r in rows:
            by_note[r["note_id"]].append(r)

        to_delete = []
        for nid, note_rows in by_note.items():
            if len(note_rows) < 3:
                continue
            cluster = []
            for r in note_rows:
                try:
                    ts = datetime.fromisoformat(r["timestamp"])
                except Exception:
                    continue
                src = r["source"]
                if not cluster:
                    cluster.append((r, ts))
                    continue
                prev_r, prev_ts = cluster[-1]
                gap = (ts - prev_ts).total_seconds()
                if gap < 180 and src == "manual" and prev_r["source"] == "manual":
                    cluster.append((r, ts))
                else:
                    if len(cluster) > 2:
                        for mid_r, _ in cluster[1:-1]:
                            to_delete.append(mid_r["rowid"])
                    cluster = [(r, ts)]
            if len(cluster) > 2:
                for mid_r, _ in cluster[1:-1]:
                    to_delete.append(mid_r["rowid"])

        if to_delete:
            conn.executemany("DELETE FROM note_versions WHERE rowid = ?", [(rid,) for rid in to_delete])
            conn.commit()
        return len(to_delete)

    def scan_backups_for_note(
        self,
        note_id: int,
        progress_callback: Optional[Callable[[int, int, int], None]] = None,
    ) -> List[VersionEntry]:
        """
        Explore les sauvegardes automatiques (.colpkg) d'Anki pour extraire
        toutes les versions antérieures distinctes de la note.
        Insère les versions trouvées dans note_versions avec source='backup'.
        Retourne la liste complète à jour de toutes les versions de la note.
        """
        mw = _get_mw()
        backup_folder = None
        if mw and hasattr(mw, "pm") and mw.pm and hasattr(mw.pm, "backupFolder"):
            try:
                backup_folder = mw.pm.backupFolder()
            except Exception:
                pass
        if not backup_folder or not os.path.isdir(backup_folder):
            db_dir = os.path.dirname(self._path)
            cand = os.path.join(db_dir, "backups")
            if os.path.isdir(cand):
                backup_folder = cand

        if not backup_folder or not os.path.isdir(backup_folder):
            return self.get_history(note_id)

        import glob
        backups = sorted(glob.glob(os.path.join(backup_folder, "*.colpkg")), reverse=True)
        if not backups:
            return self.get_history(note_id)

        try:
            import anki._backend
            backend = anki._backend.RustBackend()
        except Exception:
            return self.get_history(note_id)

        import tempfile
        import shutil

        # Récupérer les empreintes déjà connues en base
        conn = self._conn()
        existing_jsons = set(
            r["fields_json"] for r in conn.execute(
                "SELECT fields_json FROM note_versions WHERE note_id = ?", (note_id,)
            ).fetchall()
        )

        tmp_dir = tempfile.mkdtemp(prefix="anki_diff_backup_scan_")
        found_count = 0
        total = len(backups)

        try:
            media_dir = os.path.join(tmp_dir, "media")
            media_db = os.path.join(tmp_dir, "media.db")
            os.makedirs(media_dir, exist_ok=True)
            col_path = os.path.join(tmp_dir, "temp_col.anki2")

            last_seen_mod = None

            for idx, bp in enumerate(backups):
                if progress_callback:
                    try:
                        progress_callback(idx + 1, total, found_count)
                    except Exception:
                        pass

                try:
                    backend.import_collection_package(
                        col_path=col_path,
                        backup_path=bp,
                        media_folder=media_dir,
                        media_db=media_db,
                    )
                    bconn = sqlite3.connect(col_path)
                    brow = bconn.execute(
                        "SELECT mid, mod, flds, tags FROM notes WHERE id = ?", (note_id,)
                    ).fetchone()

                    if brow:
                        b_mid, b_mod, b_flds_str, b_tags_str = brow
                        if b_mod != last_seen_mod:
                            last_seen_mod = b_mod

                            # Récupérer les noms de champs
                            fld_names = [
                                r[0] for r in bconn.execute(
                                    "SELECT name FROM fields WHERE ntid = ? ORDER BY ord ASC", (b_mid,)
                                ).fetchall()
                            ]
                            vals = b_flds_str.split("\x1f")
                            if fld_names and len(vals) <= len(fld_names):
                                fld_dict = dict(zip(fld_names, vals))
                            else:
                                fld_dict = {fld_names[i] if i < len(fld_names) else f"Field_{i}": vals[i] for i in range(len(vals))}

                            b_tags = [t for t in b_tags_str.strip().split(" ") if t]
                            fld_dict["__tags__"] = " ".join(b_tags)
                            fj = json.dumps(fld_dict, ensure_ascii=False)

                            if fj not in existing_jsons:
                                existing_jsons.add(fj)
                                import uuid
                                vid = uuid.uuid4().hex
                                dt = datetime.fromtimestamp(b_mod)
                                b_comment = f"Sauvegarde Anki du {dt.strftime('%d/%m/%Y %H:%M')}"
                                conn.execute(
                                    """INSERT INTO note_versions (version_id, note_id, timestamp, source, comment, fields_json)
                                       VALUES (?, ?, ?, 'backup', ?, ?)""",
                                    (vid, note_id, dt.isoformat(), b_comment, fj),
                                )
                                conn.commit()
                                found_count += 1
                    bconn.close()
                except Exception:
                    continue
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

        return self.get_history(note_id)

    def get_history_with_deltas(
        self,
        note_id: int,
        current_fields: Optional[Dict[str, str]] = None,
        current_tags: Optional[List[str]] = None,
    ) -> List[VersionDelta]:
        """
        Retourne l'historique de la note enrichi avec le calcul des différences
        champ par champ (Previous vs Updated) pour chaque version.
        La liste est ordonnée de la plus récente à la plus ancienne.
        """
        raw_history = self.get_history(note_id)
        chronological = list(reversed(raw_history))

        deltas_chronological: List[VersionDelta] = []
        prev_entry: Optional[VersionEntry] = None

        for idx, entry in enumerate(chronological):
            v_num = idx + 1
            fields = dict(entry.fields)
            tags = list(entry.tags)

            changed_fields: Dict[str, FieldDelta] = {}
            if prev_entry is None:
                for fname, val in fields.items():
                    if val:
                        changed_fields[fname] = FieldDelta(
                            field_name=fname,
                            old_value="",
                            new_value=val,
                            has_changed=True,
                        )
                tags_changed = bool(tags)
                old_tags: List[str] = []
            else:
                prev_fields = prev_entry.fields
                all_keys = set(fields.keys()) | set(prev_fields.keys())
                for fname in all_keys:
                    old_v = prev_fields.get(fname, "")
                    new_v = fields.get(fname, "")
                    if old_v != new_v:
                        changed_fields[fname] = FieldDelta(
                            field_name=fname,
                            old_value=old_v,
                            new_value=new_v,
                            has_changed=True,
                        )
                old_tags = list(prev_entry.tags)
                tags_changed = sorted(old_tags) != sorted(tags)

            delta = VersionDelta(
                version=entry,
                version_number=v_num,
                previous_version=prev_entry,
                changed_fields=changed_fields,
                all_fields=fields,
                tags_changed=tags_changed,
                old_tags=old_tags,
                new_tags=tags,
                is_current=False,
            )
            deltas_chronological.append(delta)
            prev_entry = entry

        res = list(reversed(deltas_chronological))

        if res and current_fields:
            clean_curr = {k: v for k, v in current_fields.items() if k != "__tags__"}
            for d in res:
                clean_ver = {k: v for k, v in d.all_fields.items() if k != "__tags__"}
                if clean_ver == clean_curr:
                    d.is_current = True
                    break

        return res

    def delete_note_history(self, note_id: int) -> None:
        """Supprime tout l'historique d'une note."""
        conn = self._conn()
        conn.execute("DELETE FROM note_versions WHERE note_id = ?", (note_id,))
        conn.commit()

    def clear_all(self) -> None:
        """Vide toute la base (utile pour les tests)."""
        conn = self._conn()
        conn.execute("DELETE FROM note_versions")
        conn.execute("DELETE FROM protected_fields")
        conn.commit()

    def note_count(self) -> int:
        """Nombre de notes distinctes dans l'historique."""
        conn = self._conn()
        row = conn.execute(
            "SELECT COUNT(DISTINCT note_id) FROM note_versions"
        ).fetchone()
        return row[0] if row else 0

    def version_count(self) -> int:
        """Nombre total de snapshots dans la base."""
        conn = self._conn()
        row = conn.execute("SELECT COUNT(*) FROM note_versions").fetchone()
        return row[0] if row else 0


# ---------------------------------------------------------------------------
# ProtectedFieldsDB
# ---------------------------------------------------------------------------

class ProtectedFieldsDB:
    """
    Gestion des champs protégés par note.

    Un champ protégé est conservé tel quel lors des diffs futurs :
    le hunk correspondant est auto-rejeté (avec badge [Verrouille]) mais l'user
    peut déverrouiller depuis le diff panel.
    """

    _instance: Optional["ProtectedFieldsDB"] = None
    _lock = threading.Lock()

    def __init__(self, db: Optional[NoteHistoryDB] = None):
        self._db = db or NoteHistoryDB.instance()

    @classmethod
    def instance(cls) -> "ProtectedFieldsDB":
        with cls._lock:
            if cls._instance is None:
                cls._instance = cls()
            return cls._instance

    @classmethod
    def reset_instance(cls) -> None:
        with cls._lock:
            cls._instance = None

    def protect_field(self, note_id: int, field_name: str, locked_value: str) -> None:
        """Protège un champ avec la valeur actuelle (ou récupérée)."""
        now = datetime.now().isoformat()
        conn = self._db._conn()
        conn.execute(
            """INSERT OR REPLACE INTO protected_fields (note_id, field_name, locked_value, locked_at)
               VALUES (?, ?, ?, ?)""",
            (note_id, field_name, locked_value, now),
        )
        conn.commit()

    def unprotect_field(self, note_id: int, field_name: str) -> None:
        """Retire la protection d'un champ."""
        conn = self._db._conn()
        conn.execute(
            "DELETE FROM protected_fields WHERE note_id = ? AND field_name = ?",
            (note_id, field_name),
        )
        conn.commit()

    def is_protected(self, note_id: int, field_name: str) -> bool:
        conn = self._db._conn()
        row = conn.execute(
            "SELECT 1 FROM protected_fields WHERE note_id = ? AND field_name = ?",
            (note_id, field_name),
        ).fetchone()
        return row is not None

    def get_protected_value(self, note_id: int, field_name: str) -> Optional[str]:
        conn = self._db._conn()
        row = conn.execute(
            "SELECT locked_value FROM protected_fields WHERE note_id = ? AND field_name = ?",
            (note_id, field_name),
        ).fetchone()
        return row["locked_value"] if row else None

    def get_all_protected(self, note_id: int) -> Dict[str, str]:
        """Retourne {field_name: locked_value} pour tous les champs protégés d'une note."""
        conn = self._db._conn()
        rows = conn.execute(
            "SELECT field_name, locked_value FROM protected_fields WHERE note_id = ?",
            (note_id,),
        ).fetchall()
        return {row["field_name"]: row["locked_value"] for row in rows}

    def get_protection_info(self, note_id: int, field_name: str) -> Optional[dict]:
        """Retourne les détails de protection (valeur + date) pour affichage UI."""
        conn = self._db._conn()
        row = conn.execute(
            "SELECT locked_value, locked_at FROM protected_fields WHERE note_id = ? AND field_name = ?",
            (note_id, field_name),
        ).fetchone()
        if row is None:
            return None
        return {
            "locked_value": row["locked_value"],
            "locked_at": datetime.fromisoformat(row["locked_at"]),
        }

    def clear_note_protections(self, note_id: int) -> None:
        conn = self._db._conn()
        conn.execute("DELETE FROM protected_fields WHERE note_id = ?", (note_id,))
        conn.commit()
