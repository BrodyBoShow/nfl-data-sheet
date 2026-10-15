-- P8 step 3: /web reads the Intel tables (docs/phases/P8.md, D14 amended, D17, D18;
-- grant lines approved by the user 2026-10-15, D20).
--
-- Same boundary as 0026/0033: row scope in RLS policies, column scope in column grants
-- with every column listed, and the `web` views are security_invoker, so they add no
-- privilege of their own. anon can read exactly the columns granted here, through the
-- three views below, and nothing else. Every granted column is rendered except
-- sleeper_adds.player_id, which the view's join and the players policy need (D17).

-- club_rss_items: 5 of 7 columns. Not content_hash, not updated_at.
GRANT SELECT (team, title, link, published_at, feed_url) ON club_rss_items TO anon;
CREATE POLICY club_rss_items_anon_read ON club_rss_items FOR SELECT TO anon USING (true);

-- sleeper_adds: 4 of 8 columns. Not sleeper_id, fetched_at, content_hash, updated_at.
-- player_id isn't rendered: it's granted because the view's join and the players
-- policy below run as anon (D17). Rows: only those placed on a team.
GRANT SELECT (player_id, team, resolution, add_count) ON sleeper_adds TO anon;
CREATE POLICY sleeper_adds_anon_read ON sleeper_adds FOR SELECT TO anon
    USING (team IS NOT NULL AND resolution IN ('player', 'team_defense'));

-- sleeper_adds_fetch: 4 of 8 columns, all rendered by the label (D18).
-- Not id, fetch_limit (diagnostic, D18), content_hash, updated_at.
GRANT SELECT (fetched_at, lookback_hours, returned_count, placed_count)
    ON sleeper_adds_fetch TO anon;
CREATE POLICY sleeper_adds_fetch_anon_read ON sleeper_adds_fetch FOR SELECT TO anon
    USING (true);

-- players: no new columns (0033 already grants player_id, display_name, position).
-- New rows only: a player with a shown Sleeper row, so the view can name the player.
CREATE POLICY players_anon_read_sleeper ON players FOR SELECT TO anon USING (
    EXISTS (SELECT 1 FROM sleeper_adds s
            WHERE s.player_id = players.player_id
              AND s.resolution = 'player' AND s.team IS NOT NULL)
);

CREATE VIEW web.club_rss_items WITH (security_invoker = true) AS
SELECT team, title, link, published_at, feed_url FROM public.club_rss_items;

-- player_id is granted above for the join and the players policy, and isn't in the
-- view: it isn't rendered (D17).
CREATE VIEW web.sleeper_adds WITH (security_invoker = true) AS
SELECT s.team, s.resolution, p.display_name, p.position, s.add_count
FROM public.sleeper_adds s
LEFT JOIN public.players p ON p.player_id = s.player_id;

CREATE VIEW web.sleeper_adds_fetch WITH (security_invoker = true) AS
SELECT fetched_at, lookback_hours, returned_count, placed_count
FROM public.sleeper_adds_fetch;

-- No default ACL exists for schema web (0026), so each new view needs its own grant.
GRANT SELECT ON web.club_rss_items, web.sleeper_adds, web.sleeper_adds_fetch TO anon;
NOTIFY pgrst, 'reload schema';
