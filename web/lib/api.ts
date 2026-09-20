/** Typed client for the AI Radar read API. */

export const API_BASE =
  process.env.API_BASE ?? process.env.NEXT_PUBLIC_API_BASE ?? "http://127.0.0.1:8000";

export const BOARDS = ["fresh", "momentum", "breakout", "popular"] as const;
export type Board = (typeof BOARDS)[number];

/** What each board answers, in the user's words. Shown as the tab's subtitle so
 *  nobody has to guess why four lists of the same repos disagree. */
export const BOARD_COPY: Record<Board, { title: string; blurb: string }> = {
  fresh: {
    title: "Fresh Power",
    blurb:
      "Yıldızlar yaşına göre değersizleşiyor (yarılanma 180 gün). 5 yılda birikmiş 50 bin star, 2 haftada gelen 50 bine yenilir.",
  },
  momentum: {
    title: "Momentum",
    blurb: "Son 14 günün günlük yıldız hızı. Şu anda en hızlı büyüyenler.",
  },
  breakout: {
    title: "Breakout",
    blurb:
      "Kendi 90 günlük temposunun en az 3 katına çıkmış ve anlamlı hızda olan projeler. Yeni patlayanlar.",
  },
  popular: {
    title: "Popüler",
    blurb: "Toplam yıldız — klasik sıralama. Yaşı ödüllendirir, bugünü değil.",
  },
};

export type LeaderboardEntry = {
  rank: number;
  score: number;
  rank_delta: number | null;
  repo_id: number;
  full_name: string;
  owner: string;
  name: string;
  description: string | null;
  homepage: string | null;
  language: string | null;
  license: string | null;
  stars: number;
  archived: boolean;
  created_at: string | null;
  category: string | null;
  subcategory: string | null;
  one_liner: string | null;
  velocity_7d: number | null;
  velocity_14d: number | null;
  velocity_28d: number | null;
  velocity_90d: number | null;
  acceleration: number | null;
  relative_growth_14d: number | null;
  fresh_power: number | null;
  momentum_score: number | null;
  peak_velocity: number | null;
  days_since_peak: number | null;
  days_to_1k: number | null;
  days_to_10k: number | null;
  days_to_50k: number | null;
  breakout: boolean | null;
  coverage_days: number | null;
  sparkline: number[] | null;
};

export type LeaderboardResponse = {
  board: Board;
  category: string;
  as_of: string | null;
  entries: LeaderboardEntry[];
};

export type CategoryRow = {
  category: string;
  repos: number;
  stars: number;
  velocity_14d: number | null;
  breakouts: number;
};

export type Overview = {
  as_of: string | null;
  counts: {
    tracked: number;
    ai_repos: number;
    backfilled: number;
    day_rows: number;
  };
  last_run: {
    command: string;
    finished_at: string | null;
    ok: boolean | null;
    api_calls: number;
    llm_cost_usd: number;
    notes: string | null;
  } | null;
};

export type HistoryPoint = {
  date: string;
  stars_gained: number;
  cumulative: number;
  day_index?: number | null;
};

export type RepoDetail = LeaderboardEntry & {
  topics: string[] | null;
  discovered_via: string | null;
  history_backfilled_through: string | null;
  first_seen_at: string | null;
  ranks: Partial<Record<Board, number>>;
};

export type CompareSeries = {
  repo: string;
  created_at: string | null;
  stars: number;
  category: string | null;
  points: HistoryPoint[];
};

export class ApiError extends Error {
  constructor(
    readonly status: number,
    message: string,
  ) {
    super(message);
  }
}

async function get<T>(path: string, revalidate = 900): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, {
    next: { revalidate },
    headers: { accept: "application/json" },
  });
  if (!response.ok) {
    let detail = response.statusText;
    try {
      detail = (await response.json())?.detail ?? detail;
    } catch {
      /* the body was not JSON; the status text will have to do */
    }
    throw new ApiError(response.status, detail);
  }
  return response.json() as Promise<T>;
}

export const api = {
  overview: () => get<Overview>("/api/overview"),
  categories: () => get<{ categories: CategoryRow[] }>("/api/categories"),
  leaderboard: (params: {
    board: Board;
    category?: string;
    limit?: number;
    language?: string;
    includeArchived?: boolean;
  }) => {
    const query = new URLSearchParams({
      board: params.board,
      category: params.category ?? "_all",
      limit: String(params.limit ?? 200),
    });
    if (params.language) query.set("language", params.language);
    if (params.includeArchived === false) query.set("include_archived", "false");
    return get<LeaderboardResponse>(`/api/leaderboard?${query}`);
  },
  repo: (fullName: string) => get<RepoDetail>(`/api/repos/${fullName}`),
  history: (fullName: string, window: "90d" | "1y" | "2y" | "all" = "all") =>
    get<{ repo: string; window: string; points: HistoryPoint[] }>(
      `/api/repos/${fullName}/history?window=${window}`,
    ),
  compare: (repos: string[], window: "90d" | "1y" | "2y" | "all" = "all") =>
    get<{ window: string; series: CompareSeries[] }>(
      `/api/compare?repos=${encodeURIComponent(repos.join(","))}&window=${window}`,
    ),
  movers: (board: Board = "momentum") =>
    get<{ risers: LeaderboardEntry[]; fallers: LeaderboardEntry[] }>(
      `/api/movers?board=${board}`,
    ),
};
