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
  /** Two Turkish paragraphs written by the summariser: what the project is,
   *  and where it fits into the reader's own work. Null means nobody has
   *  written about this repo yet — which is not the same as the summariser
   *  finding no use for it. That case is a written paragraph with a null
   *  `matched_project`. */
  description_tr: string | null;
  usage_tr: string | null;
  matched_project: string | null;
  relevance: number | null;
};

/** How much a board actually moved since the previous snapshot.
 *
 *  The per-row arrows were always there and were never enough: a repository
 *  climbing 118 places on Momentum is invisible until you scroll past its row.
 */
export type BoardMovement = {
  /** The date compared against — not always yesterday, if a run was missed. */
  since: string | null;
  compared: number;
  entered: number;
  moved: number;
  /** Rows that moved at least 5 places. Fewer places is shuffling, not news. */
  moved_far: number;
  biggest_move: number;
  top_climber: { full_name: string; places: number } | null;
};

export type BoardFile = {
  board: Board;
  category: string;
  as_of: string | null;
  movement?: BoardMovement;
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
  /** The same two paragraphs the board rows carry. See BoardEntry. */
  description_tr: string | null;
  usage_tr: string | null;
  matched_project: string | null;
  relevance: number | null;
  investment_note: string | null;
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
  /** The explosion tabs that have rows. Absent on a site built before they
   *  existed, which reads the same as none: no tabs offered. */
  explosions?: ExplosionBoardSummary[];
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

/** One row of the morning digest. */
export type DigestArrival = {
  full_name: string;
  stars: number;
  description: string | null;
  language: string | null;
  created_at: string | null;
  category: string | null;
  /** Days between the repo's creation and the digest date. */
  age_days: number | null;
  description_tr: string | null;
  usage_tr: string | null;
  matched_project: string | null;
};

export type DigestMover = {
  full_name: string;
  stars: number;
  category: string | null;
  stars_gained: number;
  matched_project: string | null;
};

/** Accelerating against its own recent pace, but short of Breakout's 3x bar. */
export type DigestWarming = {
  full_name: string;
  stars: number;
  category: string | null;
  acceleration: number;
  velocity_14d: number;
  relative_growth_14d: number | null;
  matched_project: string | null;
};

/** How a row on the explosion boards relates to AI. Never "not AI": absence of
 *  evidence is `none_found` (looked, found nothing) or `unchecked` (not looked
 *  yet), and is_ai=0 from the classifier is not a verdict either. */
export type AiTag =
  | "ai_project"
  | "agent_file"
  | "built_statement"
  | "agent_ready"
  | "uses_ai"
  | "mentions_ai"
  | "unsettled"
  | "none_found"
  | "unchecked";

/** Short quotes from the repository's own files — third-party text, shown as
 *  evidence. */
export type AiEvidence = {
  agent_file: string | null;
  built: string | null;
  agent_ready: string | null;
  uses_ai: string | null;
  mentions_ai?: string | null;
  checked_on: string | null;
};

export type ExplosionLevel = {
  max_age_days: number;
  min_stars_outright: number;
  min_stars: number;
  min_window_velocity: number;
  min_lifetime_velocity: number;
  resurgent_min_gain: number;
  resurgent_min_growth: number;
  window_days: number;
};

export type ExplosionEntry = {
  rank: number;
  rank_delta: number | null;
  full_name: string;
  description: string | null;
  language: string | null;
  license: string | null;
  homepage: string | null;
  stars: number;
  forks: number | null;
  created_at: string | null;
  age_days: number | null;
  /** Days the current speed is measured over; null when there is no earlier
   *  capture to measure from, which is not a speed of zero. */
  window_days: number | null;
  gain_window: number | null;
  gain_1d: number | null;
  velocity: number | null;
  lifetime_velocity: number | null;
  is_ai: boolean | null;
  category: string | null;
  ai_tags: AiTag[];
  ai_evidence: AiEvidence | null;
  /** Whether this site wrote a page for the repo. Otherwise it links to GitHub. */
  has_page: boolean;
  description_tr: string | null;
  usage_tr: string | null;
  matched_project: string | null;
};

export type ExplosionBoardSummary = {
  slug: string;
  title: string;
  blurb: string;
  count: number;
};

export type ExplosionBoardFile = {
  slug: string;
  title: string;
  blurb: string;
  as_of: string | null;
  since: string | null;
  level: ExplosionLevel;
  movement?: BoardMovement | null;
  total: number;
  entries: ExplosionEntry[];
  /** Above the level but shaped like a malware lure (no forks, no language,
   *  no licence): named, never linked. Absent on older files. */
  held_back?: { full_name: string; stars: number; age_days: number | null; description: string | null }[];
};

/** Routes are built from this list, not from the manifest, so a bookmarked tab
 *  keeps working on a day it happens to be empty (static export cannot build a
 *  dynamic segment with nothing in it). The tabs shown still come from the
 *  manifest. */
export const EXPLOSION_SLUGS = ["son-90-gun", "bugun-girenler", "yeniden-patlayanlar"] as const;

export type DigestExplosion = Pick<
  ExplosionEntry,
  | "rank"
  | "full_name"
  | "description"
  | "language"
  | "stars"
  | "age_days"
  | "velocity"
  | "lifetime_velocity"
  | "gain_1d"
  | "is_ai"
  | "category"
  | "ai_tags"
  | "has_page"
  | "description_tr"
>;

export type DigestExplosionRow = DigestExplosion & {
  /** Absent on a digest written before the quotes were carried here. */
  ai_evidence?: AiEvidence | null;
};

export type DigestExplosions = {
  date: string | null;
  since: string | null;
  total: number;
  entered_total?: number;
  entered: DigestExplosionRow[];
  level: ExplosionLevel;
};

export type Digest = {
  date: string | null;
  /** Arrivals young enough to be new projects rather than new to us. */
  new_projects: DigestArrival[];
  /** Long-lived repos that only now crossed into the index. */
  newly_tracked: DigestArrival[];
  movers: DigestMover[];
  warming: DigestWarming[];
  /** Absent on a digest written before the explosion boards existed. */
  explosions?: DigestExplosions;
  counts: {
    arrivals_total: number;
    arrivals_shown: number;
    arrival_min_stars: number;
  };
};
