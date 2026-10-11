"""
diff.py — Diff engine for Anki Import Diff add-on.

Provides two levels of diffing:
  1. highlight_diff(old, new) -> HTML string  (used by the plain HTML preview)
  2. compute_hunks(old, new) -> List[DiffHunk]  (used by the per-hunk UI)
"""

import difflib
import html
import re
import uuid
from dataclasses import dataclass, field
from typing import List, Tuple


# ---------------------------------------------------------------------------
# DiffHunk dataclass
# ---------------------------------------------------------------------------

@dataclass
class DiffHunk:
    """Represents one contiguous region of change between old and new text."""
    hunk_id: str
    # Lines/tokens that were removed (present in old, absent in new)
    old_parts: List[str]
    # Lines/tokens that were added (absent in old, present in new)
    new_parts: List[str]
    # Context text immediately before this hunk (for display only)
    context_before: str = ""
    # Context text immediately after this hunk (for display only)
    context_after: str = ""
    # User decision: True = accept the new version, False = keep the original
    accepted: bool = True


# ---------------------------------------------------------------------------
# Token-level hunk computation
# ---------------------------------------------------------------------------

_CONTEXT_CHARS = 60  # characters of context to show around each hunk


def normalize_html_tags(text: str) -> str:
    """Normalise les guillemets et espaces mineurs dans les balises pour éviter les faux diffs."""
    if not text:
        return ""
    def norm_tag(m):
        tag = m.group(0)
        # Standardiser les guillemets simples en doubles pour les attributs
        return re.sub(r"=\s*'([^']*)'", r'="\1"', tag)
    return re.sub(r"<[^>]+>", norm_tag, text)


def _tokenize(text: str) -> List[str]:
    """Découpe le texte en balises HTML atomiques, mots avec apostrophes, espaces ou ponctuation."""
    if not text:
        return []
    return re.findall(r'<[^>]+>|[\w\'-]+|\s+|[^\w\s<>]', text)


def _group_opcodes(opcodes, t1: List[str], t2: List[str]):
    """Fusionne les petits changements contigus (séparés par un simple espace ou mot de liaison court)."""
    grouped_ops = []
    for tag, i1, i2, j1, j2 in opcodes:
        if not grouped_ops:
            grouped_ops.append([tag, i1, i2, j1, j2])
            continue
        last = grouped_ops[-1]
        if tag == "equal":
            grouped_ops.append([tag, i1, i2, j1, j2])
        else:
            if last[0] == "equal":
                eq_text = "".join(t1[last[1]:last[2]])
                # Si le séparateur intermédiaire est court (<= 12 caractères) et sans saut de ligne, fusionner
                if len(grouped_ops) >= 2 and len(eq_text) <= 12 and "\n" not in eq_text:
                    prev_change = grouped_ops[-2]
                    prev_change[0] = "replace"
                    prev_change[2] = i2
                    prev_change[4] = j2
                    grouped_ops.pop()  # Supprimer le bloc égal intermédiaire
                    continue
            grouped_ops.append([tag, i1, i2, j1, j2])
    return grouped_ops


def compute_hunks(old_text: str, new_text: str) -> List[DiffHunk]:
    """
    Compare old_text et new_text au niveau mot/balise intelligent.
    Fusionne les micro-variations contiguës et ignore les différences cosmétiques de balises.
    """
    if old_text == new_text:
        return []

    old_norm = normalize_html_tags(old_text)
    new_norm = normalize_html_tags(new_text)

    if old_norm == new_norm:
        return []

    old_tokens = _tokenize(old_norm)
    new_tokens = _tokenize(new_norm)

    sm = difflib.SequenceMatcher(None, old_tokens, new_tokens, autojunk=False)
    grouped = _group_opcodes(sm.get_opcodes(), old_tokens, new_tokens)

    hunks: List[DiffHunk] = []

    for tag, i1, i2, j1, j2 in grouped:
        if tag == "equal":
            continue

        old_parts = old_tokens[i1:i2]
        new_parts = new_tokens[j1:j2]

        # Contexte avant et après
        ctx_before = ""
        ctx_after = ""

        for ptag, pi1, pi2, pj1, pj2 in grouped:
            if ptag == "equal" and pi2 == i1:
                ctx_before = "".join(old_tokens[pi1:pi2])
                break

        for ntag, ni1, ni2, nj1, nj2 in grouped:
            if ntag == "equal" and ni1 == i2:
                ctx_after = "".join(old_tokens[ni1:ni2])
                break

        if len(ctx_before) > _CONTEXT_CHARS:
            ctx_before = "…" + ctx_before[-_CONTEXT_CHARS:]
        if len(ctx_after) > _CONTEXT_CHARS:
            ctx_after = ctx_after[:_CONTEXT_CHARS] + "…"

        hunks.append(DiffHunk(
            hunk_id=uuid.uuid4().hex,
            old_parts=old_parts,
            new_parts=new_parts,
            context_before=ctx_before,
            context_after=ctx_after,
            accepted=True,
        ))

    return hunks


def apply_hunks(old_text: str, new_text: str, hunks: List[DiffHunk]) -> str:
    """
    Reconstruit le texte mergé selon les décisions de hunks.
    Pour chaque hunk accepté : utilise new_parts.
    Pour chaque hunk rejeté : utilise old_parts.
    """
    if not hunks:
        return new_text

    old_norm = normalize_html_tags(old_text)
    new_norm = normalize_html_tags(new_text)

    old_tokens = _tokenize(old_norm)
    new_tokens = _tokenize(new_norm)

    sm = difflib.SequenceMatcher(None, old_tokens, new_tokens, autojunk=False)
    grouped = _group_opcodes(sm.get_opcodes(), old_tokens, new_tokens)

    hunk_iter = iter(hunks)
    result: List[str] = []

    for tag, i1, i2, j1, j2 in grouped:
        if tag == "equal":
            result.extend(old_tokens[i1:i2])
        else:
            try:
                hunk = next(hunk_iter)
            except StopIteration:
                result.extend(new_tokens[j1:j2])
                continue

            if hunk.accepted:
                result.extend(new_tokens[j1:j2])
            else:
                result.extend(old_tokens[i1:i2])

    return "".join(result)


# ---------------------------------------------------------------------------
# HTML highlighting (used by legacy/fallback preview)
# ---------------------------------------------------------------------------

def highlight_diff(old_text: str, new_text: str) -> str:
    """
    Returns HTML with <span> styling: green for additions, red for deletions.
    Falls back to block diff for very long texts.
    """
    if len(old_text) > 10000 or len(new_text) > 10000:
        return _simple_block_diff(old_text, new_text)

    if len(old_text) >= 500 or len(new_text) >= 500:
        return _token_word_level_diff(old_text, new_text)

    try:
        diff = difflib.ndiff(old_text, new_text)
        result = []
        for char in diff:
            if char.startswith(' '):
                result.append(html.escape(char[2:]))
            elif char.startswith('+'):
                result.append(
                    f'<span style="background-color: #d4edda; color: #155724;">'
                    f'{html.escape(char[2:])}</span>'
                )
            elif char.startswith('-'):
                result.append(
                    f'<span style="background-color:#f8d7da;color:#721c24;'
                    f'text-decoration:line-through;">'
                    f'{html.escape(char[2:])}</span>'
                )
        return "".join(result)
    except RecursionError:
        return _simple_block_diff(old_text, new_text)


def _token_word_level_diff(old_text: str, new_text: str) -> str:
    try:
        old_tokens = re.findall(r'\w+|\s+|[^\w\s]', old_text)
        new_tokens = re.findall(r'\w+|\s+|[^\w\s]', new_text)
        diff = difflib.ndiff(old_tokens, new_tokens)
        result = []
        for token in diff:
            if token.startswith(' '):
                result.append(html.escape(token[2:]))
            elif token.startswith('+'):
                result.append(
                    f'<span style="background-color: #d4edda; color: #155724;">'
                    f'{html.escape(token[2:])}</span>'
                )
            elif token.startswith('-'):
                result.append(
                    f'<span style="background-color: #f8d7da; color: #721c24; '
                    f'text-decoration: line-through;">'
                    f'{html.escape(token[2:])}</span>'
                )
        return "".join(result)
    except RecursionError:
        return _simple_block_diff(old_text, new_text)


def _simple_block_diff(old_text: str, new_text: str) -> str:
    return (
        f'<span style="background-color: #f8d7da; color: #721c24; text-decoration: line-through;">{html.escape(old_text)}</span>'
        f'<span style="background-color: #d4edda; color: #155724;">'
        f'{html.escape(new_text)}</span>'
    )
