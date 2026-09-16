import { useId, type ReactNode } from "react";
import { Controller, useWatch } from "react-hook-form";
import type { Network } from "../../api/client";
import { confirm } from "../../components/confirm";
import { KeyValueRows } from "../../components/data/KeyValueRows";
import { SegmentedField, TextField } from "../../components/ui/Field";
import { Toggle } from "../../components/ui/Toggle";
import { cn } from "../../lib/cn";
import { DISARM_CONFIRM, poolIssue } from "./networkForm";
import type { NetworkFormState } from "./useNetworkForm";

/** Help under a field; its error replaces it. */
export function Help({ children }: { children: ReactNode }) {
  return <p className="-mt-0.5 text-[11px] text-t3">{children}</p>;
}

/** A switch with its visible label and text, the switch named by the label. */
export function SwitchRow({ label, text, checked, disabled, onChange, className }: {
  label: string; text: ReactNode; checked: boolean; disabled?: boolean; onChange: (on: boolean) => void; className?: string;
}) {
  return (
    <div className={cn("flex items-start gap-3", className)}>
      <div className="pt-0.5"><Toggle label={label} checked={checked} disabled={disabled} onCheckedChange={onChange} /></div>
      <div className="min-w-0">
        <p className="text-sm font-semibold text-t1">{label}</p>
        <p className="text-xs leading-relaxed text-t3">{text}</p>
      </div>
    </div>
  );
}

/** The /24 an address sits in, as "a.b.c". */
function net24(ip: string): string {
  return ip.trim().split(".").slice(0, 3).join(".");
}

/** W2: the six segment fields in pairs, the DHCP pool's error under its pair and tied to both of its fields. */
export function SegmentFields({ state, disabled }: { state: NetworkFormState; disabled: boolean }) {
  const poolId = useId();
  const { form } = state;
  const { register, control, formState: { errors } } = form;
  const [ip, dhcpStart, dhcpEnd, clientDns] = useWatch({ control, name: ["ip", "dhcpStart", "dhcpEnd", "clientDns"] });
  const pool = poolIssue({ ip, dhcpStart, dhcpEnd });
  const poolProps = pool ? { "aria-invalid": true, "aria-describedby": poolId } : {};
  const error = (message: string | undefined, help: ReactNode) => (message ? null : <Help>{help}</Help>);
  return (
    <div className="grid gap-x-3 gap-y-2.5 sm:grid-cols-2">
      <div className="flex flex-col gap-1">
        <TextField label="Segment interface" autoComplete="off" spellCheck={false} className="font-mono" disabled={disabled} error={errors.iface?.message} {...register("iface")} />
        {error(errors.iface?.message, "plain interface name · up to 15 characters")}
      </div>
      <div className="flex flex-col gap-1">
        <TextField label="Gateway IPv4 address" placeholder="192.168.10.2" inputMode="decimal" autoComplete="off" className="font-mono" disabled={disabled} error={errors.ip?.message} {...register("ip")} />
        {error(errors.ip?.message, net24(ip) ? `segment ${net24(ip)}.0/24` : "IPv4 address")}
      </div>
      <div className="flex flex-col gap-1">
        <TextField label="DHCP range start" inputMode="decimal" autoComplete="off" className="font-mono" disabled={disabled} error={errors.dhcpStart?.message} {...register("dhcpStart")} {...(errors.dhcpStart ? {} : poolProps)} />
        {error(errors.dhcpStart?.message, "IPv4 address")}
      </div>
      <div className="flex flex-col gap-1">
        <TextField label="DHCP range end" inputMode="decimal" autoComplete="off" className="font-mono" disabled={disabled} error={errors.dhcpEnd?.message} {...register("dhcpEnd")} {...(errors.dhcpEnd ? {} : poolProps)} />
        {error(errors.dhcpEnd?.message, "IPv4 address")}
      </div>
      {pool ? <p id={poolId} className="text-[11px] text-bad sm:col-span-2">{pool}</p> : null}
      <div className="flex flex-col gap-1">
        <TextField label="Client DNS" inputMode="decimal" autoComplete="off" className="font-mono" disabled={disabled} error={errors.clientDns?.message} {...register("clientDns")} />
        {error(errors.clientDns?.message, clientDns.trim() !== "" && clientDns.trim() === ip.trim() ? "= the gateway IP" : "IPv4 address")}
      </div>
      <div className="flex flex-col gap-1">
        <TextField label="DHCP lease" autoComplete="off" className="font-mono" disabled={disabled} error={errors.lease?.message} {...register("lease")} />
        {error(errors.lease?.message, "a lease time like '12h', '3600', or 'infinite'")}
      </div>
    </div>
  );
}

function uplinkLabel(up: boolean | null): string {
  return up === null ? "unknown" : up ? "✓ up" : "✕ down";
}

/**
 * W3: LAN access and IPv6, staged into the form. The IPv6 fields show while IPv6 is on — and also while one of them
 * holds an error with IPv6 off, since the backend checks them regardless and Apply must never be blocked by a field
 * nobody can see.
 */
export function LanIpv6Fields({ state, network, disabled, desktop }: { state: NetworkFormState; network: Network; disabled: boolean; desktop: boolean }) {
  const { form } = state;
  const { register, control, formState: { errors } } = form;
  const [ipv6, ip6Mode] = useWatch({ control, name: ["ipv6", "ip6Mode"] });
  const showV6 = ipv6 || errors.clientDns6 !== undefined || errors.ip6Static !== undefined;
  const { status } = network;
  return (
    <div className="flex flex-col gap-3">
      <Controller
        control={control}
        name="lanAccess"
        render={({ field }) => (
          <SwitchRow label="LAN access" text="let segment clients reach the home LAN directly (internet still tunnel-only)." checked={field.value} disabled={disabled} onChange={field.onChange} />
        )}
      />
      <Controller
        control={control}
        name="ipv6"
        render={({ field }) => (
          <SwitchRow
            label="IPv6 (tunnel)"
            text={`carry segment client IPv6 through the tunnel. Off keeps v6 blocked.${desktop ? " Flipping it while the tunnel runs restarts xray." : ""}`}
            checked={field.value}
            disabled={disabled}
            onChange={field.onChange}
          />
        )}
      />
      {showV6 ? (
        <div className="ml-12 flex min-w-0 flex-col gap-2.5">
          <div className="flex flex-wrap items-end gap-2.5">
            <SegmentedField
              legend="Segment IPv6 /64"
              options={[{ value: "static", label: "static" }, { value: "auto", label: "auto" }, { value: "ula", label: "ULA" }]}
              radio={register("ip6Mode")}
              disabled={disabled}
            />
            {ip6Mode === "static" ? (
              <div className="flex min-w-48 flex-1 flex-col gap-1">
                <TextField label="Static /64" placeholder="2001:db8:0:2::/64" autoComplete="off" spellCheck={false} className="font-mono" disabled={disabled} error={errors.ip6Static?.message} {...register("ip6Static")} />
                {errors.ip6Static ? null : <Help>a /64, normalised to its network</Help>}
              </div>
            ) : null}
          </div>
          <p className="text-[11px] text-t3">static, auto for DHCPv6-PD, blank = ULA</p>
          <div className="flex flex-col gap-1">
            <TextField label="Client DNS (v6)" placeholder="2606:4700:4700::1111" autoComplete="off" spellCheck={false} className="font-mono" disabled={disabled} error={errors.clientDns6?.message} {...register("clientDns6")} />
            {errors.clientDns6 ? null : <Help>IPv6 address</Help>}
          </div>
          <KeyValueRows
            className="sm:grid-cols-3"
            rows={[
              { key: "prefix source", value: status.ipv6_prefix_source ?? "unknown" },
              ...(ip6Mode === "auto" ? [{ key: "IPv6 prefix", value: status.ipv6_prefix ?? "waiting for a delegated prefix" }] : []),
              { key: "v6 uplink", value: uplinkLabel(status.uplink6) },
            ]}
          />
        </div>
      ) : null}
    </div>
  );
}

/** W6: the kill-switch toggle, staged into the form. Disarming asks first; arming does not. */
export function KillSwitchToggle({ state, disabled, applyHint }: { state: NetworkFormState; disabled: boolean; applyHint: boolean }) {
  const { control, setValue, trigger } = state.form;
  async function change(on: boolean) {
    if (!on && !(await confirm(DISARM_CONFIRM, { confirmLabel: "Disarm" }))) return;
    setValue("killSwitch", on, { shouldDirty: true });
    // setValue alone only revalidates the field it touched (react-hook-form, even with a schema
    // resolver, does not re-check the rest of the form) — so re-check every field here, the same
    // way register() and Controller.onChange do for the fields wired through them, or a v6 field
    // holding a bad saved value would clear only on the next edit, never on Apply.
    await trigger();
  }
  return (
    <Controller
      control={control}
      name="killSwitch"
      render={({ field }) => (
        <SwitchRow
          label="Fail-closed kill-switch"
          text={<>When armed, any client traffic that can't reach a healthy upstream is dropped at nftables — no plaintext leak around the tunnel.{applyHint && state.dirty ? <b className="text-t1"> Apply to host to take effect.</b> : null}</>}
          checked={field.value}
          disabled={disabled}
          onChange={(on) => void change(on)}
        />
      )}
    />
  );
}
