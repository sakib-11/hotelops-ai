/**
 * ErrorBoundary - Production React Error Boundary
 *
 * Implements Subtask 40.8:
 * - Catches JavaScript runtime and rendering errors
 * - Prevents uncontrolled blank application state
 * - Provides graceful fallback UI styled with pastel design tokens
 * - Allows isolated recovery actions without restarting the entire desktop shell
 */

import { Component, type ErrorInfo, type ReactNode } from "react";
import { translateError } from "./translator";
import type { AppError } from "./types";
import { Button, Card, CardContent, CardHeader, CardFooter } from "@/components/ui";

interface ErrorBoundaryProps {
  children: ReactNode;
  fallback?: ReactNode | ((error: AppError, reset: () => void) => ReactNode);
  onReset?: () => void;
  featureName?: string;
}

interface ErrorBoundaryState {
  hasError: boolean;
  appError: AppError | null;
}

export class ErrorBoundary extends Component<ErrorBoundaryProps, ErrorBoundaryState> {
  constructor(props: ErrorBoundaryProps) {
    super(props);
    this.state = {
      hasError: false,
      appError: null,
    };
  }

  static getDerivedStateFromError(error: Error): ErrorBoundaryState {
    const appError = translateError(error);
    return {
      hasError: true,
      appError,
    };
  }

  override componentDidCatch(error: Error, errorInfo: ErrorInfo): void {
    console.error(
      `[ErrorBoundary${this.props.featureName ? `:${this.props.featureName}` : ""}] Uncaught error:`,
      error,
      errorInfo,
    );
  }

  handleReset = (): void => {
    this.setState({ hasError: false, appError: null });
    this.props.onReset?.();
  };

  handleReload = (): void => {
    window.location.reload();
  };

  override render(): ReactNode {
    if (this.state.hasError && this.state.appError) {
      if (typeof this.props.fallback === "function") {
        return this.props.fallback(this.state.appError, this.handleReset);
      }

      if (this.props.fallback) {
        return this.props.fallback;
      }

      return (
        <div
          style={{
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
            minHeight: "400px",
            padding: "var(--spacing-xl)",
            width: "100%",
          }}
          role="alert"
          aria-live="assertive"
        >
          <Card
            style={{
              maxWidth: "560px",
              width: "100%",
              borderColor: "var(--color-status-error)",
              boxShadow: "var(--shadow-elevated)",
            }}
          >
            <CardHeader
              title={
                this.props.featureName
                  ? `${this.props.featureName} encountered an error`
                  : "Something went wrong"
              }
              description={this.state.appError.message}
            />
            <CardContent>
              {import.meta.env.DEV && this.state.appError.technicalDetails && (
                <pre
                  style={{
                    backgroundColor: "var(--color-background-secondary)",
                    padding: "var(--spacing-md)",
                    borderRadius: "var(--border-radius-medium)",
                    fontSize: "var(--font-size-caption)",
                    fontFamily: "var(--font-family-mono)",
                    color: "var(--color-text-secondary)",
                    overflowX: "auto",
                    maxHeight: "180px",
                    marginTop: "var(--spacing-sm)",
                  }}
                >
                  {this.state.appError.technicalDetails}
                </pre>
              )}
            </CardContent>
            <CardFooter
              style={{ display: "flex", gap: "var(--spacing-md)", justifyContent: "flex-end" }}
            >
              <Button variant="secondary" size="md" onClick={this.handleReload}>
                Reload Application
              </Button>
              <Button variant="primary" size="md" onClick={this.handleReset}>
                Try Again
              </Button>
            </CardFooter>
          </Card>
        </div>
      );
    }

    return this.props.children;
  }
}
