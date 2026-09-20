# AI Radar

GitHub'daki yapay zeka ekosistemini keşfeden, günlük metrik toplayan ve
**mutlak popülerlik ile momentum'u ayrı ayrı** sıralayan bir veri boru hattı.

## Neden

`sort:stars` yanıltıcıdır. 5 yılda 50.000 star toplamış bir proje ile son 2
haftada 50.000 star almış bir proje aynı listede yan yana durduğunda, ikincisinin
piyasayı ezdiği bilgisi tamamen kaybolur. AI Radar bu iki şeyi ayırır:

| Board | Ne ölçer |
|---|---|
| **Popular** | Toplam star — klasik görünüm |
| **Momentum** | Son 14 günün günlük star hızı |
| **Breakout** | İvme × göreli büyüme — patlamak üzere olanlar |
| **Fresh Power** ⭐ | Zamanla sönümlenmiş popülerlik (star'ın ağırlığı 180 günde yarılanır) |

`fresh_power` bayrak metriktir: eski birikmiş star'lar sıralamayı domine edemez.

### Kapsama (coverage) kuralı

Tek bir API sayfası ~210 gün veri döndürür. Bu, **sınırlı pencereli** metriklerin
(7/14/28/90 günlük hız, ivme, göreli büyüme) hiçbir backfill olmadan **tam doğru**
hesaplanması demektir — günlük tazeleme repo başına tek istek.

`fresh_power` ise reponun tüm ömrünü integre eder. 180 günlük yarılanmada 210
günlük geçmiş gerçek değerin ancak **%55**'ini yakalar. Bu yüzden Fresh Power
board'u yalnızca **backfill'i tamamlanmış** repoları sıralar (`history_complete`);
aksi halde hakkında daha az şey bildiğimiz repo haksız yere öne çıkardı.

## Veri kaynağı

Bu alandaki alışıldık yöntemlerin çoğu 2025-2026'da bozuldu:

- **GH Archive / BigQuery `WatchEvent`** — 2025 ortasından beri feed neredeyse
  sadece `PushEvent` döndürüyor, star olayları ağır eksik. Kullanılmıyor.
- **OSS Insight** — aynı sebeple star bazlı sıralamalarını askıya aldı.
- **`GET /repos/.../stargazers`** — Temmuz 2026'da admin/collaborator'a kısıtlandı.

Kullanılan kaynak, GitHub'ın 4 Eylül 2026'da yayınladığı gizlilik-güvenli resmî
endpoint'i: **`GET /repos/{owner}/{repo}/stargazers/history`** (REST API version
`2026-03-10`). Haftalık gruplanmış, gün gün star kazanımı döndürür; tek sayfa
(30 hafta) ≈ 7 ay, yani günlük tazelemede repo başına tek istek yeter.

## Kurulum

```bash
cd pipeline
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
cp ../.env.example ../.env    # GH_PAT, DATABASE_URL, ANTHROPIC_API_KEY doldurun
```

`GH_PAT` bir **personal access token** olmalı. Actions'ın `GITHUB_TOKEN`'ı repo
başına saatte 1.000 istekle sınırlı; bu iş yükü için yetersiz.

## İlk kontrol

```bash
.venv/bin/airadar doctor --repo huggingface/transformers
```

Kimlik doğrulama, rate limit kotaları ve star-history endpoint'ini doğrular.
Projenin geri kalanı bu kontrolün geçmesine bağlı.

## Test

```bash
.venv/bin/python -m pytest tests/ -q
.venv/bin/ruff check airadar tests
```

## Yapı

```
pipeline/airadar/
├── config.py          # ayarlar (pydantic-settings)
├── gh/client.py       # ETag cache, rate-limit governor, backoff
├── gh/metrics.py      # star-history çözümleme
├── db/schema.sql      # PostgreSQL şeması
└── cli.py             # airadar <komut>
```
