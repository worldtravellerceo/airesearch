import { notFound } from "next/navigation";

import { BoardShell } from "@/components/BoardShell";
import { BOARDS, type Board } from "@/lib/types";

export const dynamic = "force-static";

export function generateStaticParams() {
  // `fresh` lives at the root, so it is not duplicated here.
  return BOARDS.filter((board) => board !== "fresh").map((board) => ({ board }));
}

export default async function BoardPage({
  params,
}: {
  params: Promise<{ board: string }>;
}) {
  const { board } = await params;
  if (!(BOARDS as readonly string[]).includes(board)) notFound();
  return <BoardShell board={board as Board} />;
}
