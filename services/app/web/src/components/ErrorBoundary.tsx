import { Component, type ErrorInfo, type ReactNode } from "react";

interface State {
  error: Error | null;
  stack: string | null;
}

/**
 * Sem isto, qualquer exceção na montagem desmonta a árvore e o usuário vê só uma
 * tela preta, sem pista nenhuma do que aconteceu.
 */
export class ErrorBoundary extends Component<{ children: ReactNode }, State> {
  state: State = { error: null, stack: null };

  static getDerivedStateFromError(error: Error): Partial<State> {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    console.error("erro na interface:", error, info.componentStack);
    this.setState({ stack: info.componentStack ?? null });
  }

  render() {
    const { error, stack } = this.state;
    if (!error) return this.props.children;

    return (
      <div className="grid h-full place-items-center p-8">
        <div className="w-full max-w-2xl rounded-md border border-red-900/60 bg-red-950/40 p-4">
          <h1 className="text-sm font-medium text-red-300">A interface quebrou</h1>
          <p className="mt-1 text-xs text-red-400/90">{error.message}</p>
          {stack && (
            <pre className="mt-3 max-h-64 overflow-auto rounded bg-zinc-950/60 p-2 text-[10px] leading-relaxed text-zinc-500">
              {stack.trim()}
            </pre>
          )}
          <button
            onClick={() => window.location.reload()}
            className="mt-4 rounded-md border border-zinc-700 bg-zinc-900 px-3 py-1.5 text-xs text-zinc-200 hover:bg-zinc-800"
          >
            recarregar
          </button>
        </div>
      </div>
    );
  }
}
