/** Shared formatting. Numbers on a dashboard should read at a glance, not be
 *  arithmetic homework. */

const NUMBER = new Intl.NumberFormat("tr-TR");

export function count(value: number | null | undefined): string {
  if (value === null || value === undefined) return "—";
  return NUMBER.format(Math.round(value));
}

export function compact(value: number | null | undefined): string {
  if (value === null || value === undefined) return "—";
  const abs = Math.abs(value);
  if (abs >= 1_000_000) return `${(value / 1_000_000).toFixed(1)}M`;
  if (abs >= 1_000) return `${(value / 1_000).toFixed(abs >= 10_000 ? 0 : 1)}k`;
  return NUMBER.format(Math.round(value));
}

export function rate(value: number | null | undefined): string {
  if (value === null || value === undefined) return "—";
  if (value >= 100) return `${count(value)}/g`;
  if (value >= 10) return `${value.toFixed(0)}/g`;
  return `${value.toFixed(1)}/g`;
}

export function multiple(value: number | null | undefined): string {
  if (value === null || value === undefined) return "—";
  if (value >= 100) return `${Math.round(value)}×`;
  return `${value.toFixed(1)}×`;
}

export function percent(value: number | null | undefined): string {
  if (value === null || value === undefined) return "—";
  return `%${(value * 100).toFixed(value < 0.1 ? 1 : 0)}`;
}

export function days(value: number | null | undefined): string {
  if (value === null || value === undefined) return "—";
  if (value >= 365) {
    const years = value / 365;
    return `${years.toFixed(1)} yıl`;
  }
  return `${count(value)} gün`;
}

export function shortDate(value: string | null | undefined): string {
  if (!value) return "—";
  return new Date(value).toLocaleDateString("tr-TR", {
    day: "numeric",
    month: "short",
    year: "numeric",
  });
}

export const CATEGORY_LABELS: Record<string, string> = {
  "awesome-list": "Kaynak listesi",
  mcp: "MCP",
  "agent-framework": "Agent framework",
  "rag-vectordb": "RAG / vektör DB",
  "inference-serving": "Çıkarım / servis",
  "training-finetuning": "Eğitim / fine-tuning",
  "prompt-eval-observability": "Prompt / değerlendirme",
  "ai-devtools": "AI dev araçları",
  "multimodal-vision": "Görüntü / multimodal",
  "audio-speech": "Ses / konuşma",
  "robotics-embodied": "Robotik",
  "model-weights": "Model ağırlıkları",
  "data-tooling": "Veri araçları",
  "llm-app": "LLM uygulaması",
  "classic-ml": "Klasik ML",
};

export function categoryLabel(value: string | null | undefined): string {
  if (!value) return "—";
  return CATEGORY_LABELS[value] ?? value;
}

/** Money, at the scale this data comes in: rounds run from six figures to
 *  eleven. Written the Turkish way rather than with a bare "B", which reads as
 *  "billion" to an English speaker and as nothing at all to anyone else. */
export function usd(value: number | null | undefined): string {
  if (value === null || value === undefined) return "—";
  const abs = Math.abs(value);
  if (abs >= 1_000_000_000) return `$${(value / 1_000_000_000).toFixed(1)} mlr`;
  if (abs >= 1_000_000) return `$${(value / 1_000_000).toFixed(abs >= 10_000_000 ? 0 : 1)} mn`;
  if (abs >= 1_000) return `$${(value / 1_000).toFixed(0)} bin`;
  return `$${NUMBER.format(Math.round(value))}`;
}

/** Crunchbase round types, in words. An untranslated `series_unknown` on a
 *  board reads as a bug rather than as "they did not say which". */
export const ROUND_LABELS: Record<string, string> = {
  pre_seed: "Pre-seed",
  seed: "Seed",
  angel: "Melek",
  series_a: "Seri A",
  series_b: "Seri B",
  series_c: "Seri C",
  series_d: "Seri D",
  series_e: "Seri E",
  series_f: "Seri F",
  series_g: "Seri G",
  series_unknown: "Tur (açıklanmadı)",
  corporate_round: "Kurumsal",
  private_equity: "Özel sermaye",
  convertible_note: "Dönüştürülebilir",
  debt_financing: "Borç",
  grant: "Hibe",
  post_ipo_equity: "Halka açık sonrası",
};

export function roundLabel(value: string | null | undefined): string {
  if (!value) return "—";
  return ROUND_LABELS[value] ?? value;
}
