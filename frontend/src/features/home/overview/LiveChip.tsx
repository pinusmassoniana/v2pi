import { Chip } from "../../../components/data/Chip";

/** Whether the live traffic feed is flowing, in the same words on every card that shows it: live · connecting… · stats off. */
export function LiveChip({ disabled, fresh }: { disabled: boolean; fresh: boolean }) {
  if (disabled) return <Chip tone="neutral">stats off</Chip>;
  return fresh ? <Chip tone="ok">live</Chip> : <Chip tone="neutral">connecting…</Chip>;
}
