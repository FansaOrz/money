export type KnowledgeCategory =
  | "returns"
  | "risk"
  | "factors"
  | "portfolio"
  | "validation"
  | "fund-basics"
  | "stock-basics"
  | "data";

export type MetricDirection =
  | "higher"
  | "lower"
  | "closer_to_zero"
  | "threshold"
  | "contextual"
  | "neutral";

export interface KnowledgeCodeRef {
  file: string;
  line: number;
  label: string;
}

export interface KnowledgeEntry {
  slug: string;
  title: string;
  aliases: string[];
  category: KnowledgeCategory;
  summary: string;
  definition?: string[];
  formula?: string;
  systemConvention: string[];
  direction: MetricDirection;
  directionLabel: string;
  interpretation?: Array<{ range: string; meaning: string }>;
  cautions: string[];
  related: string[];
  codeRefs: KnowledgeCodeRef[];
}
