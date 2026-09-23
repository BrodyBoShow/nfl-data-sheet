-- P4 environment: IANA time zone per venue (e.g. 'America/Phoenix'), for the Environment
-- analyst's timezone-crossing signal (home venue tz vs. game venue tz). A zone, not a
-- fixed UTC offset, so DST -- and Arizona's lack of it -- resolves per kickoff date.
--
-- Values live in reference/stadiums.csv (hand-entered by city, cross-checked against
-- Open-Meteo's timezone=auto for each row's coords), loaded by the stadiums collector.
-- Nullable here only because existing rows are filled by the next collector run after
-- this migration; the collector's validation requires a loadable zone on every row.

ALTER TABLE stadiums ADD COLUMN tz text;
