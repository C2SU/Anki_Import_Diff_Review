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
    source: str          # "import_csv", "import_apkg", "manual", "ankicollab", "restore"
    comment: str         # Message descriptif libre
    fields: Dict[str, str]
    tags: List[str] = field(default_factory=list)

    @property
    def timestamp_str(self) -> str:
        return self.timestamp.strftime("%Y-%m-%d %H:%M")

    @property
    def source_icon(self) -> str:
        labels = {
            "import_csv":  "[CSV]",
            "import_apkg": "[APKG]",
            "manual":      "[Manuel]",
            "ankicollab":  "[Collab]",
            "restore":     "[Restauration]",
        }
        return labels.get(self.source, "[Version]")


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
        self._init_schema()

    @classmethod
    def instance(cls) -> "NoteHistoryDB":
        with cls._lock:
            if cls._instance is None:
                cls._instance = cls()
            return cls._instance

    @classmethod
    def reset_instance(cls) -> None:
        """Force la recréation du singleton (utile après changement de profil)."""
        with cls._lock:
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
        return conn

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

            # B. Coalescing de session : si la dernière version est de type 'manual' et date de moins de 5 minutes,
            # et que la nouvelle sauvegarde est également une édition manuelle en cours,
            # on met à jour la version de la session active pour préserver l'historique sans saturation.
            if source == "manual" and last_src == "manual" and "édition" in comment.lower():
                try:
                    last_dt = datetime.fromisoformat(last_ts_str)
                    elapsed = (datetime.now() - last_dt).total_seconds()
                    if elapsed < 300:  # 5 minutes
                        now = datetime.now().isoformat()
                        conn.execute(
                            """UPDATE note_versions
                               SET timestamp = ?, comment = ?, fields_json = ?
                               WHERE version_id = ?""",
                            (now, comment, json_to_store, last_vid),
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

    def cleanup_redundant_duplicates(self) -> int:
        """Nettoie les doublons consécutifs identiques accumulés dans la base."""
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
                     AND nv1.source = 'manual'
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

    def __init__(self, db: Optional[NoteHistoryDB] = None):
        self._db = db or NoteHistoryDB.instance()

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
