import type { Metadata } from "next";
import Link from "next/link";
import { notFound } from "next/navigation";
import { CATEGORY_LABELS, KNOWLEDGE_ENTRIES, getKnowledgeEntry } from "@/lib/knowledge/entries";
import type { KnowledgeEntry } from "@/lib/knowledge/types";
import { DirectionBadge } from "@/components/KnowledgeDialog";
import { Card } from "@/components/ui";

export function generateStaticParams() {
  return KNOWLEDGE_ENTRIES.map((entry) => ({ slug: entry.slug }));
}

export async function generateMetadata({
  params,
}: {
  params: Promise<{ slug: string }>;
}): Promise<Metadata> {
  const { slug } = await params;
  const entry = getKnowledgeEntry(slug);
  if (!entry) return { title: "词条不存在" };
  return {
    title: `${entry.title} · 指标与方法百科`,
    description: entry.summary,
  };
}

function SectionTitle({ children }: { children: string }) {
  return <h2 className="text-sm font-semibold text-slate-800">{children}</h2>;
}

function EntryBody({ entry }: { entry: KnowledgeEntry }) {
  return (
    <div className="space-y-6">
      <div>
        <div className="flex flex-wrap items-center gap-3">
          <DirectionBadge entry={entry} />
          <span className="text-xs text-slate-400">{CATEGORY_LABELS[entry.category]}</span>
        </div>
        <p className="mt-3 text-base font-medium leading-relaxed text-slate-800">{entry.summary}</p>
      </div>

      {entry.definition && entry.definition.length > 0 && (
        <section>
          <SectionTitle>定义</SectionTitle>
          <div className="mt-2 space-y-2 text-sm leading-relaxed text-slate-600">
            {entry.definition.map((paragraph) => (
              <p key={paragraph}>{paragraph}</p>
            ))}
          </div>
        </section>
      )}

      {entry.formula && (
        <section className="rounded-xl border border-blue-100 bg-blue-50/70 px-4 py-3">
          <p className="text-xs font-semibold text-blue-700">计算公式</p>
          <p className="mt-1 font-mono text-sm leading-relaxed text-slate-800">{entry.formula}</p>
        </section>
      )}

      <section>
        <SectionTitle>本系统口径</SectionTitle>
        <ul className="mt-2 list-inside list-disc space-y-1 text-sm leading-relaxed text-slate-600">
          {entry.systemConvention.map((item) => (
            <li key={item}>{item}</li>
          ))}
        </ul>
      </section>

      {entry.interpretation && entry.interpretation.length > 0 && (
        <section>
          <SectionTitle>如何解读</SectionTitle>
          <div className="mt-2 divide-y divide-slate-100 rounded-xl border border-slate-200">
            {entry.interpretation.map((item) => (
              <div key={item.range} className="grid grid-cols-[100px_1fr] gap-3 px-3.5 py-2.5 text-sm">
                <span className="font-medium tabular-nums text-slate-800">{item.range}</span>
                <span className="text-slate-600">{item.meaning}</span>
              </div>
            ))}
          </div>
        </section>
      )}

      <section className="rounded-xl border border-amber-200 bg-amber-50 px-4 py-3">
        <h2 className="text-sm font-semibold text-amber-800">注意事项</h2>
        <ul className="mt-2 list-inside list-disc space-y-1 text-xs leading-relaxed text-amber-800">
          {entry.cautions.map((item) => (
            <li key={item}>{item}</li>
          ))}
        </ul>
      </section>

      {entry.related.length > 0 && (
        <section>
          <SectionTitle>相关词条</SectionTitle>
          <div className="mt-2 flex flex-wrap gap-2">
            {entry.related.map((related) => {
              const target = getKnowledgeEntry(related);
              return target ? (
                <Link
                  key={related}
                  href={`/knowledge/${related}`}
                  className="rounded-full bg-slate-100 px-3 py-1.5 text-xs text-slate-600 transition-colors hover:bg-slate-200 hover:text-slate-900"
                >
                  {target.title}
                </Link>
              ) : null;
            })}
          </div>
        </section>
      )}

      {entry.codeRefs.length > 0 && (
        <section className="border-t border-slate-100 pt-4">
          <h2 className="text-xs font-semibold text-slate-500">实现参考</h2>
          <div className="mt-2 space-y-1">
            {entry.codeRefs.map((item) => (
              <p key={`${item.file}:${item.line}`} className="font-mono text-[11px] leading-relaxed text-slate-400">
                {item.label} · {item.file}:{item.line}
              </p>
            ))}
          </div>
        </section>
      )}
    </div>
  );
}

export default async function KnowledgeEntryPage({
  params,
}: {
  params: Promise<{ slug: string }>;
}) {
  const { slug } = await params;
  const entry = getKnowledgeEntry(slug);
  if (!entry) notFound();

  return (
    <div>
      <nav className="mb-4 flex items-center gap-1.5 text-xs text-slate-400" aria-label="面包屑">
        <Link href="/knowledge" className="hover:text-blue-700 hover:underline">
          指标与方法百科
        </Link>
        <span aria-hidden="true">/</span>
        <span className="text-slate-500">{CATEGORY_LABELS[entry.category]}</span>
        <span aria-hidden="true">/</span>
        <span className="text-slate-700">{entry.title}</span>
      </nav>

      <div className="mb-6">
        <h1 className="text-xl font-semibold text-slate-900 sm:text-2xl">{entry.title}</h1>
        {entry.aliases.length > 0 && (
          <p className="mt-1 text-sm text-slate-400">{entry.aliases.join(" · ")}</p>
        )}
      </div>

      <Card className="p-5 sm:p-6">
        <EntryBody entry={entry} />
      </Card>

      <p className="mt-6 text-xs leading-relaxed text-slate-400">
        词条内容解释本系统的指标定义与计算口径，仅供研究参考，不构成投资建议。
      </p>
    </div>
  );
}
