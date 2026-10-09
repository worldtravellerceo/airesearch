import { ExplosionShell } from "@/components/ExplosionShell";
import { getExplosionBoards } from "@/lib/data";

export const dynamic = "force-static";

export const metadata = {
  title: "Patlayanlar — AI Radar",
  description: "Son üç ayda açılmış ve patlayan her GitHub projesi, AI olsun olmasın.",
};

/** The landing tab is the first one written, for the same reason as the
 *  company section: which tabs exist depends on the day's data. */
export default async function ExplosionsPage() {
  const boards = await getExplosionBoards();
  return <ExplosionShell slug={boards[0]?.slug ?? "son-90-gun"} />;
}
