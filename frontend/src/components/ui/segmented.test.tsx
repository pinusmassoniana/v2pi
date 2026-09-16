import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { useForm, useWatch } from "react-hook-form";
import { describe, expect, it, vi } from "vitest";
import { SegmentedField } from "./Field";

// Module scope: a form that registers the radios, as the node form does.
function Registered({ onValue }: { onValue: (value: string) => void }) {
  const { register, control } = useForm({ defaultValues: { transport: "vision" } });
  onValue(useWatch({ control, name: "transport" }));
  return <SegmentedField legend="Transport" options={["vision", "xhttp"]} radio={register("transport")} />;
}

function Controlled({ onPick }: { onPick: (value: string) => void }) {
  const [value, setValue] = useState("proxy");
  return (
    <SegmentedField
      legend="Rule 2 action"
      name="rule-2-action"
      options={[{ value: "direct", className: "has-[:checked]:text-g1" }, { value: "proxy" }, { value: "block", label: "block (drop)" }]}
      value={value}
      onValueChange={(next) => { setValue(next); onPick(next); }}
    />
  );
}

describe("SegmentedField", () => {
  it("registered with react-hook-form: a radio group named by its legend that writes the form value", async () => {
    const seen: string[] = [];
    render(<Registered onValue={(value) => seen.push(value)} />);
    const group = screen.getByRole("group", { name: "Transport" });
    expect(within(group).getByRole("radio", { name: "vision" })).toBeChecked();
    await userEvent.click(within(group).getByRole("radio", { name: "xhttp" }));
    expect(within(group).getByRole("radio", { name: "xhttp" })).toBeChecked();
    expect(seen.at(-1)).toBe("xhttp");
  });

  it("controlled: checks its value, reports a pick, shows an option's label and takes its classes", async () => {
    const onPick = vi.fn();
    render(<Controlled onPick={onPick} />);
    const group = screen.getByRole("group", { name: "Rule 2 action" });
    const radios = within(group).getAllByRole("radio");
    expect(radios.map((radio) => radio.getAttribute("name"))).toEqual(["rule-2-action", "rule-2-action", "rule-2-action"]);
    expect(within(group).getByRole("radio", { name: "proxy" })).toBeChecked();
    await userEvent.click(within(group).getByRole("radio", { name: "block (drop)" }));
    expect(onPick).toHaveBeenCalledWith("block");
    expect(within(group).getByRole("radio", { name: "block (drop)" })).toBeChecked();
    expect(within(group).getByRole("radio", { name: "direct" }).closest("label")).toHaveClass("has-[:checked]:text-g1");
  });

  it("a disabled field disables every option", () => {
    render(<SegmentedField legend="QUIC" options={["allow", "drop"]} value="allow" onValueChange={() => {}} disabled />);
    for (const radio of screen.getAllByRole("radio")) expect(radio).toBeDisabled();
  });

  it("two controlled fields without a name never share a radio group", () => {
    render(
      <>
        <SegmentedField legend="A" options={["x", "y"]} value="x" onValueChange={() => {}} />
        <SegmentedField legend="B" options={["x", "y"]} value="y" onValueChange={() => {}} />
      </>,
    );
    const [a, b] = [screen.getByRole("group", { name: "A" }), screen.getByRole("group", { name: "B" })];
    expect(within(a).getAllByRole("radio")[0]!.getAttribute("name")).not.toBe(within(b).getAllByRole("radio")[0]!.getAttribute("name"));
    expect(within(a).getByRole("radio", { name: "x" })).toBeChecked();
    expect(within(b).getByRole("radio", { name: "y" })).toBeChecked();
  });
});
