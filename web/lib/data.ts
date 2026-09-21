/** Build-time reads of the exported JSON.
 *
 *  Server-side only: the site is statically exported, so these run once during
 *  the build and the result is baked into the HTML. Missing files are not an
 *  error — the very first deploy happens before any data exists, and the site
 *  should say so rather than fail to build.
 */

import { readFile } from "node:fs/promises";
import path from "node:path";

import { ALL_CATEGORIES, BOARDS } from "@/lib/types";
import type {
  BoardFile,
  CategoryRow,
  CompanyBoardFile,
  CompanyBoardSummary,
  IndexEntry,
  Manifest,
  Overview,
  RepoDetail,
} from "@/lib/types";

const DATA_DIR = path.join(process.cwd(), "public", "data");

async function readJson<T>(relative: string, fallback: T): Promise<T> {
  try {
    return JSON.parse(await readFile(path.join(DATA_DIR, relative), "utf8")) as T;
  } catch {
    return fallback;
  }
}

export const EMPTY_MANIFEST: Manifest = {
  as_of: null,
  boards: [],
  categories: [],
  repos: [],
};

export function getManifest(): Promise<Manifest> {
  return readJson("manifest.json", EMPTY_MANIFEST);
}

export function getOverview(): Promise<Overview | null> {
  return readJson<Overview | null>("overview.json", null);
}

export async function getCategories(): Promise<CategoryRow[]> {
  return (await readJson("categories.json", { categories: [] as CategoryRow[] }))
    .categories;
}

export async function getSearchIndex(): Promise<IndexEntry[]> {
  return (await readJson("index.json", { repos: [] as IndexEntry[] })).repos;
}

export function getBoard(board: string, category: string): Promise<BoardFile | null> {
  return readJson<BoardFile | null>(`boards/${board}/${category}.json`, null);
}

/** How many entries each board holds.
 *
 *  The tabs need this: a board can be legitimately empty for a while — Fresh
 *  Power ranks nothing until repos have been backfilled — and a visitor who
 *  lands on it has to be able to see that the others are not.
 */
export async function getBoardCounts(): Promise<Record<string, number>> {
  const counts: Record<string, number> = {};
  for (const board of BOARDS) {
    counts[board] = (await getBoard(board, ALL_CATEGORIES))?.entries.length ?? 0;
  }
  return counts;
}

export function getRepo(owner: string, name: string): Promise<RepoDetail | null> {
  return readJson<RepoDetail | null>(`repos/${owner}/${name}.json`, null);
}

export async function getCompanyBoards(): Promise<CompanyBoardSummary[]> {
  return (await getManifest()).companies ?? [];
}

export function getCompanyBoard(slug: string): Promise<CompanyBoardFile | null> {
  return readJson<CompanyBoardFile | null>(`companies/${slug}.json`, null);
}
