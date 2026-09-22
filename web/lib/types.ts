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
  /** Daily star gains, dense from `sparkline_from` to the export date. A null
   *  inside it is a day with no row, which is not the same as a day with no
   *  stars. */
  sparkline: Array<number | null>;
  /** Where in the 90-day window the series starts, so a repo with two recorded
   *  days is not drawn as wide as one with ninety. */
  sparkline_from: string | null;
  /** measured | too_young | no_baseline — two of acceleration's values are
   *  produced by the metric code rather than observed. */
  acceleration_basis: string | null;
  history_backfilled_through: string | null;
};

export type BoardFile = {
  board: Board;
  category: string;
  as_of: string | null;
  entries: BoardEntry[];
};

export type CategoryFile = {
  categories: CategoryRow[];
  /** Per board, how many repositories that board was allowed to rank in each
   *  category. The chips used to print the size of the category across the
   *  whole universe next to a filter that returns at most fifty rows. */
  pools: Record<string, Record<string, number>>;
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
    ai_measured: number;
    ai_unsettled: number;
    backfilled: number;
    day_rows: number;
    pools: Partial<Record<Board, number>>;
  };
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

export type HistoryPoint = {
  date: string;
  stars_gained: number;
  cumulative: number;
};

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
  /** measured | too_young | no_baseline. See BoardEntry. */
  acceleration_basis: string | null;
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
  /** How many points the separate history file holds. The curve itself is
   *  fetched by the browser, not inlined into the page. */
  history_points: number;
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
  /** Absent on a site built before the company universe existed, and empty
   *  until a source has actually run. Both mean the same thing to the reader:
   *  no company tabs. */
  companies?: CompanyBoardSummary[];
};

/** One row of a company board.
 *
 *  Deliberately loose: the eight boards answer different questions and share
 *  only the company they are about, so a single strict shape would be a lie in
 *  seven of the eight cases. The column spec for each board names the fields
 *  it actually reads.
 */
export type CompanyEntry = {
  domain?: string | null;
  name?: string | null;
  top_repo?: string | null;
  repo_stars?: number | null;

  // funded
  company_name?: string | null;
  company_domain?: string | null;
  round_type?: string | null;
  amount_usd?: number | null;
  announced_on?: string | null;
  investors?: string | null;
  source?: string | null;
  source_url?: string | null;

  // raised / valuation
  total_usd?: number | null;
  rounds?: number | null;
  last_round?: string | null;
  last_round_on?: string | null;
  employee_range?: string | null;
  country?: string | null;
  valuation_usd?: number | null;
  valuation_src?: string | null;
  valuation_on?: string | null;

  // acquisitions
  acquirer?: string | null;
  target?: string | null;

  // traffic
  month?: string | null;
  visits?: number | null;
  prev_visits?: number | null;
  growth?: number | null;
  global_rank?: number | null;
  category?: string | null;
  traffic_genai?: number | null;

  // g2
  product_slug?: string | null;
  reviews?: number | null;
  avg_rating?: number | null;
};

export type CompanyBoardSummary = {
  slug: string;
  title: string;
  blurb: string;
  count: number;
};

export type CompanyBoardFile = {
  slug: string;
  title: string;
  blurb: string;
  as_of: string | null;
  entries: CompanyEntry[];
};

/** Every company board the exporter knows how to write.
 *
 *  The tabs still come from the manifest — only the boards that have rows are
 *  offered — but the routes are built from this list. A static export refuses
 *  to build a dynamic segment with nothing in it, and a page that exists
 *  without being linked claims nothing: it is there so a bookmarked URL keeps
 *  working through a run where that source happened to return no rows.
 */
export const COMPANY_BOARD_SLUGS = [
  "funded",
  "valuation",
  "raised",
  "acquired",
  "traffic",
  "rising",
  "ai-traffic",
  "rated",
] as const;
