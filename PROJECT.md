# Project: Anki Import Diff

## Architecture
This add-on intercepts Anki's native CSV and APKG import processes when the "Visual Diff Import" checkbox is enabled. It performs a dry-run import within an SQLite savepoint, captures changes to note fields, displays a visual diff/merge dialog to the user, and selectively commits the accepted changes to the Anki database.

### Module Boundaries
1. **`__init__.py`**: Add-on entry point, initializes hooks and UI modifications.
2. **`src/hooks.py`**: Intercepts the CSV and APKG import panels to inject the checkbox and hijack the import execution.
3. **`src/savepoint.py`**: SQLite savepoint wrapper. Wraps Anki collection DB transaction, executes the import inside the savepoint, diffs the modified notes, and executes rolls backs.
4. **`src/diff.py`**: Utility to compute and render HTML diffs (additions in light green, deletions in light red).
5. **`src/ui.py`**: PyQt modal dialog showing modified notes, field editor layouts, raw HTML toggle, and merge/commit controls.

### Data Flow
1. User clicks "Import" → Dialog has "Visual Diff Import" checked.
2. Import triggers → `hooks.py` intercepts.
3. `savepoint.py` establishes `SAVEPOINT import_diff` on `mw.col.db`.
4. Native import executes within the savepoint.
5. `savepoint.py` queries `notes` table before/after states, computes changed notes/fields, and rolls back the savepoint.
6. `ui.py` displays differences. User modifies, accepts, or rejects field-level updates.
7. Accepted values are written to the database using `mw.col.db` updates and committed.

## Code Layout
- `__init__.py`: Main package entry point.
- `src/__init__.py`
- `src/hooks.py`: Interception & UI checkbox injection.
- `src/savepoint.py`: SQLite transaction savepoint dry-run capturing logic.
- `src/diff.py`: Text/HTML diffing engine.
- `src/ui.py`: Qt visual diff dialog and editor.
- `test_import_diff.py`: Python unit/E2E test suite (mocking CSV/APKG import, testing savepoint rollback/commits, diff merging).

## Milestones
| # | Name | Scope | Dependencies | Status |
|---|------|-------|-------------|--------|
| M1 | Exploration & Hook Setup | Investigate Anki hooks and import API, set up add-on skeleton | None | DONE |
| M2 | E2E Test Suite | Create comprehensive requirement-driven test cases (Tiers 1-4) | None | DONE |
| M3 | Savepoint Dry-Run Capture | Implement SQLite savepoint and before/after change capture | M1, M2 | DONE |
| M4 | Diff UI & Merge Controls | Build visual diff explorer dialog with PyQt, HTML toggles, and edit fields | M1, M2 | DONE |
| M5 | Database Commit & Rollback | Connect UI choices to savepoint results, write updates to collection DB, cancel cleanup | M3, M4 | DONE |
| M6 | E2E Verification & Hardening | Pass E2E tests, run Challenger/Auditor, and perform adversarial coverage hardening | M5 | DONE |

## Interface Contracts
### `src.savepoint` Interface
- `capture_import_changes(import_fn, *args, **kwargs) -> List[NoteDiff]`: Executes `import_fn` within a savepoint, detects differences, rolls back, and returns list of changed notes.
- `NoteDiff`: dataclass or dict containing `note_id`, `original_fields` (dict), `updated_fields` (dict), `model_name`, `field_names` (list).

### `src.diff` Interface
- `highlight_diff(old_text: str, new_text: str) -> str`: Compares old and new text, returning HTML with `<span>` styling for red deletions/green additions.

### `src.ui` Interface
- `show_diff_dialog(note_diffs: List[NoteDiff]) -> Optional[List[NoteDiff]]`: Opens the modal dialog and returns the user-modified/merged note diffs if accepted, or `None` if cancelled.
