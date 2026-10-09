import { notFound } from "next/navigation";

import { ExplosionShell } from "@/components/ExplosionShell";
import { EXPLOSION_SLUGS } from "@/lib/types";

export const dynamic = "force-static";

export function generateStaticParams() {
  return EXPLOSION_SLUGS.map((board) => ({ board }));
}

export default async function ExplosionBoardPage({
  params,
}: {
  params: Promise<{ board: string }>;
}) {
  const { board } = await params;
  if (!(EXPLOSION_SLUGS as readonly string[]).includes(board)) notFound();
  return <ExplosionShell slug={board} />;
}
