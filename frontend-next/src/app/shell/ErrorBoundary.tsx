import { Component, type ReactNode } from "react";
import { ErrorState } from "../../components/ui/States";

interface Props { resetKey: string; children: ReactNode }
interface State { error: Error | null; key: string }

/** One per screen: a crash shows a card with Reload, and the shell, status and navigation keep working. */
export class ErrorBoundary extends Component<Props, State> {
  state: State = { error: null, key: this.props.resetKey };

  static getDerivedStateFromError(error: Error): Partial<State> {
    return { error };
  }

  static getDerivedStateFromProps(props: Props, state: State): Partial<State> | null {
    return props.resetKey !== state.key ? { error: null, key: props.resetKey } : null;
  }

  render() {
    if (this.state.error) {
      return (
        <ErrorState
          message="This screen crashed. The rest of the panel still works."
          retryLabel="Reload"
          onRetry={() => this.setState({ error: null })}
        />
      );
    }
    return this.props.children;
  }
}
