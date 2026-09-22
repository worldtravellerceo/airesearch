"""Two Turkish paragraphs per repository, written by Claude over the Batch API.

The site used to show one line about a project: GitHub's own English
`description`. Understanding what something was meant clicking through, usually
all the way to GitHub. This module writes what the list itself should have
said — for every repository the site actually renders, biggest first.

Each repository gets two paragraphs:

- **`description_tr`** — what the project is, neutrally. No reader, no pitch.
- **`usage_tr`** — where it fits into the reader's own work, named project by
  named project, or plainly that it does not fit anywhere.

The second one is the whole point and the hard half. It is written against a
profile of the reader that is held as a GitHub Actions secret and never
committed, because this repository is public. With no profile there is no
second paragraph and the command fails loudly; a generic one would be worse
than none, because it reads like an answer.

Three cost decisions, following `classify/llm.py`:

- **The Batch API**, for its 50% discount. Nothing here is latency sensitive:
  it runs after scoring and before the site is rebuilt.
- **Five repositories per request**, not the classifier's fifteen. Each one
  costs roughly 600 output tokens, so fifteen would not fit in one response,
  and the writing thins out towards the end of a long list.
- **A content hash per repository**, so a repository whose README, metadata and
  profile have not changed is never paid for twice. After the first full sweep
  a daily run costs cents.

The dollar ceiling is ours, not the provider's. The Batch API has no
server-side equivalent of Apify's `maxTotalChargeUsd`, so the guard is a
pre-flight estimate that refuses to submit, plus a row in `llm_run` written
before the job starts. `max_tokens` is the only ceiling the server enforces,
and it bounds the dominant cost.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)

DEFAULT_MODEL = "claude-sonnet-5"
REPOS_PER_REQUEST = 5
MAX_TOKENS = 8000
POLL_INTERVAL_SECONDS = 30
DEFAULT_TIMEOUT_SECONDS = 6 * 60 * 60

# This is bulk writing against a fully specified contract rather than a problem
# to be solved, so the effort knob starts low. It is the first thing to raise
# if the hand-read of a random slice comes back thin.
EFFORT = "low"

# Claude Sonnet 5 list prices, halved by the Batch API.
INPUT_USD_PER_MTOK = 2.00
OUTPUT_USD_PER_MTOK = 10.00
BATCH_DISCOUNT = 0.5

# Bumped when the instructions below change in a way that should rewrite text
# already on the site. The profile's own hash is mixed into the content hash
# separately, so editing the profile re-runs the second paragraph by itself.
SUMMARY_VERSION = "1"

INSTRUCTIONS = """Yukarıdaki profil, yazdığın metinleri okuyacak kişiyi tanıtıyor.
Sana bir grup GitHub reposu verilecek. Her biri için iki Türkçe paragraf yaz.

**description_tr — projenin tanıtımı (4-6 cümle).** Ne yapar, kim için, hangi
problemi çözer; öne çıkan teknik özellikler (stack, desteklediği modeller,
yerel çalışabilirlik, lisans); olgunluk (yıldız sayısı, bakım durumu). Nesnel
ve reklam dilinden uzak. **Bu paragrafta okuyucudan hiç bahsetme.**

**usage_tr — "senin için nerede ve nasıl" (4-7 cümle).** Profilin 4. ve 5.
bölümlerinden en güçlü bir veya iki eşleşmeyi seç ve **proje adıyla** belirt.
Somut ol: hangi adımda, mevcut hangi araçla birlikte. Okuyucunun kriterlerini
değerlendir: Mac'te yerel çalışır mı, API maliyeti getirir mi, kendi
uygulamasına gömülebilir mi, asistanına devredilebilir mi. Kısa bir fayda ve
uyarı ekle. Okuyucuya doğrudan hitap et ("...için kullanabilirsin").

**Dürüstlük kuralı — en önemlisi.** Gerçek bir eşleşme yoksa zorlama.
`matched_project` alanını `null` bırak ve `usage_tr` içinde açıkça şöyle yaz:
"Mevcut projelerinde doğrudan bir kullanım alanı görünmüyor" — ardından en
yakın olası bağlamı bir cümleyle belirt. Yapay ilgi kurmak, sahte eşleşme
üretmekten daha zararlı ve okuyucunun güvenini bitirir. Verilen repoların
çoğunun gerçek bir eşleşmesi olmayacaktır; bu beklenen ve doğru sonuçtur.

`matched_project`: eşleşen projenin profildeki kısa adı (ör. "Lyricdrop",
"Folio", "Arslan Holding"), eşleşme yoksa null.
`relevance`: 0-10 arası; profilin 7.3'teki öncelik sırasını kullan. Eşleşme
yoksa 0-2.
`investment_note`: proje bir ürün olarak değil bir **yatırım fırsatı** olarak
ilgi çekiyorsa (ekip, pazar boşluğu, ticarileşme potansiyeli) bir cümle; aksi
halde null.

**Dil ve ton.** Türkçe yaz; araç, kütüphane ve ürün adlarını İngilizce orijinal
haliyle bırak. Sakin, yetişkin, düz anlatım. "Devrim niteliğinde", "oyunun
kurallarını değiştiren" gibi abartılar ve ünlem yok. Somut fiiller kullan;
"verimliliği artırır" gibi genel cümleler değersizdir.

**Gizlilik.** Profildeki kişisel ve finansal ayrıntıları (bütçe rakamları,
aile, sağlık, kişi isimleri) çıktıya asla kopyalama; yalnızca karar vermek için
kullan. Bu metinler herkese açık bir sitede yayınlanacak. Proje veya ihtiyaç
adı yeterlidir. Profilde olmayan bir bilgi uydurma.

Her repo için sana verilen `id` değerini aynen geri döndür. Yalnızca sana
verilen metinden karar ver; metin karar vermek için çok zayıfsa bunu
`description_tr` içinde söyle ve uydurma."""

RESULT_SCHEMA = {
    "type": "object",
    "properties": {
        "results": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "integer"},
                    "description_tr": {"type": "string"},
                    "usage_tr": {"type": "string"},
                    # Nullable on purpose: a model with no way to say "no
                    # match" invents one, and the profile's own honesty rule
                    # is the first thing that would be lost.
                    "matched_project": {"type": ["string", "null"]},
                    "relevance": {"type": "integer"},
                    "investment_note": {"type": ["string", "null"]},
                },
                "required": [
                    "id",
                    "description_tr",
                    "usage_tr",
                    "matched_project",
                    "relevance",
                    "investment_note",
                ],
                "additionalProperties": False,
            },
        }
    },
    "required": ["results"],
    "additionalProperties": False,
}


class ProfileMissing(RuntimeError):
    """No reader profile on disk.

    Raised rather than falling back to a generic prompt: a summary written
    without the profile still reads like an answer to "where does this fit into
    my work", and would quietly replace the real one on the site.
    """


class SpendCeilingExceeded(RuntimeError):
    """The pre-flight estimate is above what this run is allowed to spend."""


def load_profile(path: str | Path) -> str:
    profile = Path(path)
    if not profile.is_file():
        raise ProfileMissing(
            f"reader profile not found at {profile}. It is held as a GitHub Actions "
            "secret and written to disk at run time; set AIRADAR_PROFILE_PATH or "
            "place the file. Refusing to write summaries without it."
        )
    text = profile.read_text(encoding="utf-8").strip()
    if not text:
        raise ProfileMissing(f"reader profile at {profile} is empty")
    return text


def profile_fingerprint(profile_text: str) -> str:
    """Editing the profile rewrites every second paragraph, and nothing else."""
    return hashlib.sha256(profile_text.encode("utf-8")).hexdigest()[:16]


def build_system_prompt(profile_text: str) -> str:
    return f"{profile_text}\n\n---\n\n{INSTRUCTIONS}"


@dataclass(frozen=True)
class SummaryInput:
    repo_id: int
    full_name: str
    description: str | None = None
    topics: tuple[str, ...] = ()
    language: str | None = None
    license: str | None = None
    stars: int = 0
    readme_excerpt: str = ""
    readme_hash: str = ""

    def content_hash(self, profile_hash: str) -> str:
        """What has to change before this repository is paid for again."""
        parts = [
            SUMMARY_VERSION,
            profile_hash,
            self.full_name,
            self.description or "",
            ",".join(sorted(self.topics)),
            self.language or "",
            self.license or "",
            self.readme_hash or hashlib.sha256(self.readme_excerpt.encode()).hexdigest()[:16],
        ]
        return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:16]

    def render(self, index: int) -> str:
        parts = [f"id: {index}", f"repo: {self.full_name}", f"stars: {self.stars:,}"]
        if self.language:
            parts.append(f"language: {self.language}")
        if self.license:
            parts.append(f"license: {self.license}")
        if self.topics:
            parts.append(f"topics: {', '.join(self.topics[:15])}")
        parts.append(f"description: {self.description or '(none)'}")
        if self.readme_excerpt:
            parts.append(f"readme:\n{self.readme_excerpt}")
        return "\n".join(parts)


@dataclass(frozen=True)
class Summary:
    repo_id: int
    full_name: str
    description_tr: str
    usage_tr: str
    matched_project: str | None
    relevance: int
    investment_note: str | None


@dataclass
class Usage:
    requests: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    errored: int = 0
    unmatched: list[str] = field(default_factory=list)

    @property
    def cost_usd(self) -> float:
        raw = (
            self.input_tokens / 1_000_000 * INPUT_USD_PER_MTOK
            + self.output_tokens / 1_000_000 * OUTPUT_USD_PER_MTOK
        )
        return round(raw * BATCH_DISCOUNT, 4)

    def summary(self) -> str:
        note = f", {len(self.unmatched)} unmatched" if self.unmatched else ""
        errored = f", {self.errored} errored" if self.errored else ""
        return (
            f"{self.requests} batch requests, {self.input_tokens:,} in / "
            f"{self.output_tokens:,} out tokens, ${self.cost_usd:.2f}{errored}{note}"
        )


def chunk(items: list[SummaryInput], size: int = REPOS_PER_REQUEST) -> list[list[SummaryInput]]:
    return [items[i : i + size] for i in range(0, len(items), size)]


def build_user_message(batch: list[SummaryInput]) -> str:
    rendered = "\n\n---\n\n".join(item.render(i) for i, item in enumerate(batch))
    return f"Bu {len(batch)} repo için iki paragrafı yaz.\n\n{rendered}"


def build_params(
    batch: list[SummaryInput], *, system_prompt: str, model: str = DEFAULT_MODEL
) -> dict:
    return {
        "model": model,
        "max_tokens": MAX_TOKENS,
        "system": system_prompt,
        "messages": [{"role": "user", "content": build_user_message(batch)}],
        "output_config": {
            "effort": EFFORT,
            "format": {"type": "json_schema", "schema": RESULT_SCHEMA},
        },
    }


def parse_results(text: str, batch: list[SummaryInput]) -> tuple[list[Summary], list[str]]:
    """Match the model's output back to the repos it was asked about.

    An id the model invented, repeated or skipped is a misalignment. Printing
    one repository's paragraphs under another's name is the worst failure this
    module has, so an unmatched row is reported and left for the next run
    rather than guessed at by position.
    """
    try:
        payload = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        log.warning("summarise: unparseable response for a batch of %d", len(batch))
        return [], [item.full_name for item in batch]

    summaries: list[Summary] = []
    claimed: set[int] = set()
    for entry in payload.get("results") or []:
        index = entry.get("id")
        if not isinstance(index, int) or not 0 <= index < len(batch) or index in claimed:
            continue
        description = (entry.get("description_tr") or "").strip()
        usage = (entry.get("usage_tr") or "").strip()
        if not description or not usage:
            # A blank paragraph is not a summary. Leave it for the next run.
            continue
        claimed.add(index)
        item = batch[index]
        matched = entry.get("matched_project")
        summaries.append(
            Summary(
                repo_id=item.repo_id,
                full_name=item.full_name,
                description_tr=description,
                usage_tr=usage,
                matched_project=(matched.strip() or None) if isinstance(matched, str) else None,
                relevance=_clamp_relevance(entry.get("relevance")),
                investment_note=_clean(entry.get("investment_note")),
            )
        )

    unmatched = [item.full_name for i, item in enumerate(batch) if i not in claimed]
    if unmatched:
        log.warning("summarise: %d of %d repos unmatched in response", len(unmatched), len(batch))
    return summaries, unmatched


def _clamp_relevance(value) -> int:
    try:
        return max(0, min(10, int(value)))
    except (TypeError, ValueError):
        return 0


def _clean(value) -> str | None:
    if not isinstance(value, str):
        return None
    return value.strip() or None


def estimate_cost_usd(
    repo_count: int,
    *,
    repos_per_request: int = REPOS_PER_REQUEST,
    system_tokens: int = 6000,
    input_tokens_per_repo: int = 1200,
    output_tokens_per_repo: int = 600,
) -> float:
    """What a sweep will cost before a single request is submitted.

    The system prompt carries the whole reader profile and is charged once per
    request, which is what batching repositories together buys. This is the
    only ceiling between the run and the bill, so it is deliberately not
    optimistic: the defaults assume a full README excerpt for every repository.
    """
    if repo_count <= 0:
        return 0.0
    requests = -(-repo_count // max(repos_per_request, 1))
    input_tokens = requests * system_tokens + repo_count * input_tokens_per_repo
    output_tokens = repo_count * output_tokens_per_repo
    raw = (
        input_tokens / 1_000_000 * INPUT_USD_PER_MTOK
        + output_tokens / 1_000_000 * OUTPUT_USD_PER_MTOK
    )
    return round(raw * BATCH_DISCOUNT, 4)


class Summariser:
    """Submits one Batch API job and collects its results."""

    def __init__(
        self,
        *,
        profile_text: str,
        api_key: str | None = None,
        model: str = DEFAULT_MODEL,
        repos_per_request: int = REPOS_PER_REQUEST,
        client=None,
        sleep=time.sleep,
    ) -> None:
        self.model = model
        self.repos_per_request = repos_per_request
        self.system_prompt = build_system_prompt(profile_text)
        self.batch_id: str | None = None
        self._sleep = sleep
        if client is not None:
            self._client = client
        else:
            import anthropic

            self._client = anthropic.Anthropic(api_key=api_key or None)

    def run(
        self, inputs: list[SummaryInput], *, timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS
    ) -> tuple[list[Summary], Usage]:
        usage = Usage()
        if not inputs:
            return [], usage

        batches = chunk(inputs, self.repos_per_request)
        self.batch_id = self._submit(batches)
        log.info("summarise: submitted %d requests as batch %s", len(batches), self.batch_id)

        if not self._await_completion(self.batch_id, timeout_seconds):
            usage.unmatched = [item.full_name for item in inputs]
            log.error("summarise: batch %s did not finish within the timeout", self.batch_id)
            return [], usage

        return self._collect(self.batch_id, batches, usage)

    def _submit(self, batches: list[list[SummaryInput]]) -> str:
        from anthropic.types.message_create_params import MessageCreateParamsNonStreaming
        from anthropic.types.messages.batch_create_params import Request

        requests = [
            Request(
                custom_id=f"summary-{i}",
                params=MessageCreateParamsNonStreaming(
                    **build_params(batch, system_prompt=self.system_prompt, model=self.model)
                ),
            )
            for i, batch in enumerate(batches)
        ]
        return self._client.messages.batches.create(requests=requests).id

    def _await_completion(self, batch_id: str, timeout_seconds: int) -> bool:
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            batch = self._client.messages.batches.retrieve(batch_id)
            if batch.processing_status == "ended":
                return True
            self._sleep(POLL_INTERVAL_SECONDS)
        return False

    def _collect(
        self, batch_id: str, batches: list[list[SummaryInput]], usage: Usage
    ) -> tuple[list[Summary], Usage]:
        by_custom_id = {f"summary-{i}": batch for i, batch in enumerate(batches)}
        summaries: list[Summary] = []
        seen: set[str] = set()

        # Results arrive in any order, so they are keyed by custom_id, never by
        # position.
        for result in self._client.messages.batches.results(batch_id):
            batch = by_custom_id.get(result.custom_id)
            if batch is None:
                log.warning("summarise: unknown custom_id %s in results", result.custom_id)
                continue
            seen.add(result.custom_id)
            usage.requests += 1

            if getattr(result.result, "type", None) != "succeeded":
                usage.errored += 1
                usage.unmatched.extend(item.full_name for item in batch)
                continue

            message = result.result.message
            usage.input_tokens += getattr(message.usage, "input_tokens", 0) or 0
            usage.output_tokens += getattr(message.usage, "output_tokens", 0) or 0

            text = next((b.text for b in message.content if b.type == "text"), "")
            matched, unmatched = parse_results(text, batch)
            summaries.extend(matched)
            usage.unmatched.extend(unmatched)

        for custom_id, batch in by_custom_id.items():
            if custom_id not in seen:
                usage.unmatched.extend(item.full_name for item in batch)

        return summaries, usage
