import Link from "next/link";

export default function NotFound() {
  return (
    <>
      <h1 className="t-title">Not found</h1>
      <section className="section">
        <p className="ink-2">
          No such season, week, or game in the schedule. <Link href="/">Current week</Link>
        </p>
      </section>
    </>
  );
}
