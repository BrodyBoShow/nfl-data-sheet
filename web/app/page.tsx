import { notFound, redirect } from "next/navigation";

import { listWeeks } from "@/lib/db";
import { etDay } from "@/lib/format";

// `/` → the first week whose last game day is today or later (ET), else the last week
// in the schedule (P6.md §3). Rendered per request so the target moves with the
// calendar. Q1 itself is still fetch-cached for 10 minutes by lib/db.ts.
export const dynamic = "force-dynamic";

export default async function Home() {
  const weeks = await listWeeks();
  const today = etDay(new Date());
  const target = weeks.find((w) => w.last_gameday !== null && w.last_gameday >= today) ?? weeks.at(-1);
  if (!target) notFound();
  redirect(`/${target.season}/${target.week}`);
}
