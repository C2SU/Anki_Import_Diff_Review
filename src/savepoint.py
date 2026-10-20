import sqlite3
import re
from dataclasses import dataclass, field
from typing import List, Dict, Callable, Any, Optional
import aqt
import threading

def _sanitize_savepoint_name(name: str) -> str:
    if not isinstance(name, str) or not re.match(r'^[a-zA-Z0-9_]+$', name):
        raise ValueError(f"Invalid savepoint name: {name}")
    return name

class SavepointManager:
    """Manages SQLite savepoints with local name sanitization (validating ^[a-zA-Z0-9_]+$)."""
    def __init__(self, db: Any, name: str = "import_diff"):
        self.db = db
        self.name = _sanitize_savepoint_name(name)

    def create(self) -> None:
        self.db.execute(f"SAVEPOINT {self.name}")

    def rollback(self) -> None:
        try:
            self.db.execute(f"ROLLBACK TO SAVEPOINT {self.name}")
        except Exception:
            pass

    def release(self) -> None:
        try:
            self.db.execute(f"RELEASE SAVEPOINT {self.name}")
        except Exception:
            pass

    def __enter__(self):
        self.create()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if exc_type is not None:
            self.rollback()
        self.release()

_import_lock = threading.Lock()

@dataclass
class NoteDiff:
    note_id: int
    original_fields: Dict[str, str]
    updated_fields: Dict[str, str]
    model_name: str
    field_names: List[str]
    action: str = "UPDATE"  # "INSERT", "UPDATE", "DELETE"
    tags: List[str] = field(default_factory=list)
    original_tags: List[str] = field(default_factory=list)
    updated_tags: List[str] = field(default_factory=list)
    merged_fields: Dict[str, str] = field(default_factory=dict)
    custom_edit_fields: Dict[str, str] = field(default_factory=dict)
    old_mod_fields: Dict[str, str] = field(default_factory=dict)
    prop_mod_fields: Dict[str, str] = field(default_factory=dict)
    field_active_target: Dict[str, str] = field(default_factory=dict)
    tags_active_target: str = "PROP"
    excluded: bool = False
    model_id: int = 0          # model ID (mid) — lookup fiable sans dépendre du nom
    deck_id: int = 0           # deck cible pour les notes INSERT (capturé pendant le savepoint)
    staged_note_id: int = 0    # ID réel après pré-insertion temporaire dans le browser
    guid: str = ""             # GUID Anki d'origine (préservé pour AnkiCollab et imports ultérieurs)

    def __post_init__(self):
        if not self.merged_fields:
            self.merged_fields = self.updated_fields.copy()
        if self.custom_edit_fields is None:
            self.custom_edit_fields = {}
        if not self.original_tags and self.action == "UPDATE" and not self.updated_tags:
            self.updated_tags = list(self.tags)
        elif not self.updated_tags and self.tags:
            self.updated_tags = list(self.tags)
        if not self.tags:
            self.tags = list(self.updated_tags) if self.updated_tags else list(self.original_tags)
        if not self.old_mod_fields:
            self.old_mod_fields = self.original_fields.copy()
        if not self.prop_mod_fields:
            self.prop_mod_fields = self.updated_fields.copy()
        if not self.field_active_target:
            self.field_active_target = {fname: "PROP" for fname in self.field_names}

def split_fields(flds_str: str, N: int, old_flds_str: str = None) -> List[str]:
    if isinstance(flds_str, bytes):
        flds_str = flds_str.decode('utf-8', 'surrogatepass')
    if isinstance(old_flds_str, bytes):
        old_flds_str = old_flds_str.decode('utf-8', 'surrogatepass')
    if flds_str is None:
        return [""] * N
    
    parts = flds_str.split("\x1f")
    if len(parts) == N:
        return parts
    if len(parts) < N:
        return parts + [""] * (N - len(parts))
        
    # If len(parts) > N, we have a delimiter leak.
    # Try using old_flds_str to align and group excess parts.
    if old_flds_str:
        old_parts = old_flds_str.split("\x1f")
        if len(old_parts) == N:
            left_idx = 0
            while left_idx < N and left_idx < len(parts) and parts[left_idx] == old_parts[left_idx]:
                left_idx += 1
                
            right_idx = 0
            while right_idx < N and right_idx < len(parts) and parts[-(right_idx+1)] == old_parts[-(right_idx+1)]:
                right_idx += 1
                
            if left_idx + right_idx >= N:
                mod_idx = N - right_idx - 1
                mod_idx = max(0, min(mod_idx, N - 1))
                excess = len(parts) - N
                start_part_idx = mod_idx
                end_part_idx = mod_idx + excess + 1
                grouped = "\x1f".join(parts[start_part_idx:end_part_idx])
                return parts[:start_part_idx] + [grouped] + parts[end_part_idx:]
            else:
                mod_idx = left_idx
                excess = len(parts) - N
                start_part_idx = mod_idx
                end_part_idx = mod_idx + excess + 1
                grouped = "\x1f".join(parts[start_part_idx:end_part_idx])
                return parts[:start_part_idx] + [grouped] + parts[end_part_idx:]
                
    return flds_str.rsplit("\x1f", N - 1)

def capture_import_changes(import_fn: Callable[..., Any], *args: Any, **kwargs: Any) -> List[NoteDiff]:
    """
    Executes import_fn within a savepoint, detects differences, rolls back,
    and returns a list of changed notes.
    """
    with _import_lock:
        db = aqt.mw.col.db
        
        # Get connection dynamically and set text_factory
        conn = None
        orig_text_factory = None
        if hasattr(db, "conn") and getattr(db, "conn") is not None:
            conn = db.conn
        elif hasattr(db, "_conn") and getattr(db, "_conn") is not None:
            conn = db._conn
        elif hasattr(db, "_db") and getattr(db, "_db") is not None:
            _db_val = db._db
            if hasattr(_db_val, "conn") and getattr(_db_val, "conn") is not None:
                conn = _db_val.conn
                
        if conn is not None:
            try:
                orig_text_factory = conn.text_factory
                conn.text_factory = lambda b: b.decode('utf-8', 'surrogatepass') if isinstance(b, bytes) else b
            except Exception:
                pass
        
        try:
            import uuid
            suffix = uuid.uuid4().hex
            tbl = f"import_diff_notes_{suffix}"
            trig_ins = f"import_diff_insert_{suffix}"
            trig_upd = f"import_diff_update_{suffix}"
            trig_del = f"import_diff_delete_{suffix}"
            
            # Create temp table
            db.execute(f"""
                CREATE TEMP TABLE IF NOT EXISTS {tbl} (
                    note_id INTEGER,
                    action TEXT,
                    old_flds TEXT,
                    new_flds TEXT,
                    model_id INTEGER,
                    tags TEXT,
                    old_tags TEXT,
                    new_tags TEXT,
                    guid TEXT
                )
            """)
            # Clear any leftover data from previous runs if temp table persisted
            db.execute(f"DELETE FROM temp.{tbl}")
            
            # Create triggers
            db.execute(f"""
                CREATE TEMP TRIGGER IF NOT EXISTS {trig_ins} AFTER INSERT ON notes BEGIN
                    INSERT INTO {tbl} (note_id, action, old_flds, new_flds, model_id, tags, old_tags, new_tags, guid)
                    VALUES (new.id, 'INSERT', NULL, new.flds, new.mid, new.tags, NULL, new.tags, new.guid);
                END;
            """)
            db.execute(f"""
                CREATE TEMP TRIGGER IF NOT EXISTS {trig_upd} AFTER UPDATE ON notes BEGIN
                    INSERT INTO {tbl} (note_id, action, old_flds, new_flds, model_id, tags, old_tags, new_tags, guid)
                    VALUES (new.id, 'UPDATE', old.flds, new.flds, new.mid, new.tags, old.tags, new.tags, new.guid);
                END;
            """)
            db.execute(f"""
                CREATE TEMP TRIGGER IF NOT EXISTS {trig_del} AFTER DELETE ON notes BEGIN
                    INSERT INTO {tbl} (note_id, action, old_flds, new_flds, model_id, tags, old_tags, new_tags, guid)
                    VALUES (old.id, 'DELETE', old.flds, NULL, old.mid, old.tags, old.tags, NULL, old.guid);
                END;
            """)
            
            # Create savepoint after temp table and triggers are created
            sp = SavepointManager(db, "import_diff")
            sp.create()
            
            try:
                import_fn(*args, **kwargs)
                
                # Query results
                rows = db.all(f"SELECT note_id, action, old_flds, new_flds, model_id, tags, old_tags, new_tags, guid FROM temp.{tbl}")
                
                # Build NoteDiff objects
                # db.all() returns plain tuples — use positional indices
                # (SELECT order: note_id=0, action=1, old_flds=2, new_flds=3, model_id=4, tags=5, old_tags=6, new_tags=7, guid=8)
                def _row_get(row, idx, name):
                    """Access a row by index (plain tuple) or name (sqlite3.Row)."""
                    try:
                        return row[idx]
                    except (KeyError, IndexError, TypeError):
                        pass
                    try:
                        return row[name]
                    except (KeyError, IndexError, TypeError):
                        return None

                # Group by note_id to preserve the initial old values and latest new values
                grouped_rows = {}
                for row in rows:
                    nid = _row_get(row, 0, "note_id")
                    if nid not in grouped_rows:
                        grouped_rows[nid] = []
                    grouped_rows[nid].append(row)

                diffs = []
                for nid, row_list in grouped_rows.items():
                    first_row = row_list[0]
                    last_row = row_list[-1]

                    action   = _row_get(last_row, 1, "action")
                    old_flds_str = _row_get(first_row, 2, "old_flds")
                    new_flds_str = _row_get(last_row, 3, "new_flds")
                    mid      = _row_get(last_row, 4, "model_id")
                    tags_str = _row_get(last_row, 5, "tags") or ""
                    old_tags_str = _row_get(first_row, 6, "old_tags")
                    new_tags_str = _row_get(last_row, 7, "new_tags")
                    guid_str = _row_get(last_row, 8, "guid") or _row_get(first_row, 8, "guid") or ""
                    
                    if isinstance(old_flds_str, bytes):
                        old_flds_str = old_flds_str.decode('utf-8', 'surrogatepass')
                    if isinstance(new_flds_str, bytes):
                        new_flds_str = new_flds_str.decode('utf-8', 'surrogatepass')
                    if isinstance(tags_str, bytes):
                        tags_str = tags_str.decode('utf-8', 'surrogatepass')
                    if isinstance(old_tags_str, bytes):
                        old_tags_str = old_tags_str.decode('utf-8', 'surrogatepass')
                    if isinstance(new_tags_str, bytes):
                        new_tags_str = new_tags_str.decode('utf-8', 'surrogatepass')
                    if isinstance(guid_str, bytes):
                        guid_str = guid_str.decode('utf-8', 'surrogatepass')
                    
                    # Split tags
                    if action == "INSERT":
                        orig_tags = []
                        upd_tags = [t for t in (new_tags_str or tags_str or "").strip().split(" ") if t]
                    elif action == "DELETE":
                        orig_tags = [t for t in (old_tags_str or tags_str or "").strip().split(" ") if t]
                        upd_tags = []
                    else:
                        orig_tags = [t for t in (old_tags_str or "").strip().split(" ") if t] if old_tags_str is not None else []
                        upd_tags = [t for t in (new_tags_str or tags_str or "").strip().split(" ") if t]

                    # Filter out no-op updates where neither fields nor tags changed
                    if action == "UPDATE" and old_flds_str == new_flds_str and set(orig_tags) == set(upd_tags):
                        continue

                    tags = list(upd_tags)
                    
                    # Fetch model information if available
                    model_name = "Default"
                    field_names = []
                    if hasattr(aqt.mw.col, "models"):
                        model = aqt.mw.col.models.get(mid)
                        if model:
                            model_name = model.get("name", "Default")
                            field_names = [f["name"] for f in model.get("flds", [])]
                    
                    # If field_names is empty or smaller than the field list, generate fallback names
                    flds_to_split = new_flds_str if new_flds_str is not None else old_flds_str
                    if not field_names:
                        split_flds = flds_to_split.split("\x1f") if flds_to_split is not None else []
                        field_names = [f"Field {i+1}" for i in range(len(split_flds))]
                    
                    N = len(field_names)
                    
                    original_fields = {}
                    updated_fields = {}
                    
                    old_split = split_fields(old_flds_str, N, None)
                    new_split = split_fields(new_flds_str, N, old_flds_str)
                    
                    for i, name in enumerate(field_names):
                        original_fields[name] = old_split[i]
                        updated_fields[name] = new_split[i]
                        
                    # Pour les notes INSERT : capturer le deck_id depuis la table cards
                    # (les cartes existent dans la DB à ce stade, avant le rollback du savepoint)
                    diff_deck_id = 0
                    if action == 'INSERT':
                        try:
                            deck_rows = db.all(f"SELECT did FROM cards WHERE nid = {nid} LIMIT 1")
                            if deck_rows:
                                did_val = _row_get(deck_rows[0], 0, "did")
                                if did_val is not None:
                                    diff_deck_id = int(did_val)
                        except Exception:
                            pass

                    diffs.append(NoteDiff(
                        note_id=nid,
                        original_fields=original_fields,
                        updated_fields=updated_fields,
                        model_name=model_name,
                        field_names=field_names,
                        action=action,
                        tags=tags,
                        original_tags=orig_tags,
                        updated_tags=upd_tags,
                        model_id=mid or 0,
                        deck_id=diff_deck_id,
                        guid=guid_str or "",
                    ))
                    
                return diffs
                
            finally:
                # Rollback and release savepoint first
                sp.rollback()
                sp.release()
                
                # Cleanup triggers and temp table
                try:
                    db.execute(f"DROP TRIGGER IF EXISTS temp.{trig_ins}")
                except Exception:
                    pass
                try:
                    db.execute(f"DROP TRIGGER IF EXISTS temp.{trig_upd}")
                except Exception:
                    pass
                try:
                    db.execute(f"DROP TRIGGER IF EXISTS temp.{trig_del}")
                except Exception:
                    pass
                try:
                    db.execute(f"DROP TABLE IF EXISTS temp.{tbl}")
                except Exception:
                    pass
        finally:
            if conn is not None and orig_text_factory is not None:
                try:
                    conn.text_factory = orig_text_factory
                except Exception:
                    pass

