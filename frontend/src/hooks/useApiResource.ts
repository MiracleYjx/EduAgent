import { useCallback, useEffect, useRef, useState } from "react";

import { SettingsApiError } from "../api/client";
import { useSettingsSession } from "../auth/SettingsSession";

export interface ApiResourceState<T> {
  data: T | null;
  error: unknown;
  loading: boolean;
  reload: () => void;
}

export function useApiResource<T>(
  loader: ((signal: AbortSignal) => Promise<T>) | null,
  key: string,
): ApiResourceState<T> {
  const loaderRef = useRef(loader);
  loaderRef.current = loader;
  const [version, setVersion] = useState(0);
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [loading, setLoading] = useState(loader !== null);

  useEffect(() => {
    const currentLoader = loaderRef.current;
    if (currentLoader === null) {
      setData(null);
      setError(null);
      setLoading(false);
      return;
    }
    let active = true;
    const controller = new AbortController();
    setData(null);
    setLoading(true);
    setError(null);
    currentLoader(controller.signal)
      .then((value) => {
        if (active) {
          setData(value);
          setLoading(false);
        }
      })
      .catch((cause: unknown) => {
        if (active) {
          setError(cause);
          setLoading(false);
        }
      });
    return () => {
      active = false;
      controller.abort();
    };
  }, [key, version]);

  const reload = useCallback(() => {
    setVersion((current) => current + 1);
  }, []);

  return { data, error, loading, reload };
}

export function useSessionResource<T>(
  loader: (token: string, signal?: AbortSignal) => Promise<T>,
): ApiResourceState<T> {
  const session = useSettingsSession();
  const token = session.token;
  const load =
    token === null || session.status !== "authorized"
      ? null
      : (signal: AbortSignal) => loader(token, signal);
  const resource = useApiResource(
    load,
    session.status === "authorized" ? `authorized:${token}` : "anonymous",
  );

  useEffect(() => {
    if (
      token !== null &&
      resource.error instanceof SettingsApiError &&
      (resource.error.status === 401 || resource.error.status === 403)
    ) {
      session.expire(resource.error);
    }
  }, [resource.error, session, token]);

  return resource;
}
