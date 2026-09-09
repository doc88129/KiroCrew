import { useEffect, useRef, useState } from 'react'
import { useMutation } from '@tanstack/react-query'
import { ShieldCheck } from 'lucide-react'
import { api } from '../api/client'
import { i18nT } from '../i18n/t'
import { pendingSlotSwitchTarget, performSlotSwitch } from '../lib/slotSwitch'
import { useAppDispatch } from '../store'
import { updateSlot } from '../store/dashboardSlice'
import ErrorNotice from './ErrorNotice'
import SimpleSelect from './SimpleSelect'

export type AdvisorOverride = 'inherit' | 'on' | 'off'

const ADVISOR_OVERRIDES: AdvisorOverride[] = ['inherit', 'on', 'off']

export default function AdvisorOverrideControl({
  slot,
  currentOverride = 'inherit',
}: {
  slot: string
  currentOverride?: AdvisorOverride
}) {
  const dispatch = useAppDispatch()
  const [selected, setSelected] = useState<AdvisorOverride>(currentOverride)
  const authoritativeRef = useRef(currentOverride)
  const intentRef = useRef(0)
  authoritativeRef.current = currentOverride

  useEffect(() => {
    if (pendingSlotSwitchTarget('advisor_override', slot) === null) setSelected(currentOverride)
  }, [currentOverride, slot])

  const mutation = useMutation({
    mutationFn: ({ value }: { value: AdvisorOverride; intent: number }) => performSlotSwitch(
      'advisor_override',
      slot,
      value,
      async () => {
        const response = await api.chatSlotAdvisorOverride(slot, value)
        return response.advisor_override ?? value
      },
      advisorOverride => dispatch(updateSlot({ key: slot, advisor_override: advisorOverride })),
    ),
    onMutate: ({ value }) => setSelected(value),
    onError: (_error, { intent }) => {
      if (intent === intentRef.current) setSelected(authoritativeRef.current)
    },
  })

  const persist = (next: string) => {
    mutation.mutate({ value: next as AdvisorOverride, intent: ++intentRef.current })
  }

  const labels = [
    i18nT('components.advisorOverrideControl.inherit'),
    i18nT('components.advisorOverrideControl.on'),
    i18nT('components.advisorOverrideControl.off'),
  ]

  return (
    <div className="shrink-0 border-t border-border px-3 py-2">
      <div className="flex items-center justify-between gap-3">
        <span className="inline-flex items-center gap-1.5 text-[13px] text-muted">
          <ShieldCheck className="lucide-inline" />
          {i18nT('components.advisorOverrideControl.advisor')}
        </span>
        <SimpleSelect
          options={ADVISOR_OVERRIDES}
          optionLabels={labels}
          value={selected}
          onChange={persist}
          aria-label={i18nT('components.advisorOverrideControl.advisor_mode')}
          className="h-7 min-w-[112px] px-2 py-1 text-[12px]"
        />
      </div>
      <p className="mt-1 text-[12px] leading-4 text-muted">
        {i18nT('components.advisorOverrideControl.helper')}
      </p>
      {mutation.isError && (
        <div className="mt-1.5">
          {/* No hand-off: the chat composer draft beneath this popover is unsaved. */}
          <ErrorNotice
            message={i18nT('components.advisorOverrideControl.update_failed')}
            variant="inline"
            onDismiss={() => mutation.reset()}
          />
        </div>
      )}
    </div>
  )
}
