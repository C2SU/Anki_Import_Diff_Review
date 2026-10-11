# Original User Request

## Initial Request — 2026-06-22T16:50:18Z

An Anki add-on that shows a visual diff hunk view when importing existing cards, inspired by VS Code's diff and merge tools, allowing users to selectively accept or reject updates.

Working directory: c:/Users/Pipou/AppData/Roaming/Anki2/addons21/anki_import_diff
Integrity mode: development

## Requirements

### R1. Import Dialog Option Integration
The add-on must add a "Visual Diff Import" checkbox to Anki's native Import options (such as the CSV and APKG import panels).
- If unchecked, import behaves normally.
- If checked, the add-on intercepts the import process to initiate the review workflow.

### R2. SQLite Savepoint Dry-Run Capture
The add-on must perform a safe "dry-run" import using an SQLite transaction savepoint (e.g., `SAVEPOINT import_diff`).
- It must apply the import inside the savepoint, detect which notes have changed (by comparing the updated state against the original database state), and record the before/after values of all fields.
- It must then rollback the savepoint so that the database is restored to its original state before presenting the diff UI.

### R3. Visual Diff Explorer Dialog
The add-on must open a modal dialog displaying a list of modified notes. For each note:
- It must display the fields using a layout resembling Anki's standard Note Editor (with field labels and the `<>` HTML toggle button).
- The differences within each field must be visually highlighted using standard diff styling (light red background with strikethrough for deleted text, light green background for added text).

### R4. Granular Merge and Edit Controls
For each field/note in the diff explorer:
- Provide controls (e.g., buttons or side-by-side selectors) to accept/reject changes for each field.
- The final field values must remain editable by the user directly (including toggleable HTML editing via the `<>` button).

### R5. Database Commit on Accept
When the user clicks "Apply" or "Confirm", the final selected/merged field values must be committed to the Anki collection database. If the user cancels the dialog, no changes must be written to the database.

## Verification Plan

### Automated Tests
- A Python unit test script (`test_import_diff.py`) that mock-imports CSV files and APKG packages, simulates the diff selection programmatically, and asserts that:
  - Database notes are updated with the correct merged values.
  - Cancelled imports leave the database completely untouched.
  - The SQLite savepoint logic executes and rolls back successfully without database corruption.

### Manual Verification
- Manually run Anki, trigger a CSV/APKG import, check the "Visual Diff Import" box, view the Diff Explorer dialog, accept some changes, reject others, and verify that the card browser displays the correct note fields.

## Acceptance Criteria

### Functionality
- [ ] The "Visual Diff Import" checkbox is visible in both CSV and APKG import pages.
- [ ] Checking the box and importing opens the custom Diff Explorer dialog listing all modified notes.
- [ ] Closing/Cancelling the Diff Explorer dialog results in zero changes to the database.
- [ ] Confirming the Diff Explorer dialog correctly commits only the accepted fields to the database.

### UI & Styling
- [ ] Diff views use a light red background (deletions) and light green background (additions) to clearly highlight text differences.
- [ ] Each field editor has a working `<>` HTML toggle button, and the diff correctly shows raw HTML differences when HTML mode is active.
