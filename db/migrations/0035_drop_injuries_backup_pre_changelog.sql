-- Drop the pre-changelog injuries copy (docs/phases/P6.md §8 Q3: "drop it in a later
-- migration, after Sunday 2026-09-27's slate runs clean through the changelog path";
-- user instruction 2026-10-14, docs/phases/P8.md "Storage context"). 32 MB, 25,653 rows.
-- Checked 2026-10-14 before dropping: no view, rule, foreign key, function or anon/
-- authenticated grant depends on it, and no pipeline, script, test or workflow reads it
-- (scripts/backfill_injuries_changelog.py only creates it). Its one later use was P3's
-- one-off replay check (2026-09-25). A gzipped CSV export (row count verified) is kept
-- locally, outside the repo, at data/backups/injuries_backup_pre_changelog_2026-10-14.csv.gz.
DROP TABLE injuries_backup_pre_changelog;
