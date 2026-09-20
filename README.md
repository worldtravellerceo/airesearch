# AI Radar

GitHub'daki yapay zeka ekosistemini keşfeden, günlük metrik toplayan ve
**mutlak popülerlik ile momentum'u ayrı ayrı** sıralayan bir sistem.

## Neden

`sort:stars` yanıltıcıdır. 5 yılda 50.000 star toplamış bir proje ile son iki
haftada 50.000 star almış bir proje aynı listede yan yana durduğunda, ikincisinin
piyasayı ezdiği bilgisi tamamen kaybolur. AI Radar aynı repoları dört ayrı soruya
göre sıralar:

| Board | Soru | Sıralama |
|---|---|---|
| **Fresh Power** ⭐ | Yıldızları yaşına göre değersizleştirirsek kim büyük? | `fresh_power` |
| **Momentum** | Şu anda en hızlı kim büyüyor? | 14 günlük günlük hız |
| **Breakout** | Kim birden hızlandı? | ivme × göreli büyüme |
| **Popüler** | Ham toplam kim büyük? | toplam yıldız |

`fresh_power` bayrak metriktir: her yıldızın ağırlığı 180 günde yarılanır, yani
beş yılda birikmiş yıldızlar sıralamayı domine edemez.

Demo veriyle doğrulanmış davranış: 2 haftada 50.000 yıldız almış bir proje,
Fresh Power'da 128.000 ve 118.000 yıldızlı projeleri geçer; Popüler board'unda
ise onların arkasındadır. İki liste de doğrudur — farklı soruları cevaplıyorlar.

## Veri kaynağı — ve neden bu

Bu alandaki alışıldık yöntemlerin çoğu 2025-2026'da bozuldu:

| Kaynak | Durum |
|---|---|
| GH Archive / BigQuery `WatchEvent` | 2025 ortasından beri feed neredeyse sadece `PushEvent` döndürüyor; star olayları ağır eksik |
| OSS Insight | Aynı sebeple star bazlı sıralamalarını askıya aldı |
| `GET /repos/.../stargazers` | Temmuz 2026'da admin/collaborator'a kısıtlandı (ayrıca 40k tavanı vardı) |

Kullanılan kaynak, GitHub'ın 4 Eylül 2026'da yayınladığı gizlilik-güvenli resmî
endpoint'i: **`GET /repos/{owner}/{repo}/stargazers/history`** (REST API sürümü
`2026-03-10`). Haftalık gruplanmış, gün gün yıldız kazanımı döndürür.

### Kapsama kuralı

Tek bir API sayfası ~210 gün veri döndürür:

- **Sınırlı pencereli metrikler** (7/14/28/90 günlük hız, ivme, göreli büyüme)
  hiçbir backfill olmadan **tam doğru** hesaplanır. Günlük tazeleme repo başına
  iki istek: repo nesnesi + geçmişin ilk sayfası.
- **`fresh_power`** reponun tüm ömrünü integre eder. 180 günlük yarılanmada 210
  günlük geçmiş gerçek değerin ancak **%55**'ini yakalar. Bu yüzden Fresh Power
  board'u yalnızca backfill'i tamamlanmış repoları sıralar (`history_complete`);
  aksi halde hakkında daha az şey bildiğimiz repo haksız yere öne çıkardı.

## Mimari

```
pipeline/   Python — keşif, toplama, sınıflandırma, skorlama (CLI: airadar)
api/        FastAPI — salt-okunur JSON API (Vercel Python Function)
web/        Next.js 16 + Recharts dashboard (Vercel)
.github/    Actions — günlük toplama, haftalık keşif, manuel backfill, CI
```

Veri PostgreSQL'de (Neon). Boru hattı yazar, API okur.

### Keşif

GitHub search her sorguyu **1.000 sonuçta** keser ve bunu sessizce yapar, yani
tek sorguyla bu büyüklükte bir popülasyon sayılamaz. Çözüm bölümleme: her sorgu
yıldız aralığına göre dilimlenir, tavana çarpan her dilim **geometrik** ortadan
ikiye bölünür (yıldız dağılımı üstel; aritmetik orta neredeyse her şeyi alt
yarıda bırakırdı). Yıldızın ayıramadığı dilimler oluşturma tarihine göre bölünür.

Dört kanal besliyor: topic × yıldız kovası, serbest metin anahtar kelimeleri,
topic kartopu (doğrulanmış AI repolarında görülen yeni topic'ler bir sonraki
turda sorgulanır), ve küratörlü `awesome-*` listeleri. Buna ek olarak
[ecosyste.ms](https://ecosyste.ms) bağımlılık grafiği ("torch'u import eden
repolar" — GitHub search'ün cevaplayamadığı soru) ve Hugging Face Hub model/space
kartlarındaki GitHub linkleri.

### Sınıflandırma

Kural motoru (ücretsiz) repoların çoğunu karara bağlar; sadece belirsiz banttaki
repolar README'si okunarak Claude Haiku 4.5'e gider — **Batch API** ile (%50
indirim) ve istek başına 15 repo gruplanarak (Haiku 4.5'in minimum cache prefix'i
4096 token; sistem prompt'u oraya ulaşmadığı için prompt caching sessizce
çalışmaz, gruplama aynı tasarrufu sağlar).

Sonuçlar girdilerinin hash'ine karşı önbelleklenir: haftalık tur yalnızca yeni
veya açıklaması/topic'i/dili gerçekten değişmiş repolar için para harcar.

5.000 belirsiz repo için tahmini maliyet **≈ $1.63**. `airadar classify --dry-run`
bunu harcamadan raporlar, çalıştırma sonrası tahmin ile gerçek yan yana gösterilir.

## Maliyet

| Kalem | Maliyet |
|---|---|
| GitHub API (PAT, 5.000 istek/saat) | $0 |
| GitHub Actions (public repo) | $0 |
| Neon Postgres (free tier) | $0 |
| Vercel (web + api, hobby) | $0 |
| LLM sınıflandırma — ilk tam tarama | ≈ $1.6 |
| LLM sınıflandırma — haftalık | birkaç sent |

## Kurulum

### 1. Boru hattı

```bash
cd pipeline
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
cp ../.env.example ../.env     # GH_PAT, DATABASE_URL, ANTHROPIC_API_KEY
```

`GH_PAT` bir **personal access token** olmalı. Actions'ın `GITHUB_TOKEN`'ı repo
başına saatte 1.000 istekle sınırlı; bu iş yükü için yetersiz.

### 2. İlk kontrol

```bash
airadar doctor
```

Kimlik doğrulama, kotalar ve star-history endpoint'ini doğrular. **Geri kalan her
şey bu kontrolün geçmesine bağlı** — endpoint iki haftalık olduğu için sahada
teyit edilmesi şart. Beklenmedik bir davranışta `doctor` yedek planı da söyler.

### 3. Çalıştırma

```bash
airadar init-db                          # şema (tekrar çalıştırılabilir)
airadar discover --channels topics       # evreni kur (uzun sürer, bölünebilir)
airadar classify --dry-run               # ne kadara mal olacak?
airadar classify                         # kural + LLM
airadar collect                          # metrikleri tazele
airadar backfill --limit 500             # Fresh Power için tam geçmiş
airadar score                            # metrikler + board'lar
airadar board fresh --limit 20           # sonucu terminalde gör
```

### 4. API ve dashboard

```bash
# API (Vercel Python runtime; app.py içindeki `app` entrypoint)
cd api && DATABASE_URL=... uvicorn app:app --port 8000

# Dashboard
cd web && npm install && API_BASE=http://127.0.0.1:8000 npm run dev
```

Vercel'de: `api/` ve `web/` ayrı proje olarak deploy edilir. `DATABASE_URL`
Neon'un **pooled** endpoint'ini (`-pooler` host) göstermeli — serverless her
çağrıda yeni bağlantı açar.

### 5. Otomasyon

Actions secret'ları: `GH_PAT`, `DATABASE_URL`, `ANTHROPIC_API_KEY`.

| Workflow | Ne zaman | Ne yapar |
|---|---|---|
| `daily-collect` | Her gün 06:10 UTC | `doctor` → `collect` → `score`, özet olarak Fresh Power ilk 20 |
| `weekly-discover` | Pazar 03:20 UTC | `discover` → `classify` (önce dry-run maliyeti) → `score` |
| `backfill` | Manuel | Tam geçmişi geri yürür; repo başına bir kez gerekir |
| `ci` | Her push | pytest + ruff + web typecheck/build |

## Test

```bash
cd pipeline
.venv/bin/python -m pytest tests -q
.venv/bin/ruff check airadar tests

cd ../web
npx tsc --noEmit && npx next build
```

Veritabanı testleri gerçek bir PostgreSQL'e karşı çalışır; `AIRADAR_TEST_DSN` ile
adres verilir. Erişilebilir sunucu yoksa o testler atlanır, süitin kalanı çalışır.
CI'da bir servis container'ı bağlandığı için orada her zaman koşarlar.

## Tasarım notları

- **Toplama yeniden başlatılabilir.** Rate limit, Actions timeout'u veya ağ
  hatası turu yarıda kesebilir; her yazma idempotent ve kuyruk `last_checked_at`
  sırasına göre, yani tekrar çalıştırmak kaldığı yerden devam eder.
- **ETag'ler saklanıyor.** `304 Not Modified` GitHub'ın kotasından düşmüyor;
  değişmeyen repo bedava.
- **Repo kimliği sayısal `id`.** Yeniden adlandırılan repolar ikizlenmiyor.
- **Hiçbir şey sessizce kaybolmuyor.** LLM yanıtında eşleşmeyen bir repo
  önbelleğe yazılmaz ve bir sonraki tur yeniden denenir; keşifte tavana çarpan
  dilim ikinci bir boyuta göre bölünür; göremediğimiz bir kilometre taşı `None`
  döner, uydurulmaz.
- **Her çalıştırma maliyetini raporluyor** (`run_log`): API isteği, 304 sayısı,
  LLM token'ı ve dolar.
