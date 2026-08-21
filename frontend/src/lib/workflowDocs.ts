/* GitHub Actions documentation, fetched rather than bundled.
 *
 * `scripts/generate_workflow_docs.py` parses `.github/workflows/*.yml` into
 * `public/workflow-docs.json` (CI's `--check` fails on drift, so the docs can't
 * diverge from the real workflows). The types below are hand-written and are
 * the contract between that generator and the two admin pages that read it.
 *
 * Why fetched and not imported. The generated data is ~180 KB and changes
 * whenever any workflow YAML changes — including pure-backend cron edits with
 * no UI intent. While it lived in `src/lib/` as a `.ts` module, every one of
 * those commits was a frontend deploy, and every frontend deploy rotates EVERY
 * hashed chunk filename (measured: 30 of 30 on a one-character change), which
 * 404s the chunks that tabs opened before the deploy still reference. Measured
 * over an 8-day window, this file alone accounted for most of the SPA deploys
 * that had nothing to do with the SPA. As a static asset in `public/` it
 * changes without renaming anything else.
 *
 * The response guard is not boilerplate: Caddy's SPA fallback serves
 * `index.html` with HTTP 200 for any unmatched path, so a wrong URL or a
 * missing file returns HTML, not a 404 — `res.json()` would then fail with
 * "Unexpected token '<'". Check the content type, not just `res.ok`. */
import { useQuery } from '@tanstack/react-query';

export interface WorkflowInput {
  name: string;
  description: string;
  required: boolean;
  type: string;
  default: string | null;
  options: string[] | null;
}

export interface WorkflowSchedule {
  cron: string;
  human: string;
}

export interface WorkflowDoc {
  filename: string;
  name: string;
  description: string;
  portal: string | null;
  manual: boolean;
  schedules: WorkflowSchedule[];
  onPush: boolean;
  onPullRequest: boolean;
  paths: string[] | null;
  inputs: WorkflowInput[];
  secrets: string[];
  concurrencyGroup: string | null;
  cancelInProgress: boolean | null;
  timeoutMinutes: number | null;
  permissions: string | null;
  runsUrl: string;
  sourceUrl: string;
}

export const WORKFLOW_DOCS_URL = '/workflow-docs.json';

export async function fetchWorkflowDocs(): Promise<WorkflowDoc[]> {
  const res = await fetch(WORKFLOW_DOCS_URL, {
    headers: { Accept: 'application/json' },
  });
  if (!res.ok) {
    throw new Error(`Workflow docs unavailable (HTTP ${res.status})`);
  }
  const contentType = res.headers.get('content-type') ?? '';
  if (!contentType.includes('json')) {
    /* The SPA fallback answered — the file isn't deployed at this path. */
    throw new Error('Workflow docs unavailable (not deployed)');
  }
  const payload: unknown = await res.json();
  const workflows =
    payload != null && typeof payload === 'object'
      ? (payload as { workflows?: unknown }).workflows
      : null;
  if (!Array.isArray(workflows)) {
    throw new Error('Workflow docs malformed');
  }
  return workflows as WorkflowDoc[];
}

/* One query key so Settings and Health share a single fetch + cache. The data
 * only changes on deploy, so it never needs refetching within a session. */
export function useWorkflowDocs() {
  return useQuery<WorkflowDoc[], Error>({
    queryKey: ['workflow-docs'],
    queryFn: fetchWorkflowDocs,
    staleTime: Infinity,
    gcTime: Infinity,
  });
}
