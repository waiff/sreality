/* What users put on a property, as marks to read, never to change (MS16): merge
 * mode shows them on every card and beside every ticked property in the merge
 * bar. Each reads the query its Browse control already shares, so they cost no
 * new request; a failed read is a retry, never an absent mark. */

import { useQuery } from '@tanstack/react-query';
import type { CSSProperties, ReactNode } from 'react';

import CollectionMark from '@/components/CollectionMark';
import { EyeOffIcon } from '@/components/icons';
import NoteMark from '@/components/NoteMark';
import PipelineMark from '@/components/PipelineMark';
import ReadFailedMark, { readFailed } from '@/components/ReadFailedMark';
import { stageAccent, stageBadge } from '@/lib/pipelineStage';
import {
  curationKeys,
  dismissalKeys,
  fetchIsDismissed,
  fetchPipelineMembers,
  fetchPipelineStages,
  fetchPropertyCollectionMemberSet,
  pipelineKeys,
} from '@/lib/queries';

const MARK =
  'flex h-6 items-center rounded-[var(--radius-xs)] border bg-[var(--color-paper-3)]/90 px-1 backdrop-blur';

function Mark({ label, tone = '', style, children }: {
  label: string;
  tone?: string;
  style?: CSSProperties;
  children: ReactNode;
}) {
  return (
    <span title={label} style={style} className={`${MARK} ${tone}`}>
      {children}
      <span className="sr-only">{label}</span>
    </span>
  );
}

export default function CurationMarks({ property_id }: { property_id: number }) {
  const membersQ = useQuery({
    queryKey: pipelineKeys.members,
    queryFn: fetchPipelineMembers,
    staleTime: 30_000,
  });
  const stagesQ = useQuery({
    queryKey: pipelineKeys.stages,
    queryFn: fetchPipelineStages,
    staleTime: 60_000,
  });
  const collectionsQ = useQuery({
    queryKey: curationKeys.propertyCollectionMembers,
    queryFn: fetchPropertyCollectionMemberSet,
    staleTime: 30_000,
  });
  const dismissedQ = useQuery({
    queryKey: dismissalKeys.state(property_id),
    queryFn: () => fetchIsDismissed(property_id),
    staleTime: 60_000,
  });
  const card = membersQ.data?.get(property_id);
  const look = card && { id: card.stage_id, color: card.stage_color, code: card.stage_code };
  const collections = collectionsQ.data?.get(property_id)?.length ?? 0;

  return (
    <span className="flex items-center gap-1">
      {readFailed(membersQ) ? (
        <ReadFailedMark what="Pipeline" onRetry={() => void membersQ.refetch()} />
      ) : card && look ? (
        <Mark
          label={`V pipeline: ${card.stage_label}`}
          style={{ color: stageAccent(look).fg, borderColor: stageAccent(look).fg }}
        >
          <PipelineMark filled badge={stageBadge(look, stagesQ.data ?? [])} />
        </Mark>
      ) : null}
      {readFailed(collectionsQ) ? (
        <ReadFailedMark what="Kolekce" onRetry={() => void collectionsQ.refetch()} />
      ) : collections > 0 ? (
        <Mark
          label={collections === 1 ? 'V kolekci' : `V kolekcích: ${collections}`}
          tone="border-[var(--color-copper)] text-[var(--color-copper)]"
        >
          <CollectionMark filled />
        </Mark>
      ) : null}
      {dismissedQ.data && (
        <Mark label="Skryto" tone="border-[var(--color-ink-3)] text-[var(--color-ink-2)]">
          <EyeOffIcon filled className="h-3.5 w-3.5" />
        </Mark>
      )}
      <NoteMark property_id={property_id} />
    </span>
  );
}

/* The merge bar's list of what is ticked: each property by number, with its marks. */
export function MergeSelection({ ids }: { ids: ReadonlySet<number> }) {
  return (
    <ul aria-label="Vybráno ke sloučení" className="flex flex-wrap items-center gap-x-3 gap-y-1">
      {[...ids].map((id) => (
        <li key={id} className="flex items-center gap-1.5">
          <span className="font-mono text-[0.75rem] tabular-nums text-[var(--color-ink-2)]">#{id}</span>
          <CurationMarks property_id={id} />
        </li>
      ))}
    </ul>
  );
}
