import { CompareBoard } from "@/components/CompareBoard";
import { getSearchIndex } from "@/lib/data";

export const dynamic = "force-static";

export default async function ComparePage() {
  return <CompareBoard index={await getSearchIndex()} />;
}
