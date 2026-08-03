import { CATEGORY_LABELS, KNOWLEDGE_ENTRIES } from "./entries";
import type { KnowledgeCategory, KnowledgeEntry } from "./types";

export function searchKnowledge(query: string, category?: KnowledgeCategory | "all"): KnowledgeEntry[] {
  const needle = query.trim().toLocaleLowerCase("zh-CN");
  return KNOWLEDGE_ENTRIES
    .filter((entry) => !category || category === "all" || entry.category === category)
    .map((entry) => {
      if (!needle) return { entry, score: 0 };
      const title = entry.title.toLocaleLowerCase("zh-CN");
      const aliases = entry.aliases.join(" ").toLocaleLowerCase("zh-CN");
      const body = `${entry.summary} ${(entry.definition ?? []).join(" ")} ${entry.systemConvention.join(" ")}`.toLocaleLowerCase("zh-CN");
      const score = title === needle ? 100 : title.includes(needle) ? 60 : aliases.includes(needle) ? 40 : body.includes(needle) ? 10 : -1;
      return { entry, score };
    })
    .filter((item) => item.score >= 0)
    .sort((a, b) => b.score - a.score || a.entry.title.localeCompare(b.entry.title, "zh-CN"))
    .map((item) => item.entry);
}

export function groupKnowledge(entries: KnowledgeEntry[]): Array<{
  category: KnowledgeCategory;
  label: string;
  entries: KnowledgeEntry[];
}> {
  const groups = new Map<KnowledgeCategory, KnowledgeEntry[]>();
  for (const entry of entries) {
    groups.set(entry.category, [...(groups.get(entry.category) ?? []), entry]);
  }
  return [...groups].map(([category, items]) => ({
    category,
    label: CATEGORY_LABELS[category],
    entries: items,
  }));
}
