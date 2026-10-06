# AI Radar verisi — dışarıdan tüketim

Üç kanal var. Hepsi her gün otomatik yenileniyor, hiçbiri token istemiyor.

| kanal | ne içerir | boyut | ne zaman |
|---|---|---|---|
| **SQLite veritabanı** | her şey, zaman serisi dahil | 202 MB gz | kendi deponu kuracaksan |
| **`feed.ndjson`** | sınıflandırılmış her repo, bugünkü hâli | 84 MB | akışla işleyeceksen |
| **`feed.json` + `feed/<trend>.json`** | 1.000+ yıldızlılar, trende göre bölünmüş | 14,5 MB / 0,2–8 MB | hızlı okuma |

## 1. Veritabanı — en geniş olan

```bash
curl -L https://github.com/worldtravellerceo/airesearch/releases/download/data/airadar.db.gz \
  | gunzip > airadar.db
sqlite3 airadar.db "SELECT count(*) FROM repos"
```

JSON'un taşıyamayacağı şey burada: **geçmiş**.

| tablo | satır | ne |
|---|---|---|
| `repos` | 177.893 | ham GitHub metadata |
| `repo_star_daily` | 2.995.582 | **gün gün yıldız artışı** |
| `repo_star_weekly` | 808.063 | haftalık kovalar (eski dönem) |
| `repo_scores` | 1.191.356 | **her günün skorları** — trendin kendi tarihi |
| `leaderboard_snapshots` | 47.833 | board sıralarının günlük geçmişi |
| `repo_classification` | 177.893 | is_ai + kategori + güven |
| `repo_topics` | 941.468 | topic'ler |
| `repo_summary` | 1.491 | Türkçe iki paragraf |
| `companies`, `funding_round`, `company_g2`, `company_traffic` | 12.985 / 1.000 / 800 / 8.019 | şirket evreni |

"Bu repo 3 ay önce de böyle miydi?" sorusunun cevabı sadece burada.

## 2. `feed.ndjson` — eşiksiz akış

```
https://worldtravellerceo.github.io/airesearch/data/feed.ndjson
```

Satır başına bir JSON nesnesi, saran dizi yok. 78.852 satır — AI olarak sınıflanmış, fork olmayan her repo. **Yıldız eşiği yok.**

```bash
curl -s .../feed.ndjson | duckdb -c "SELECT * FROM read_json_auto('/dev/stdin') WHERE trend='rising'"
# ya da
curl -s .../feed.ndjson | sqlite-utils insert radar.db repos - --nl
```

Ölçüldü: 78.852 satır SQLite'a **1,1 saniyede**, 0 bozuk satır.

## 3. `feed.json` — hızlı yol

1.000+ yıldızlı 11.811 repo, tek dizi. Trend bazlı bölünmüşleri:

```
data/feed/breakout.json    80 repo    199 KB
data/feed/rising.json     227 repo    476 KB
data/feed/steady.json   1.182 repo    1,9 MB
data/feed/cooling.json  1.456 repo    2,2 MB
data/feed/dying.json    1.105 repo    1,6 MB
data/feed/dormant.json  7.656 repo    7,9 MB
data/feed/unknown.json    105 repo    169 KB
```

## Satır şeması (33 alan)

**kimlik** `full_name` `owner` `name` `url` `description` `homepage` `language` `license` `archived` `topics[]` `category`

**yaş** `created_at` `age_days` `days_to_1k` `days_to_10k` `days_to_50k`

**büyüklük** `stars`

**trend** `trend` `velocity_7d` `velocity_14d` `velocity_28d` `acceleration` `acceleration_basis` `relative_growth_14d` `fresh_power` `momentum_score` `breakout` `peak_velocity` `days_since_peak` `coverage_days`

**sıralama** `boards` — `{"popular": 12, "momentum": 3}` gibi, sadece o board'da varsa

**Türkçe** `description_tr` `usage_tr` — yazıldıysa

## `trend` ne demek

`acceleration` = son 14 günün hızı ÷ kendi 90 günlük temposu.

| değer | tanım | 1.000+ yıldızlılarda |
|---|---|---|
| `breakout` | ≥3× ve anlamlı hızda | 80 — %0,7 |
| `rising` | 1,5×–3× | 227 — %1,9 |
| `steady` | 0,8×–1,5× | 1.182 — %10,0 |
| `cooling` | 0,5×–0,8× | 1.456 — %12,3 |
| `dying` | <0,5× | 1.105 — %9,4 |
| `dormant` | günde <1 yıldız | 7.656 — **%64,8** |
| `unknown` | ivme ölçülemedi | 105 — %0,9 |

**Sıra önemli:** `dormant` her orandan önce test ediliyor. Haftada 1 yıldızdan haftada 3'e çıkan bir proje "3× hızlandı" görünür ve yine de bitmiştir.

Bu tablodaki asıl bilgi şu: **1.000+ yıldızlı AI projelerinin %2,6'sı hızlanıyor, dörtte üçü bitmiş.** Yıldız sayısı canlılık göstermez.

## Dikkat

- `acceleration_basis` `measured` değilse `acceleration` bir ölçüm değil yer tutucudur (`no_baseline` → 30, `too_young` → 1,0). Filtrele.
- `license` boş olabilir; GitHub'ın tanımadığı lisanslarda `NOASSERTION` döner. Benimseme kararında bunu okunmamış say.
- `description_tr` / `usage_tr` yalnızca 1.491 repoda var; eksikliği kalitesizlik değil, sıranın gelmemesidir.
- Veri her gün yenilenir; `as_of` alanı hangi günün fotoğrafı olduğunu söyler.
