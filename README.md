# AI Radar

GitHub'daki yapay zeka ekosistemini keşfeden, günlük metrik toplayan ve
**mutlak popülerlik ile momentum'u ayrı ayrı** sıralayan bir sistem.

Site: GitHub Pages · Veri: günde bir, otomatik · Aylık maliyet: **$0**

## Neden

`sort:stars` yanıltıcıdır. 5 yılda 50.000 yıldız toplamış bir proje ile son iki
haftada 50.000 yıldız almış bir proje aynı listede yan yana durduğunda,
ikincisinin piyasayı ezdiği bilgisi tamamen kaybolur. AI Radar aynı repoları
dört ayrı soruya göre sıralar:

| Board | Soru | Sıralama |
|---|---|---|
| **Fresh Power** ⭐ | Yıldızları yaşına göre değersizleştirirsek kim büyük? | `fresh_power` |
| **Momentum** | Şu anda en hızlı kim büyüyor? | 14 günlük günlük hız |
| **Breakout** | Kim birden hızlandı? | ivme × göreli büyüme |
| **Popüler** | Ham toplam kim büyük? | toplam yıldız |

`fresh_power` bayrak metriktir: her yıldızın ağırlığı 180 günde yarılanır, yani
beş yılda birikmiş yıldızlar sıralamayı domine edemez. Listelerin birbirinden
farklı çıkması bir tutarsızlık değil — bütün mesele o.

## Veri kaynağı

Bu alandaki alışıldık yöntemlerin çoğu 2025-2026'da bozuldu:

| Kaynak | Durum |
|---|---|
| GH Archive / BigQuery `WatchEvent` | 2025 ortasından beri feed neredeyse sadece `PushEvent` döndürüyor; yıldız olayları ağır eksik |
| OSS Insight | Aynı sebeple yıldız bazlı sıralamalarını askıya aldı |
| `GET /repos/.../stargazers` | Temmuz 2026'da admin/collaborator'a kısıtlandı |

Kullanılan kaynak, GitHub'ın 4 Eylül 2026'da yayınladığı gizlilik-güvenli resmî
endpoint'i: **`GET /repos/{owner}/{repo}/stargazers/history`** (REST API sürümü
`2026-03-10`). Haftalık gruplanmış, gün gün yıldız kazanımı döndürür.

### Kapsama kuralı

Tek bir API sayfası ~210 gün veri döndürür:

- **Sınırlı pencereli metrikler** (7/14/28/90 günlük hız, ivme, göreli büyüme)
  hiçbir backfill olmadan **tam doğru** hesaplanır. Günlük tazeleme repo başına
  iki istek.
- **`fresh_power`** reponun tüm ömrünü integre eder. 180 günlük yarılanmada 210
  günlük geçmiş gerçek değerin ancak **%55**'ini yakalar. Bu yüzden Fresh Power
  board'u yalnızca backfill'i tamamlanmış repoları sıralar.

## Mimari

```
pipeline/   Python — keşif, toplama, sınıflandırma, skorlama (CLI: airadar)
web/        Next.js 16 + Recharts, statik export → GitHub Pages
.github/    Actions — günlük toplama, haftalık keşif, manuel backfill, CI
```

Sunucu yok, veritabanı hesabı yok. Veri tek bir SQLite dosyası; **GitHub release
dosyası** olarak saklanıyor (git geçmişinde değil — her gün değişen çok megabaytlık
bir ikili dosya depoyu her çalıştırmada kendi boyutu kadar şişirirdi). Site
statik: günde bir üretilen JSON'dan besleniyor.

### Keşif

GitHub search her sorguyu **1.000 sonuçta** keser ve bunu sessizce yapar. Çözüm
bölümleme: her sorgu yıldız aralığına göre dilimlenir, tavana çarpan her dilim
**geometrik** ortadan ikiye bölünür (yıldız dağılımı üstel; aritmetik orta
neredeyse her şeyi alt yarıda bırakırdı). Yıldızın ayıramadığı dilimler
oluşturma tarihine göre bölünür.

Dört kanal: topic × yıldız kovası, serbest metin anahtar kelimeleri, topic
kartopu (doğrulanmış AI repolarında görülen yeni topic'ler bir sonraki turda
sorgulanır), küratörlü `awesome-*` listeleri. Ek olarak
[ecosyste.ms](https://ecosyste.ms) bağımlılık grafiği ve Hugging Face Hub
model/space kartlarındaki GitHub linkleri.

Arama 30 istek/dakika ile sınırlı olduğu için tam bir tarama tek bir CI işinden
uzun sürer. `--max-queries` turu temiz biçimde durdurur; taranan topic'ler
kaydedildiği için bir sonraki tur kaldığı yerden devam eder.

### Sınıflandırma

Kural motoru (ücretsiz) repoların çoğunu karara bağlar. Sınırdaki repolar için
LLM yolu (`classify/llm.py`, Claude Haiku 4.5 + Batch API) hazır ama varsayılan
olarak kapalı: otomasyon `--no-llm` ile çalışır ve hiç para harcamaz.

### Budama

Veri depoda durduğu için sınırsız büyüyemez. Günlük satırların son **120 günü**
tam çözünürlükte tutulur; daha eskisi haftalık kovalara katlanır (detay
grafiğinin tüm ömrü göstermeye devam etmesi için) ve `fresh_power` katkısı tek
bir taşınan sayıya çöker.

Bu çöküş **kayıpsız**: üstel sönüm çarpımsal olduğu için budanmış geçmiş +
taşınan kuyruk, tam geçmişle aynı sonucu verir. Test bunu 1e-9 hassasiyetinde
doğruluyor, ve ayrı bir test yedi günlük çalıştırma zincirinde skorun
aşınmadığını kontrol ediyor.

## Kurulum

Tek bir kimlik bilgisi gerekiyor.

1. **Depo herkese açık olmalı** — Actions dakikaları ve Pages bu sayede ücretsiz.
2. **`GH_PAT` secret'ı**: bir personal access token
   ([üret](https://github.com/settings/personal-access-tokens/new), "Public
   Repositories (read-only)" yeter) → Settings → Secrets and variables →
   Actions → New repository secret.
   Actions'ın kendi `GITHUB_TOKEN`'ı repo başına saatte 1.000 istekle sınırlı;
   bu iş yükü için yetersiz.
3. **Settings → Pages → Source: GitHub Actions**

## Otomasyon

| Workflow | Ne zaman | Ne yapar |
|---|---|---|
| `daily` | Her gün 06:10 UTC | `doctor` → `collect` → `score` → siteyi yayınla |
| `discover` | Pazar 03:20 UTC | `discover` (bütçeli) → `classify --no-llm` → `score` → yayınla |
| `backfill` | Manuel | Tam geçmişi geri yürür; repo başına bir kez gerekir |
| `publish` | Manuel / çağrılır | Veriyi değiştirmeden siteyi yeniden kurar |
| `ci` | Her push | pytest + ruff + web typecheck/build |

## Yerel geliştirme

```bash
cd pipeline
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
cp ../.env.example ../.env     # GH_PAT

.venv/bin/airadar doctor              # kimlik + endpoint kontrolü
.venv/bin/airadar init-db
.venv/bin/airadar discover --channels topics --max-queries 500
.venv/bin/airadar classify --no-llm
.venv/bin/airadar collect
.venv/bin/airadar backfill --limit 50
.venv/bin/airadar score
.venv/bin/airadar board fresh --limit 20
.venv/bin/airadar export-site --out ../web/public/data

cd ../web && npm install && npm run dev
```

## Test

```bash
cd pipeline && .venv/bin/python -m pytest tests -q && .venv/bin/ruff check airadar tests
cd ../web && npx tsc --noEmit && npx next build
```

Veritabanı SQLite olduğu için bütün testler her yerde koşar — "erişilebilir bir
veritabanı yoktu, atlandı" diye bir delik yok.

## Tasarım notları

- **Toplama yeniden başlatılabilir.** Her yazma idempotent, kuyruk
  `last_checked_at` sırasına göre; yarıda kesilen tur kaldığı yerden devam eder.
- **ETag'ler saklanıyor.** `304 Not Modified` GitHub'ın kotasından düşmüyor.
- **Repo kimliği sayısal `id`.** Yeniden adlandırılan repolar ikizlenmiyor.
- **Hiçbir şey sessizce kaybolmuyor.** Keşifte tavana çarpan dilim ikinci bir
  boyuta göre bölünür; LLM yanıtında eşleşmeyen repo önbelleğe yazılmaz ve
  yeniden denenir; göremediğimiz bir kilometre taşı `None` döner, uydurulmaz.
- **Her çalıştırma maliyetini raporluyor** (`run_log`): API isteği, 304 sayısı,
  LLM token'ı ve dolar.
