import { BoardShell } from "@/components/BoardShell";

export const dynamic = "force-static";

export default function HomePage() {
  return <BoardShell board="fresh" />;
}
