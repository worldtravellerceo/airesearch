import { notFound } from "next/navigation";

import { CompanyShell } from "@/components/CompanyShell";
import { COMPANY_BOARD_SLUGS } from "@/lib/types";

export const dynamic = "force-static";

export function generateStaticParams() {
  return COMPANY_BOARD_SLUGS.map((board) => ({ board }));
}

export default async function CompanyBoardPage({
  params,
}: {
  params: Promise<{ board: string }>;
}) {
  const { board } = await params;
  if (!(COMPANY_BOARD_SLUGS as readonly string[]).includes(board)) notFound();
  return <CompanyShell slug={board} />;
}
