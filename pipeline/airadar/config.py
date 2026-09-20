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
    database_url: str = Field(default="", alias="DATABASE_URL")
    anthropic_api_key: str = Field(default="", alias="ANTHROPIC_API_KEY")

    # --- discovery ---------------------------------------------------------
    min_stars: int = Field(default=50, alias="AIRADAR_MIN_STARS")
    # Search returns at most 1,000 results per query, so any star bucket that fills
    # up has to be split. Leave headroom under the cap.
    search_page_cap: int = 1000
    bucket_split_threshold: int = 900

    # --- collection --------------------------------------------------------
    # Repos ranked inside the top N by stars are refreshed daily; the rest weekly.
    tier1_size: int = Field(default=3000, alias="AIRADAR_TIER1_SIZE")
    user_agent: str = "airadar/0.1 (+https://github.com/worldtravellerceo/airesearch)"

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


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings
