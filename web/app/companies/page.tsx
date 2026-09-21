import { CompanyShell } from "@/components/CompanyShell";
import { getCompanyBoards } from "@/lib/data";

export const dynamic = "force-static";

/** The landing tab is whichever board exists first, because which boards exist
 *  depends on which sources have run. Hard-coding one would 404 the entry
 *  point on a build where that source happened to return nothing. */
export default async function CompaniesPage() {
  const boards = await getCompanyBoards();
  return <CompanyShell slug={boards[0]?.slug ?? ""} />;
}
