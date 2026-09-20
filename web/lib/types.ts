export const BOARDS = ["fresh", "momentum", "breakout", "popular"] as const;
export type Board = (typeof BOARDS)[number];

export const ALL_CATEGORIES = "_all";

/** What each board answers, in plain words. Shown under the tabs so nobody has
 *  to guess why four lists of the same repositories disagree. */
export const BOARD_COPY: Record<Board, { title: string; blurb: string }> = {
  fresh: {
    title: "Fresh Power",
    blurb:
      "Yıldızlar yaşına göre değersizleşiyor (yarılanma 180 gün). 5 yılda birikmiş 50 bin yıldız, 2 haftada gelen 50 bine yenilir.",
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

export type BoardEntry = {
  rank: number;
  score: number;
  rank_delta: number | null;
  repo_id: number;
  full_name: string;
  description: string | null;
  language: string | null;
  stars: number;
  archived: boolean;
  category: string | null;
  one_liner: string | null;
  velocity_14d: number | null;
  acceleration: number | null;
  relative_growth_14d: number | null;
  fresh_power: number | null;
  momentum_score: number | null;
  breakout: boolean;
  days_to_1k: number | null;
  days_to_10k: number | null;
  days_to_50k: number | null;
  coverage_days: number | null;
  sparkline: number[];
};

export type BoardFile = {
  board: Board;
  category: string;
  as_of: string | null;
  entries: BoardEntry[];
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
  counts: { tracked: number; ai_repos: number; backfilled: number; day_rows: number };
  last_run: {
    command: string;
    finished_at: string | null;
    ok: boolean | null;
    api_calls: number;
    api_304s: number;
    llm_cost_usd: number;
    notes: string | null;
  } | null;
};

export type HistoryPoint = { date: string; stars_gained: number; cumulative: number };

export type RepoDetail = {
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
  discovered_via: string | null;
  history_backfilled_through: string | null;
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
  breakout: boolean;
  coverage_days: number | null;
  topics: string[];
  ranks: Partial<Record<Board, number>>;
  history: HistoryPoint[];
};

export type IndexEntry = {
  full_name: string;
  description: string | null;
  stars: number;
  language: string | null;
  category: string | null;
  one_liner: string | null;
};

export type Manifest = {
  as_of: string | null;
  boards: string[];
  categories: string[];
  repos: string[];
};
