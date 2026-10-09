import { categoryLabel } from "@/lib/format";
import type { AiEvidence, AiTag } from "@/lib/types";

/** How a row relates to AI, as small labels — evidence, never a verdict.
 *
 *  The explosion boards have no classifier gate, so this is the only place a
 *  reader learns whether a project is AI, is built with AI, or exposes itself
 *  to agents. The one thing it never says is "not AI": on the 2026-10-09 slice
 *  three of sixteen rows the rules had called non-AI were AI products by their
 *  own README. Absence is printed as "kanıt bulunamadı" (looked, nothing
 *  there) or "henüz bakılmadı" (not looked yet), which are different facts.
 */
const COPY: Record<AiTag, { label: string; strong: boolean }> = {
  ai_project: { label: "AI projesi", strong: true },
  agent_file: { label: "AI ajanlarıyla geliştiriliyor", strong: true },
  built_statement: { label: "AI ile yazıldığını söylüyor", strong: true },
  agent_ready: { label: "ajan-hazır", strong: false },
  uses_ai: { label: "AI kullanıyor", strong: false },
  mentions_ai: { label: "AI'dan söz ediyor", strong: false },
  unsettled: { label: "AI durumu karara bağlanmadı", strong: false },
  none_found: { label: "AI kanıtı bulunamadı", strong: false },
  unchecked: { label: "henüz bakılmadı", strong: false },
};

/** The quote, only when there is one: an empty pair of quotation marks
 *  attributed to a README claims the README said nothing. */
function said(quote: string | null | undefined): string {
  return quote ? ` README'de: “${quote}”` : "";
}

function title(tag: AiTag, evidence: AiEvidence | null, category: string | null): string {
  switch (tag) {
    case "ai_project":
      return `Sınıflandırıcı AI projesi dedi: ${categoryLabel(category)}`;
    case "agent_file":
      return `Kök dizinde ${evidence?.agent_file ?? "ajan talimat dosyası"} var — kodlama ajanlarına yazılmış talimatlar. Okunan 89 dosyanın 81'i tam olarak buydu.`;
    case "built_statement":
      return `README, projenin AI ile yazıldığını söylüyor.${said(evidence?.built)}`;
    case "agent_ready":
      return `Kendini ajanlara açıyor (MCP sunucusu, skill ya da eklenti).${said(evidence?.agent_ready)}`;
    case "uses_ai":
      return `Çalışırken bir model kullanıyor görünüyor.${said(evidence?.uses_ai)}`;
    case "mentions_ai":
      return `README bir model ya da sağlayıcı adı geçiriyor${
        evidence?.mentions_ai ? ` (“${evidence.mentions_ai}”)` : ""
      }, başka bir şey söylemiyor. Bu çoğu zaman "Claude ile yazdım" demek; uygulamanın AI kullandığı anlamına gelmeyebilir.`;
    case "unsettled":
      return "Kural motoru karar veremedi; inceleme kuyruğunda. Bu bir 'hayır' değil.";
    case "none_found":
      return `AGENTS.md, CLAUDE.md ve README okundu${
        evidence?.checked_on ? ` (${evidence.checked_on})` : ""
      }; AI ile ilgili bir iz bulunamadı. Bu, AI olmadığı anlamına gelmez.`;
    case "unchecked":
      return "Dosyaları henüz okunmadı; bir sonraki tur bakacak.";
  }
}

export function AiTags({
  tags,
  evidence,
  category,
}: {
  tags: AiTag[];
  evidence: AiEvidence | null;
  category: string | null;
}) {
  return (
    <span className="mt-1 flex flex-wrap gap-1">
      {tags.map((tag) => {
        const copy = COPY[tag];
        const label =
          tag === "ai_project" && category ? `${copy.label} · ${categoryLabel(category)}` : copy.label;
        return (
          <span
            key={tag}
            title={title(tag, evidence, category)}
            className={
              copy.strong
                ? "border-accent/50 text-ink rounded border px-1.5 py-0.5 text-[11px] leading-none"
                : "border-border text-ink-muted rounded border px-1.5 py-0.5 text-[11px] leading-none"
            }
          >
            {label}
          </span>
        );
      })}
    </span>
  );
}
