import { zodResolver } from "@hookform/resolvers/zod";
import { useState } from "react";
import { useForm } from "react-hook-form";
import { z } from "zod";
import { api, errText } from "../../api/client";
import { Button } from "../../components/ui/Button";
import { Input } from "../../components/ui/Input";
import { AuthLayout, PasswordField } from "./AuthLayout";

const schema = z.object({ username: z.string().min(1), password: z.string().min(1) });
type Values = z.infer<typeof schema>;

export function LoginScreen({ onLogin }: { onLogin: () => void }) {
  const [error, setError] = useState("");
  const [shown, setShown] = useState(false);
  const { register, handleSubmit, formState } = useForm<Values>({
    resolver: zodResolver(schema),
    mode: "onChange",
    defaultValues: { username: "", password: "" },
  });

  const submit = handleSubmit(async ({ username, password }) => {
    setError("");
    try {
      await api.login(username, password);
      await api.ensureCsrf();
      onLogin();
    } catch (err) {
      setError(errText(err, "login failed"));
    }
  });

  return (
    <AuthLayout>
      <form onSubmit={submit} className="flex flex-col gap-3" noValidate>
        <Input placeholder="username" aria-label="username" autoComplete="username" {...register("username")} />
        <PasswordField
          placeholder="password" aria-label="password" autoComplete="current-password"
          shown={shown} onToggle={() => setShown((v) => !v)} {...register("password")}
        />
        {error ? <p role="alert" className="err text-sm text-bad">{error}</p> : null}
        <Button type="submit" variant="primary" disabled={formState.isSubmitting || !formState.isValid}>
          {formState.isSubmitting ? "…" : "Log in"}
        </Button>
      </form>
    </AuthLayout>
  );
}
