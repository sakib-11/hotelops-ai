import {
  forwardRef,
  type InputHTMLAttributes,
  type SelectHTMLAttributes,
  type TextareaHTMLAttributes,
  useId,
} from "react";
import "./Input.css";

export interface InputProps extends InputHTMLAttributes<HTMLInputElement> {
  label?: string;
  helperText?: string;
  error?: string;
  leadingIcon?: React.ReactNode;
  trailingIcon?: React.ReactNode;
  fullWidth?: boolean;
}

export interface SelectProps extends SelectHTMLAttributes<HTMLSelectElement> {
  label?: string;
  helperText?: string;
  error?: string;
  options: { value: string; label: string; disabled?: boolean }[];
  placeholder?: string;
  fullWidth?: boolean;
}

export interface TextareaProps extends Omit<TextareaHTMLAttributes<HTMLTextAreaElement>, "rows"> {
  label?: string;
  helperText?: string;
  error?: string;
  fullWidth?: boolean;
  rows?: number;
}

export interface CheckboxProps extends Omit<InputHTMLAttributes<HTMLInputElement>, "type"> {
  label: string;
  description?: string;
  error?: string;
}

export interface ToggleProps extends Omit<InputHTMLAttributes<HTMLInputElement>, "type" | "size"> {
  label?: string;
  description?: string;
  error?: string;
  size?: "sm" | "md" | "lg";
}

const Input = forwardRef<HTMLInputElement, InputProps>(
  (
    {
      label,
      helperText,
      error,
      leadingIcon,
      trailingIcon,
      fullWidth = false,
      className = "",
      id: providedId,
      ...props
    },
    ref,
  ) => {
    const generatedId = useId();
    const id = providedId ?? generatedId;
    const helperId = helperText ? `${id}-helper` : undefined;
    const errorId = error ? `${id}-error` : undefined;
    const describedBy = [helperId, errorId].filter(Boolean).join(" ");

    const wrapperClassNames = [
      "form-field",
      fullWidth ? "form-field--full-width" : "",
      error ? "form-field--error" : "",
      className,
    ]
      .filter(Boolean)
      .join(" ");

    const inputClassNames = [
      "form-input",
      leadingIcon ? "form-input--leading-icon" : "",
      trailingIcon ? "form-input--trailing-icon" : "",
    ]
      .filter(Boolean)
      .join(" ");

    return (
      <div className={wrapperClassNames}>
        {label && (
          <label htmlFor={id} className="form-label">
            {label}
          </label>
        )}
        <div className="form-input-wrapper">
          {leadingIcon && (
            <span className="form-input-icon form-input-icon--leading" aria-hidden="true">
              {leadingIcon}
            </span>
          )}
          <input
            ref={ref}
            id={id}
            className={inputClassNames}
            aria-describedby={describedBy}
            aria-invalid={!!error}
            {...props}
          />
          {trailingIcon && (
            <span className="form-input-icon form-input-icon--trailing" aria-hidden="true">
              {trailingIcon}
            </span>
          )}
        </div>
        {error && (
          <p id={errorId} className="form-error" role="alert">
            {error}
          </p>
        )}
        {helperText && !error && (
          <p id={helperId} className="form-helper">
            {helperText}
          </p>
        )}
      </div>
    );
  },
);

Input.displayName = "Input";

const Select = forwardRef<HTMLSelectElement, SelectProps>(
  (
    {
      label,
      helperText,
      error,
      options,
      placeholder,
      fullWidth = false,
      className = "",
      id: providedId,
      ...props
    },
    ref,
  ) => {
    const generatedId = useId();
    const id = providedId ?? generatedId;
    const helperId = helperText ? `${id}-helper` : undefined;
    const errorId = error ? `${id}-error` : undefined;
    const describedBy = [helperId, errorId].filter(Boolean).join(" ");

    const wrapperClassNames = [
      "form-field",
      fullWidth ? "form-field--full-width" : "",
      error ? "form-field--error" : "",
      className,
    ]
      .filter(Boolean)
      .join(" ");

    return (
      <div className={wrapperClassNames}>
        {label && (
          <label htmlFor={id} className="form-label">
            {label}
          </label>
        )}
        <div className="form-select-wrapper">
          <select
            ref={ref}
            id={id}
            className="form-select"
            aria-describedby={describedBy}
            aria-invalid={!!error}
            {...props}
          >
            {placeholder && (
              <option value="" disabled>
                {placeholder}
              </option>
            )}
            {options.map((option) => (
              <option key={option.value} value={option.value} disabled={option.disabled}>
                {option.label}
              </option>
            ))}
          </select>
          <svg
            className="form-select-icon"
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            strokeWidth="2"
            aria-hidden="true"
          >
            <polyline points="6 9 12 15 18 9" />
          </svg>
        </div>
        {error && (
          <p id={errorId} className="form-error" role="alert">
            {error}
          </p>
        )}
        {helperText && !error && (
          <p id={helperId} className="form-helper">
            {helperText}
          </p>
        )}
      </div>
    );
  },
);

Select.displayName = "Select";

const Textarea = forwardRef<HTMLTextAreaElement, TextareaProps>(
  (
    {
      label,
      helperText,
      error,
      fullWidth = false,
      rows = 3,
      className = "",
      id: providedId,
      ...props
    },
    ref,
  ) => {
    const generatedId = useId();
    const id = providedId ?? generatedId;
    const helperId = helperText ? `${id}-helper` : undefined;
    const errorId = error ? `${id}-error` : undefined;
    const describedBy = [helperId, errorId].filter(Boolean).join(" ");

    const wrapperClassNames = [
      "form-field",
      fullWidth ? "form-field--full-width" : "",
      error ? "form-field--error" : "",
      className,
    ]
      .filter(Boolean)
      .join(" ");

    return (
      <div className={wrapperClassNames}>
        {label && (
          <label htmlFor={id} className="form-label">
            {label}
          </label>
        )}
        <textarea
          ref={ref}
          id={id}
          className="form-textarea"
          rows={rows}
          aria-describedby={describedBy}
          aria-invalid={!!error}
          {...props}
        />
        {error && (
          <p id={errorId} className="form-error" role="alert">
            {error}
          </p>
        )}
        {helperText && !error && (
          <p id={helperId} className="form-helper">
            {helperText}
          </p>
        )}
      </div>
    );
  },
);

Textarea.displayName = "Textarea";

const Checkbox = forwardRef<HTMLInputElement, CheckboxProps>(
  ({ label, description, error, className = "", id: providedId, ...props }, ref) => {
    const generatedId = useId();
    const id = providedId ?? generatedId;
    const errorId = error ? `${id}-error` : undefined;

    const wrapperClassNames = ["form-checkbox", error ? "form-checkbox--error" : "", className]
      .filter(Boolean)
      .join(" ");

    const inputRef = ref as React.RefObject<HTMLInputElement>;

    return (
      <div className={wrapperClassNames}>
        <label htmlFor={id} className="form-checkbox-label">
          <div className="form-checkbox-input-wrapper">
            <input
              ref={inputRef}
              id={id}
              type="checkbox"
              className="form-checkbox-input"
              aria-describedby={errorId}
              aria-invalid={!!error}
              {...props}
            />
            <span className="form-checkbox-indicator" aria-hidden="true">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5">
                <polyline points="20 6 9 17 4 12" />
              </svg>
            </span>
            <span className="form-checkbox-indeterminate" aria-hidden="true">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5">
                <line x1="5" y1="12" x2="19" y2="12" />
              </svg>
            </span>
          </div>
          <div className="form-checkbox-text">
            <span className="form-checkbox-label-text">{label}</span>
            {description && <span className="form-checkbox-description">{description}</span>}
          </div>
        </label>
        {error && (
          <p id={errorId} className="form-error" role="alert">
            {error}
          </p>
        )}
      </div>
    );
  },
);

Checkbox.displayName = "Checkbox";

const Toggle = forwardRef<HTMLInputElement, ToggleProps>(
  ({ label, description, error, size = "md", className = "", id: providedId, ...props }, ref) => {
    const generatedId = useId();
    const id = providedId ?? generatedId;
    const errorId = error ? `${id}-error` : undefined;

    const sizeClasses = {
      sm: "form-toggle--sm",
      md: "form-toggle--md",
      lg: "form-toggle--lg",
    };

    const wrapperClassNames = [
      "form-toggle",
      sizeClasses[size],
      error ? "form-toggle--error" : "",
      className,
    ]
      .filter(Boolean)
      .join(" ");

    return (
      <div className={wrapperClassNames}>
        <div className="form-toggle-main">
          <label htmlFor={id} className="form-toggle-label">
            <div className="form-toggle-track">
              <input
                ref={ref}
                id={id}
                type="checkbox"
                className="form-toggle-input"
                aria-describedby={errorId}
                aria-invalid={!!error}
                {...props}
              />
              <span className="form-toggle-thumb" aria-hidden="true" />
            </div>
          </label>
          {(label ?? description) && (
            <div className="form-toggle-text">
              {label && <span className="form-toggle-label-text">{label}</span>}
              {description && <span className="form-toggle-description">{description}</span>}
            </div>
          )}
        </div>
        {error && (
          <p id={errorId} className="form-error" role="alert">
            {error}
          </p>
        )}
      </div>
    );
  },
);

Toggle.displayName = "Toggle";

export { Input, Select, Textarea, Checkbox, Toggle };
