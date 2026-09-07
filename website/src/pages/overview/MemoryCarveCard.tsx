import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Layers, X } from 'lucide-react'
import { api } from '../../api/client'
import { Card, CardTitle, Btn, Badge, EmptyState } from '../../components/ui'
import Clickable from '../../components/Clickable'
import SimpleSelect from '../../components/SimpleSelect'
import { fmtNumber } from '../../i18n/format'
import { i18nT } from '../../i18n/t'
import { MemoryScopeNotice, memoryErrorCode, memoryQueryRetry } from './MemoryStoreCard'

/**
 * How a store's memory divides up: which crews, surfaces, scopes and sessions
 * filled it, and how much each contributed.
 *
 * A count first, rows second. "Which surfaces wrote this crew's memory" is the
 * question an operator actually opens this card with, and a bare page of rows
 * answers it only by being read end to end.
 */

/** The row TYPE column. Not a facet — nothing stamps it, it is what a row IS —
 *  but it is a legal count axis and belongs on the same control, because "how
 *  much of this store is directives" is asked in the same breath. Mirrors
 *  `memory_schema._KIND_COLUMN`. */
const KIND_AXIS = 'kind'

/** The five carve facets, mirroring `memory_schema.FACET_NAMES`, in that tuple's
 *  order. These are the backend's own axis identifiers and are shown verbatim:
 *  they are the same names the CLI's `kirocrew memory carve` prints and the same
 *  ones the wire carries, so renaming them here would only hide the mapping. */
const FACET_AXES = ['scope', 'surface', 'crew', 'session_key', 'derived_from'] as const

/** Every axis a count may be grouped by. Mirrors `memory_schema.GROUPABLE_COLUMNS`. */
const COUNT_AXES: readonly string[] = [...FACET_AXES, KIND_AXIS]

/** Rows one carve page asks for. Matches the route's own default page size. */
const CARVE_PAGE = 50

/** Longest remembered text rendered inline, so one long episode cannot push the
 *  facet columns off the card. */
const TEXT_PREVIEW_CHARS = 160

/** The narrowing a clicked count applies: one axis pinned to one value.
 *  `axis` is `kind` or one of {@link FACET_AXES}; the two travel to the gateway on
 *  different parameters, which is why the axis is carried rather than inferred. */
interface CarveFilter {
  axis: string
  value: string
}

/** One count row's label. An empty stored value is a real answer — the rows no
 *  writer attributed on that axis — and has to read as that rather than as a
 *  blank cell. */
function AxisValue({ value }: { value: string }) {
  if (value === '') return <span className="text-muted">{i18nT('pages.overview.memoryCarveCard.not_recorded')}</span>
  return <>{value}</>
}

export default function MemoryCarveCard({ store }: { store: string }) {
  const [countBy, setCountBy] = useState<string>(FACET_AXES[1])
  const [filter, setFilter] = useState<CarveFilter | null>(null)

  const counts = useQuery({
    queryKey: ['memory-carve', store, 'counts', countBy],
    queryFn: () => api.memoryCarve({ store: store || undefined, countBy }),
    retry: memoryQueryRetry,
  })

  const entries = useQuery({
    queryKey: ['memory-carve', store, 'entries', filter?.axis ?? '', filter?.value ?? ''],
    queryFn: () => api.memoryCarve({
      store: store || undefined,
      ...(filter && filter.axis === KIND_AXIS ? { kind: filter.value } : {}),
      ...(filter && filter.axis !== KIND_AXIS ? { facets: { [filter.axis]: filter.value } } : {}),
      limit: CARVE_PAGE,
    }),
    retry: memoryQueryRetry,
    enabled: filter !== null,
  })

  // 409 on the v1 lineage, whose rows carry no facet columns at all. Rendered as
  // the explanation it is: an empty list here would read as "this crew remembers
  // nothing", which is the opposite of what the refusal says.
  const unsupported = memoryErrorCode(counts.error) === 'facets_unsupported'
  const countRows = Object.entries(counts.data?.counts ?? {})
  const entryRows = entries.data?.entries ?? []

  return (
    <Card>
      <CardTitle>{i18nT('pages.overview.memoryCarveCard.carve')}</CardTitle>
      {unsupported ? (
        <p className="text-[13px] leading-relaxed text-muted">
          {i18nT('pages.overview.memoryCarveCard.this_store_uses_the_shared_schema_so_carve_facet')}
        </p>
      ) : (
        <>
          <p className="text-[12px] leading-relaxed text-muted mb-2">
            {i18nT('pages.overview.memoryCarveCard.how_this_store_s_memory_divides_up_across_the_ax')}
          </p>
          <div className="flex gap-2 items-center flex-wrap mb-3">
            {/* A caption, not a `<label htmlFor>`: the select's trigger is a
                button, which `<label>` does not name in every screen reader, so
                the accessible name comes from `aria-label` instead. */}
            <span className="text-[13px] text-muted">{i18nT('pages.overview.memoryCarveCard.count_by')}</span>
            <SimpleSelect
              aria-label={i18nT('pages.overview.memoryCarveCard.count_by')}
              style={{ flex: '0 0 180px' }}
              options={[...COUNT_AXES]}
              value={countBy}
              onChange={axis => { setCountBy(axis); setFilter(null) }}
            />
            {filter && (
              <>
                <Badge variant="ok">
                  {filter.axis} = <AxisValue value={filter.value} />
                </Badge>
                <Btn onClick={() => setFilter(null)}>
                  <X className="lucide-inline" aria-hidden="true" /> {i18nT('pages.overview.memoryCarveCard.clear_filter')}
                </Btn>
              </>
            )}
          </div>
          <MemoryScopeNotice error={counts.error} />
          {!counts.error && countRows.length === 0 && (
            <EmptyState
              icon={<Layers className="lucide-inline" />}
              title={i18nT('pages.overview.memoryCarveCard.nothing_is_stamped_on_this_axis_yet')}
              subtitle={i18nT('pages.overview.memoryCarveCard.a_count_appears_once_something_is_remembered_wit')}
            />
          )}
          {countRows.length > 0 && (
            <div className="flex flex-col gap-1">
              {countRows.map(([value, count]) => (
                <Clickable
                  key={value}
                  onClick={() => setFilter({ axis: countBy, value })}
                  className="flex justify-between items-center gap-3 px-2.5 py-1.5 rounded-md text-sm hover:bg-bg-hover transition-colors focus-ring"
                >
                  <AxisValue value={value} />
                  <span className="text-muted">{fmtNumber(count)}</span>
                </Clickable>
              ))}
            </div>
          )}
          {filter && (
            <div className="mt-3">
              <MemoryScopeNotice error={entries.error} />
              {!entries.error && (
                <table className="w-full border-collapse table-striped">
                  <thead>
                    <tr>
                      <th className="text-left text-muted text-[12px] uppercase tracking-[.04em] px-2.5 py-2 border-b border-border font-medium">{i18nT('pages.overview.memoryCarveCard.kind')}</th>
                      <th className="text-left text-muted text-[12px] uppercase tracking-[.04em] px-2.5 py-2 border-b border-border font-medium">{i18nT('pages.overview.memoryCarveCard.memory')}</th>
                      <th className="text-left text-muted text-[12px] uppercase tracking-[.04em] px-2.5 py-2 border-b border-border font-medium">{i18nT('pages.overview.memoryCarveCard.crew')}</th>
                      <th className="text-left text-muted text-[12px] uppercase tracking-[.04em] px-2.5 py-2 border-b border-border font-medium">{i18nT('pages.overview.memoryCarveCard.surface')}</th>
                    </tr>
                  </thead>
                  <tbody>
                    {entryRows.length === 0 ? (
                      <tr>
                        <td colSpan={4}>
                          <EmptyState
                            icon={<Layers className="lucide-inline" />}
                            title={i18nT('pages.overview.memoryCarveCard.no_live_memories_in_this_slice')}
                            subtitle={i18nT('pages.overview.memoryCarveCard.the_count_includes_only_live_rows_so_a_slice_can')}
                          />
                        </td>
                      </tr>
                    ) : entryRows.map(row => (
                      <tr key={row.id} className="hover:bg-bg-hover transition-colors">
                        <td className="px-2.5 py-2 border-b border-border text-sm"><Badge variant="muted">{row.kind}</Badge></td>
                        <td className="px-2.5 py-2 border-b border-border text-sm">
                          {(row.text ?? row.key ?? '').slice(0, TEXT_PREVIEW_CHARS)}
                        </td>
                        <td className="px-2.5 py-2 border-b border-border text-sm"><AxisValue value={row.crew ?? ''} /></td>
                        <td className="px-2.5 py-2 border-b border-border text-sm"><AxisValue value={row.surface ?? ''} /></td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}
            </div>
          )}
        </>
      )}
    </Card>
  )
}
