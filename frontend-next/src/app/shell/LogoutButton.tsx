import { Button } from "../../components/ui/Button";
import { useAuth } from "../auth";

export function LogoutButton({ className }: { className?: string }) {
  const { logout } = useAuth();
  return <Button variant="ghost" className={className} onClick={() => void logout()}>Log out</Button>;
}
