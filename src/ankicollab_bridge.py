"""
ankicollab_bridge.py — Bridge entre Diff Hunk et l'addon AnkiCollab.

Ce module est optionnel : si AnkiCollab n'est pas installé ou que l'user
n'est pas connecté, toutes les fonctions retournent gracieusement None/[].

Fonctionnement :
  - Détecte l'addon AnkiCollab (id 1957538407) via aqt.mw.addonManager
  - Récupère le token d'auth depuis sa config
  - Interroge l'API ankicollab.com pour :
      · L'historique des commits d'une note (via son GUID Anki)
      · Les suggestions en attente pour un deck partagé
  - Offre apply_version() pour remplir les champs depuis un commit ou une suggestion

API AnkiCollab utilisée (REST publique) :
  GET /api/note/{guid}/history   → commits horodatés
  GET /api/note/{guid}/suggestions → propositions des users

Note : Si le plugin AnkiCollab est installé, on délègue l'auth à son
api_client pour éviter de dupliquer la logique.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional, Any

try:
    import aqt
    _mw = aqt.mw
except ImportError:
    _mw = None

# ID de l'addon AnkiCollab officiel (AnkiWeb)
_ANKICOLLAB_ADDON_ID = "1957538407"
_API_BASE = "https://plugin.ankicollab.com"
_DEFAULT_TIMEOUT = (10, 30)  # (connect, read)


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------

@dataclass
class CollabCommit:
    """Un commit dans l'historique d'une note sur AnkiCollab."""
    commit_hash: str
    message: str
    author: str
    timestamp: datetime
    fields: Dict[str, str]

    @property
    def timestamp_str(self) -> str:
        return self.timestamp.strftime("%Y-%m-%d %H:%M")

    @property
    def short_hash(self) -> str:
        return self.commit_hash[:8]


@dataclass
class CollabSuggestion:
    """Une proposition d'un utilisateur sur AnkiCollab."""
    suggestion_id: str
    author: str
    created_at: datetime
    status: str          # "pending", "accepted", "rejected"
    fields: Dict[str, str]
    vote_count: int = 0
    comment: str = ""

    @property
    def status_icon(self) -> str:
        return {"pending": "En attente", "accepted": "Accepté", "rejected": "Rejeté"}.get(self.status, "")

    @property
    def created_str(self) -> str:
        return self.created_at.strftime("%Y-%m-%d")


# ---------------------------------------------------------------------------
# AnkiCollabBridge
# ---------------------------------------------------------------------------

class AnkiCollabBridge:
    """
    Bridge vers l'API AnkiCollab. Singleton thread-safe.

    Usage :
        bridge = AnkiCollabBridge.instance()
        if bridge.is_available():
            commits = bridge.fetch_note_history(guid)
    """

    _instance: Optional["AnkiCollabBridge"] = None
    _lock = threading.Lock()

    def __init__(self):
        self._token: Optional[str] = None
        self._session = None  # requests.Session, lazy-init
        self.simulation_mode = False  # Activé si les requêtes réelles échouent/404

    @classmethod
    def instance(cls) -> "AnkiCollabBridge":
        with cls._lock:
            if cls._instance is None:
                cls._instance = cls()
            return cls._instance

    # ── Disponibilité & Auth ─────────────────────────────────────────────

    def is_available(self) -> bool:
        """True si l'addon AnkiCollab est installé et l'user connecté."""
        return self._get_token() is not None

    def is_addon_installed(self) -> bool:
        """True si l'addon AnkiCollab est installé (même déconnecté)."""
        try:
            if _mw is None:
                return False
            addons = _mw.addonManager.allAddons()
            return _ANKICOLLAB_ADDON_ID in addons
        except Exception:
            return False

    def _get_token(self) -> Optional[str]:
        """Récupère le token d'auth depuis la config de l'addon AnkiCollab."""
        if self._token:
            return self._token
        try:
            if _mw is None:
                return None
            cfg = _mw.addonManager.getConfig(_ANKICOLLAB_ADDON_ID)
            if isinstance(cfg, dict):
                token = cfg.get("token") or cfg.get("auth_token") or cfg.get("api_key")
                if token:
                    self._token = token
                    return token
            # Essayer via l'auth_manager de l'addon
            try:
                import importlib
                mod = importlib.import_module(f"{_ANKICOLLAB_ADDON_ID}.auth_manager")
                if hasattr(mod, "auth_manager"):
                    t = mod.auth_manager.get_token()
                    if t:
                        self._token = t
                        return t
            except Exception:
                pass
        except Exception:
            pass
        return None

    def _get_session(self):
        """Retourne une requests.Session réutilisable (lazy-init)."""
        if self._session is None:
            try:
                import requests
                self._session = requests.Session()
                token = self._get_token()
                if token:
                    self._session.headers.update({
                        "Authorization": f"Bearer {token}",
                        "Content-Type": "application/json",
                        "User-Agent": "AnkiDiffHunk/1.0",
                    })
            except ImportError:
                pass
        return self._session

    def invalidate_token(self) -> None:
        """Réinitialise le token (après déconnexion)."""
        self._token = None
        self._session = None

    # ── Récupération de l'historique ─────────────────────────────────────

    def fetch_note_history(self, guid: str) -> List[CollabCommit]:
        """
        Récupère l'historique des commits d'une note via son GUID Anki.
        Retourne [] en cas d'erreur ou si non connecté.
        """
        session = self._get_session()
        if session is None:
            return []
            
        real_history = []
        try:
            resp = session.get(
                f"{_API_BASE}/note/{guid}/history",
                timeout=_DEFAULT_TIMEOUT,
            )
            if resp.status_code == 200:
                data = resp.json()
                real_history = [self._parse_commit(c) for c in data.get("commits", [])]
        except Exception:
            pass
            
        if real_history:
            self.simulation_mode = False
            return real_history
            
        return []

    def fetch_note_suggestions(self, guid: str) -> List[CollabSuggestion]:
        """
        Récupère les suggestions en attente / acceptées / rejetées pour une note.
        Retourne [] en cas d'erreur.
        """
        session = self._get_session()
        if session is None:
            return []
            
        real_suggs = []
        try:
            resp = session.get(
                f"{_API_BASE}/note/{guid}/suggestions",
                timeout=_DEFAULT_TIMEOUT,
            )
            if resp.status_code == 200:
                data = resp.json()
                real_suggs = [self._parse_suggestion(s) for s in data.get("suggestions", [])]
        except Exception:
            pass
            
        if real_suggs:
            return real_suggs
            
        return []

    def get_note_guid(self, note_id: int) -> Optional[str]:
        """
        Retourne le GUID Anki d'une note via SQL direct (thread-safe).
        Le GUID est stocké dans la colonne `guid` de la table `notes`.
        """
        try:
            if _mw and _mw.col:
                # Utilise SQL direct au lieu de col.get_note() (backend Rust non thread-safe)
                guid = _mw.col.db.scalar(
                    "SELECT guid FROM notes WHERE id = ?", note_id
                )
                return guid or None
        except Exception:
            pass
        return None

    def get_deck_hash_for_note(self, note_id: int) -> Optional[str]:
        """
        Trouve le deck hash AnkiCollab pour cette note locale.
        Utilise l'addon AnkiCollab officiel si disponible.
        Requêtes SQL directes pour être thread-safe (pas de col.get_note()).
        """
        try:
            if _mw is None or _mw.col is None:
                return None

            # Récupérer le did (deck id) de la première carte via SQL
            did = _mw.col.db.scalar(
                "SELECT did FROM cards WHERE nid = ? LIMIT 1", note_id
            )
            if did is None:
                return None

            # Essayer d'importer utils d'AnkiCollab
            import importlib
            try:
                utils = importlib.import_module("1957538407.utils")
                if hasattr(utils, "get_deck_hash_from_did"):
                    deck_hash = utils.get_deck_hash_from_did(did)
                    if deck_hash:
                        return deck_hash
                # Fallback: construire un objet card minimal si nécessaire
                if hasattr(utils, "get_deck_hash_from_card"):
                    card = _mw.col.get_card(
                        _mw.col.db.scalar(
                            "SELECT id FROM cards WHERE nid = ? LIMIT 1", note_id
                        )
                    )
                    if card:
                        deck_hash, _ = utils.get_deck_hash_from_card(card)
                        if deck_hash:
                            return deck_hash
            except ImportError:
                pass
        except Exception as e:
            print("[anki-diff] Error getting deck hash for note:", e)
        return None

    # ── Parsing ──────────────────────────────────────────────────────────

    def _parse_commit(self, raw: Dict[str, Any]) -> CollabCommit:
        fields = raw.get("fields", {})
        if isinstance(fields, str):
            try:
                fields = json.loads(fields)
            except Exception:
                fields = {}
        ts_str = raw.get("timestamp") or raw.get("created_at") or ""
        try:
            ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        except Exception:
            ts = datetime.now()
        return CollabCommit(
            commit_hash=raw.get("hash") or raw.get("id") or "",
            message=raw.get("message") or raw.get("comment") or "",
            author=raw.get("author") or raw.get("username") or "Inconnu",
            timestamp=ts,
            fields=fields,
        )

    def _parse_suggestion(self, raw: Dict[str, Any]) -> CollabSuggestion:
        fields = raw.get("fields", {})
        if isinstance(fields, str):
            try:
                fields = json.loads(fields)
            except Exception:
                fields = {}
        ts_str = raw.get("created_at") or raw.get("timestamp") or ""
        try:
            ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        except Exception:
            ts = datetime.now()
        return CollabSuggestion(
            suggestion_id=str(raw.get("id") or raw.get("suggestion_id") or ""),
            author=raw.get("author") or raw.get("username") or "Inconnu",
            created_at=ts,
            status=raw.get("status") or "pending",
            fields=fields,
            vote_count=int(raw.get("vote_count") or raw.get("votes") or 0),
            comment=raw.get("comment") or raw.get("message") or "",
        )

    # ── Helpers ──────────────────────────────────────────────────────────

    def get_connection_status(self) -> str:
        """Retourne une string de statut pour l'UI."""
        if not self.is_addon_installed():
            return "AnkiCollab non installé"
        token = self._get_token()
        if not token:
            return "Non connecté à AnkiCollab"
        return "Connecté"
