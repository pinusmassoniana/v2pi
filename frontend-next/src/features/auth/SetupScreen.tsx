import { zodResolver } from "@hookform/resolvers/zod";
import { useState } from "react";
import { useForm, useWatch } from "react-hook-form";
import { z } from "zod";
import { api, errText } from "../../api/client";
import { Button } from "../../components/ui/Button";
import { Input } from "../../components/ui/Input";
import { AuthLayout, PasswordField } from "./AuthLayout";

function schemaFor(bootstrapRequired: boolean) {
  return z
    .object({
      username: z.string().min(1, "username is required"),
      password: z.string().min(8, "password too short (min 8 characters)"),
      confirm: z.string(),
      bootstrapToken:
        bootstrapRequired
          ? z.string().trim().min(1, "the one-time bootstrap token is required")
          : z.string(),
    })
    .refine((v) => v.password === v.confirm, { message: "passwords don't match", path: ["confirm"] });
}

interface Values { username: string; password: string; confirm: string; bootstrapToken: string }

export function SetupScreen({ bootstrapRequired, onDone }: { bootstrapRequired: boolean; onDone: () => void }) {
  const [error, setError] = useState("");
  const [shown, setShown] = useState(false);
  const { register, handleSubmit, control, formState } = useForm<Values>({
    resolver: zodResolver(schemaFor(bootstrapRequired)),
    defaultValues: { username: "", password: "", confirm: "", bootstrapToken: "" },
  });
  const values = useWatch({ control });
  const filled = !!values.username && !!values.password && !!values.confirm
    && (!bootstrapRequired || !!values.bootstrapToken?.trim());
  const { errors } = formState;
  const message = error || errors.password?.message || errors.confirm?.message
    || errors.bootstrapToken?.message || errors.username?.message;

  const submit = handleSubmit(async ({ username, password, bootstrapToken }) => {
    setError("");
    try {
      await api.setup(username, password, bootstrapToken.trim());   // creates the credential AND opens a session
      await api.ensureCsrf();
      onDone();
    } catch (err) {
      setError(errText(err, "setup failed"));
    }
  });

  return (
    <AuthLayout>
      <form onSubmit={submit} className="flex flex-col gap-3" noValidate>
        <p className="text-sm text-t2">Create the administrator account for this gateway.</p>
        {bootstrapRequired ? (
          <label className="flex flex-col gap-1 text-xs font-semibold text-t2">
            One-time bootstrap token
            <Input autoComplete="off" placeholder="token from the gateway console or file" {...register("bootstrapToken")} />
          </label>
        ) : null}
        <Input placeholder="username" aria-label="username" autoComplete="username" {...register("username")} />
        <PasswordField
          placeholder="password" aria-label="password" autoComplete="new-password"
          shown={shown} onToggle={() => setShown((v) => !v)} {...register("password")}
        />
        <PasswordField
          placeholder="confirm password" aria-label="confirm password" autoComplete="new-password"
          shown={shown} onToggle={() => setShown((v) => !v)} {...register("confirm")}
        />
        {message ? <p role="alert" className="err text-sm text-bad">{message}</p> : null}
        <Button type="submit" variant="primary" disabled={formState.isSubmitting || !filled}>
          {formState.isSubmitting ? "…" : "Create account"}
        </Button>
      </form>
    </AuthLayout>
  );
}
