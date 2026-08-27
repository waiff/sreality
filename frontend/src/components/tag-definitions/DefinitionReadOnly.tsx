import { useMemo } from 'react';
import type { TagDefinition } from '@/lib/api';

/* One historical version, rendered read-only. Tag references arrive as ids and
 * are resolved through the version's own `referenced_tags` — an id whose tag was
 * deleted since simply has no label, so it renders as the bare id rather than
 * disappearing. */
export default function DefinitionReadOnly({ definition }: { definition: TagDefinition }) {
  const labelOf = useMemo(() => {
    const byId = new Map(definition.referenced_tags.map((r) => [r.tag_id, r.label]));
    return (id: number): string => byId.get(id) ?? `tag ${id}`;
  }, [definition.referenced_tags]);

  return (
    <div className="space-y-3 text-[0.82rem]">
      <Block label="means">
        <p className="text-[var(--color-ink)]">{definition.means}</p>
      </Block>

      <Block label="counts">
        {definition.counts.length === 0 ? (
          <Empty />
        ) : (
          <ul className="space-y-0.5">
            {definition.counts.map((c, i) => (
              <li key={i} className="text-[var(--color-ink-2)]">
                {c}
              </li>
            ))}
          </ul>
        )}
      </Block>

      <Block label="does not count">
        {definition.does_not_count.length === 0 ? (
          <Empty />
        ) : (
          <ul className="space-y-0.5">
            {definition.does_not_count.map((r, i) => (
              <li key={i} className="text-[var(--color-ink-2)]">
                {r.case}
                {r.goes_to_tag_id != null && (
                  <span className="text-[var(--color-ink-4)]">
                    {' → '}
                    <span className="font-mono">{labelOf(r.goes_to_tag_id)}</span>
                  </span>
                )}
              </li>
            ))}
          </ul>
        )}
      </Block>

      <Block label="confusable with">
        {definition.confusable_with.length === 0 ? (
          <Empty />
        ) : (
          <ul className="space-y-0.5">
            {definition.confusable_with.map((r, i) => (
              <li key={i} className="text-[var(--color-ink-2)]">
                <span className="font-mono text-[0.78rem]">{labelOf(r.tag_id)}</span>
                <span className="text-[var(--color-ink-4)]">{' — '}</span>
                {r.tell}
              </li>
            ))}
          </ul>
        )}
      </Block>

      <Block label="leave out when">
        {definition.leave_out_when ? (
          <p className="text-[var(--color-ink-2)]">{definition.leave_out_when}</p>
        ) : (
          <Empty />
        )}
      </Block>

      <Block label="example images">
        {definition.example_image_ids.length === 0 ? (
          <Empty />
        ) : (
          <p className="font-mono text-[0.76rem] tabular-nums text-[var(--color-ink-2)]">
            {definition.example_image_ids.join(' · ')}
          </p>
        )}
      </Block>
    </div>
  );
}

function Block({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="border-t border-[var(--color-rule-soft)] pt-3 first:border-t-0 first:pt-0">
      <p className="mb-1 text-[0.65rem] tracking-[0.14em] uppercase text-[var(--color-ink-3)]">
        {label}
      </p>
      {children}
    </div>
  );
}

const Empty = () => <p className="text-[var(--color-ink-4)]">—</p>;
