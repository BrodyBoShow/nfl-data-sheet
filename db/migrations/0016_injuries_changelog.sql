-- injuries moves from "store every poll" to a change-log: the collector now writes a
-- row only on first appearance or when team/designation/body_part/notes actually
-- changes (see pipeline/core/injury_changelog.py). is_cleared marks the terminal event
-- when a player disappears from a source's feed -- distinguishing "no longer listed"
-- from "not polled". On a cleared row, designation/body_part/notes are NULL (enforced
-- below); the last-known designation is preserved only inside raw's synthetic marker.
--
-- transactions is dropped: nothing in pipeline/ ever read it (grepped, confirmed), and
-- it only ever tracked team/designation changes -- a strict subset of what the injuries
-- change-log now records directly.

ALTER TABLE injuries ADD COLUMN is_cleared boolean NOT NULL DEFAULT false;

ALTER TABLE injuries ADD CONSTRAINT injuries_cleared_fields_null
    CHECK (NOT is_cleared OR (designation IS NULL AND body_part IS NULL AND notes IS NULL));

DROP TABLE transactions;
