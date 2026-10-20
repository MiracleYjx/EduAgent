import {
  createContext,
  type ReactNode,
  useCallback,
  useContext,
  useEffect,
  useState,
} from "react";

import {
  exchangeOneTimeCode,
  SettingsApiError,
} from "../api/client";
import type { SessionExchangeResponse } from "../api/contracts";

export type SessionStatus = "checking" | "authorized" | "unauthorized";

export interface SessionError {
  code: string;
  message: string;
}

interface SettingsSessionValue {
  status: SessionStatus;
  token: string | null;
  error: SessionError | null;
  expire: (error?: SettingsApiError | SessionError | null) => void;
}

const SettingsSessionContext = createContext<SettingsSessionValue | null>(null);

function consumeAuthorizationCode(): string | null {
  const fragment = window.location.hash;
  if (!fragment) {
    return null;
  }
  // Clear the one-time code from the address bar before any network request.
  window.history.replaceState(
    null,
    "",
    `${window.location.pathname}${window.location.search}`,
  );
  const parameters = new URLSearchParams(fragment.replace(/^#/, ""));
  const code = parameters.get("code")?.trim();
  return code ? code : null;
}

function toSessionError(error: unknown): SessionError {
  if (error instanceof SettingsApiError) {
    return { code: error.code, message: error.message };
  }
  return {
    code: "AUTHORIZATION_FAILED",
    message: "设置授权未完成，请返回 EduAgent Launcher 重新授权。",
  };
}

export function SettingsSessionProvider({ children }: { children: ReactNode }) {
  // Consume during the first render, before BrowserRouter child effects can
  // navigate away from `/` and discard the one-time fragment.
  const [attempt] = useState(() => ({
    code: consumeAuthorizationCode(),
    exchange: null as Promise<SessionExchangeResponse> | null,
  }));
  const [status, setStatus] = useState<SessionStatus>("checking");
  const [token, setToken] = useState<string | null>(null);
  const [error, setError] = useState<SessionError | null>(null);

  const expire = useCallback(
    (cause?: SettingsApiError | SessionError | null) => {
      attempt.code = null;
      attempt.exchange = null;
      setToken(null);
      setStatus("unauthorized");
      setError(
        cause
          ? "code" in cause
            ? { code: cause.code, message: cause.message }
            : cause
          : null,
      );
    },
    [attempt],
  );

  useEffect(() => {
    // StrictMode replays effects, but a one-time code must never be exchanged
    // twice. Keep only the in-flight promise; discard the code immediately.
    if (attempt.exchange === null && attempt.code !== null) {
      const code = attempt.code;
      attempt.code = null;
      attempt.exchange = exchangeOneTimeCode(code);
    }
    if (attempt.exchange === null) {
      setStatus("unauthorized");
      return;
    }
    let cancelled = false;
    setStatus("checking");
    setError(null);
    attempt.exchange
      .then((response) => {
        if (cancelled) {
          return;
        }
        attempt.exchange = null;
        setToken(response.management_token);
        setStatus("authorized");
      })
      .catch((cause: unknown) => {
        if (cancelled) {
          return;
        }
        attempt.exchange = null;
        setToken(null);
        setStatus("unauthorized");
        setError(toSessionError(cause));
      });
    return () => {
      cancelled = true;
    };
  }, [attempt]);

  return (
    <SettingsSessionContext.Provider
      value={{ status, token, error, expire }}
    >
      {children}
    </SettingsSessionContext.Provider>
  );
}

export function useSettingsSession(): SettingsSessionValue {
  const value = useContext(SettingsSessionContext);
  if (value === null) {
    throw new Error("SettingsSessionProvider 未挂载。");
  }
  return value;
}
