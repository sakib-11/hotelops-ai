import { useState } from "react";
import { useForm } from "react-hook-form";
import {
  Button,
  Input,
  Card,
  CardHeader,
  CardContent,
  CardFooter,
  ErrorState,
} from "@/components/ui";
import { HotelIcon } from "@/app/shell/icons";
import { useAuthActions, useAuthError } from "../hooks/useAuthStore";
import type { LoginCredentials } from "../types";
import "./LoginScreen.css";

export function LoginScreen() {
  const error = useAuthError();
  const { login, clearError } = useAuthActions();
  const [isSubmitting, setIsSubmitting] = useState(false);

  const {
    register,
    handleSubmit,
    formState: { errors },
  } = useForm<LoginCredentials>({
    mode: "onBlur",
  });

  const onSubmit = (data: LoginCredentials) => {
    setIsSubmitting(true);
    // Fire and forget - errors handled by auth store
    void login(data)
      .then(
        // eslint-disable-next-line @typescript-eslint/no-empty-function
        function onFulfilled() {},
        // eslint-disable-next-line @typescript-eslint/no-empty-function
        function onRejected() {},
      )
      .finally(() => {
        setIsSubmitting(false);
      });
  };

  const handleClearError = () => {
    clearError();
  };

  return (
    <div className="login-screen">
      <div className="login-container">
        <Card className="login-card">
          <CardHeader
            title="Welcome back"
            description="Sign in to access your operational dashboard"
            className="login-header"
            action={
              <div className="login-brand">
                <HotelIcon className="login-brand-icon" aria-hidden="true" />
                <span className="login-brand-text">HotelOps AI</span>
              </div>
            }
          />

          <CardContent className="login-content">
            {error && (
              <ErrorState
                title="Sign in failed"
                message={error.message}
                code={error.code}
                onDismiss={handleClearError}
                size="md"
              />
            )}
            <form
              // eslint-disable-next-line @typescript-eslint/no-misused-promises
              onSubmit={handleSubmit(onSubmit)}
              className="login-form"
              noValidate
            >
              <Input
                label="Email"
                type="email"
                placeholder="you@hotelops.ai"
                autoComplete="email"
                error={errors.email?.message}
                {...register("email", {
                  required: "Email is required",
                  pattern: {
                    value: /^[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}$/i,
                    message: "Enter a valid email address",
                  },
                })}
                fullWidth
                disabled={isSubmitting}
              />

              <Input
                label="Password"
                type="password"
                placeholder="Enter your password"
                autoComplete="current-password"
                error={errors.password?.message}
                {...register("password", {
                  required: "Password is required",
                  minLength: {
                    value: 1,
                    message: "Password is required",
                  },
                })}
                fullWidth
                disabled={isSubmitting}
              />

              <div className="login-actions">
                <Button
                  type="submit"
                  variant="primary"
                  size="lg"
                  fullWidth
                  loading={isSubmitting}
                  disabled={isSubmitting}
                >
                  {isSubmitting ? "Signing in..." : "Sign in"}
                </Button>
              </div>
            </form>

            <div className="login-hint">
              <p>Demo credentials:</p>
              <ul>
                <li>
                  <code>admin@hotelops.ai</code> / <code>admin123</code> (Admin)
                </li>
                <li>
                  <code>manager@hotelops.ai</code> / <code>manager123</code> (Manager)
                </li>
                <li>
                  <code>operator@hotelops.ai</code> / <code>operator123</code> (Operator)
                </li>
              </ul>
            </div>
          </CardContent>

          <CardFooter className="login-footer">
            <p className="login-version">HotelOps AI v0.1.0</p>
          </CardFooter>
        </Card>
      </div>
    </div>
  );
}
