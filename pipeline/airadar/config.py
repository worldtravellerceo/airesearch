"""Runtime configuration, loaded from environment variables / .env."""

from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# The REST API version that exposes GET /repos/{owner}/{repo}/stargazers/history.
# The legacy stargazer-listing endpoints were restricted to admins/collaborators in
# July 2026; this endpoint is the privacy-safe replacement and the only supported
# source of historical star counts.
GITHUB_API_VERSION = "2026-03-10"
GITHUB_API_ROOT = "https://api.github.com"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- credentials -------------------------------------------------------
    # A personal access token, NOT the Actions GITHUB_TOKEN: the latter is capped
    # at 1,000 requests/hour per repository, which is far too low for this workload.
    github_token: str = Field(default="", alias="GH_PAT")
    # Extra tokens are optional and purely about throughput. The primary rate
    # limit is per token, so a second and third one triple the ceiling: the
    # backfill that took 148 minutes spent almost all of it waiting for quota,
    # with the CPU idle. Adding tokens is the only lever that moves that wall,
    # since no amount of hardware buys a higher limit.
    github_token_2: str = Field(default="", alias="GH_PAT_2")
    github_token_3: str = Field(default="", alias="GH_PAT_3")
    anthropic_api_key: str = Field(default="", alias="ANTHROPIC_API_KEY")
    # Apify runs the scrapers behind the company universe and bills per result.
    apify_token: str = Field(default="", alias="APIFY_TOKEN")

    @property
    def github_tokens(self) -> list[str]:
        """Every configured token, in order, without blanks or duplicates.

        A duplicate would be worse than useless: two lanes sharing one bucket
        would each believe they had the full allowance and run it dry together.
        """
        seen: list[str] = []
        for token in (self.github_token, self.github_token_2, self.github_token_3):
            token = token.strip()
            if token and token not in seen:
                seen.append(token)
        return seen

    # --- storage -----------------------------------------------------------
    # A SQLite file that lives in the repository. Keeping the data here rather
    # than in a hosted database is what removes the last account from the setup.
    db_path: str = Field(default="data/airadar.db", alias="AIRADAR_DB")

    # --- discovery ---------------------------------------------------------
    min_stars: int = Field(default=50, alias="AIRADAR_MIN_STARS")
    # The census enumerates GitHub outright above this many stars, with no topic
    # or keyword filter. Every other channel is a heuristic that can miss a
    # project; 98% of the corpus arrived through the topic sweep, which cannot
    # see a repo that has no topics — and `karpathy/nanoGPT`,
    # `facebookresearch/faiss` and `deepseek-ai/DeepSeek-R1` have none. This is
    # the channel that makes a gap like that impossible rather than unlikely.
    # It only has to reach below `track_limit`'s star cutoff to guarantee the
    # boards, which is why it is not simply set to `min_stars`.
    census_min_stars: int = Field(default=1000, alias="AIRADAR_CENSUS_MIN_STARS")
    # How far back the nursery channel looks. The census floor is a thousand
    # stars, and below it the only channel that runs is the topic sweep —
    # which measured half of the 300-1,000 band as carrying no topics at all.
    # Ninety days is where a repository that will cross the floor has usually
    # started: the band's median takes twelve days to reach 300.
    nursery_days: int = Field(default=90, alias="AIRADAR_NURSERY_DAYS")
    # Search returns at most 1,000 results per query, so any star bucket that fills
    # up has to be split. Leave headroom under the cap.
    search_page_cap: int = 1000
    bucket_split_threshold: int = 900

    # --- collection --------------------------------------------------------
    # Repos ranked inside the top N by stars are refreshed daily; the rest weekly.
    tier1_size: int = Field(default=3000, alias="AIRADAR_TIER1_SIZE")
    # How many repos we collect metrics for at all. Discovery finds far more;
    # this is what keeps the database small enough to live in the repository.
    # A riser below the cut enters the tracked set at the next weekly discovery,
    # which re-reads everyone's star counts.
    track_limit: int = Field(default=12000, alias="AIRADAR_TRACK_LIMIT")
    # Days of per-day detail retained; older rows fold into fresh_power_tail.
    retain_days: int = Field(default=120, alias="AIRADAR_RETAIN_DAYS")
    user_agent: str = "airadar/0.1 (+https://github.com/worldtravellerceo/airesearch)"
    # How many requests are in flight at once. Every run so far was sequential:
    # one request, wait for the round trip, next request. At ~250ms that is four
    # requests a second no matter how much quota is left, which is why a
    # 17,799-request backfill took 148 minutes. GitHub asks for no more than 100
    # concurrent requests; a dozen is far inside that and already enough to keep
    # three tokens' worth of quota saturated.
    concurrency: int = Field(default=12, alias="AIRADAR_CONCURRENCY")

    # --- companies ---------------------------------------------------------
    # The month's ceiling on Apify spend. Every run is started with a share of
    # what is left as Apify's own `maxTotalChargeUsd`, so this holds even if the
    # code asking for the run is wrong. The Starter plan includes $19 of usage;
    # the rest is overage the account is willing to pay.
    apify_monthly_cap_usd: float = Field(default=80.0, alias="AIRADAR_APIFY_CAP")
    # Companies below this many repository stars are not worth paying to look
    # up yet. 9,682 seeds come out of the corpus; 2,961 clear a thousand stars.
    company_min_stars: int = Field(default=1000, alias="AIRADAR_COMPANY_MIN_STARS")

    # --- scoring -----------------------------------------------------------
    # Half-life in days for `fresh_power`: a star contributes half as much after
    # this many days. 180d means five-year-old stars are effectively worthless,
    # which is the whole point of the metric.
    fresh_power_half_life_days: float = Field(default=180.0, alias="AIRADAR_FRESH_HALF_LIFE")

    # --- classification ----------------------------------------------------
    classifier_model: str = Field(default="claude-haiku-4-5", alias="AIRADAR_CLASSIFIER_MODEL")
    # Rule-engine confidence band that gets escalated to the LLM. Anything outside
    # the band is decided for free.
    llm_band_low: float = 0.2
    llm_band_high: float = 0.8

    # --- summaries ---------------------------------------------------------
    # The two Turkish paragraphs the site shows for each repository. Sonnet
    # rather than the classifier's Haiku: the second paragraph has to weigh a
    # project against a reader's actual work and say plainly when there is no
    # match, and a cheaper model reaches for a match that is not there.
    summary_model: str = Field(default="claude-sonnet-5", alias="AIRADAR_SUMMARY_MODEL")
    # The reader profile the second paragraph is written for. It holds personal
    # and financial detail, so it is delivered as a GitHub Actions secret and
    # written to disk at run time; this repository is public and never carries
    # it. With no profile there is no second paragraph, and the command says so
    # rather than inventing a generic one.
    profile_path: str = Field(default="SAM_PROFILE.md", alias="AIRADAR_PROFILE_PATH")


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings
