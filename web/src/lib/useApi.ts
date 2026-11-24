"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { errorMessage, errorStatus, isCanceled } from "./errors";

export interface Loaded<T> {
  data: T | null;
  error: string | null;
  status: number | undefined;
  loading: boolean;
  reload: () => void;
  setData: (update: (prev: T | null) => T | null) => void;
}

export function useApi<T>(load: () => Promise<T>, deps: unknown[]): Loaded<T> {
  const [data, setDataState] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [status, setStatus] = useState<number | undefined>(undefined);
  const [loading, setLoading] = useState(true);
  const [tick, setTick] = useState(0);
  const seq = useRef(0);

  const latest = useRef(load);
  const key = JSON.stringify(deps);

  useEffect(() => {
    latest.current = load;
  });

  useEffect(() => {
    const mine = ++seq.current;
    // eslint-disable-next-line react-hooks/set-state-in-effect -- fetch on mount and when inputs change
    setLoading(true);
    latest.current().then(
      (value) => {
        if (mine !== seq.current) return;
        setDataState(value);
        setError(null);
        setStatus(undefined);
        setLoading(false);
      },
      (err) => {
        if (mine !== seq.current || isCanceled(err)) return;
        setError(errorMessage(err));
        setStatus(errorStatus(err));
        setLoading(false);
      }
    );
  }, [key, tick]);

  const reload = useCallback(() => setTick((t) => t + 1), []);
  const setData = useCallback(
    (update: (prev: T | null) => T | null) => setDataState((prev) => update(prev)),
    []
  );
  return { data, error, status, loading, reload, setData };
}

export interface Paged<T> {
  items: T[];
  loading: boolean;
  error: string | null;
  reload: () => void;
  hasMore: boolean;
  loadMore: () => Promise<void>;
  loadingMore: boolean;
  moreError: string | null;
  update: (fn: (items: T[]) => T[]) => void;
}

export function usePaged<T>(
  load: (cursor?: string) => Promise<{ items: T[]; next_cursor?: string | null }>,
  deps: unknown[]
): Paged<T> {
  const first = useApi(() => load(), deps);
  const [extra, setExtra] = useState<{ page: unknown; items: T[]; cursor: string | null } | null>(null);
  const [edited, setEdited] = useState<{ page: unknown; items: T[] } | null>(null);
  const [loadingMore, setLoadingMore] = useState(false);
  const [moreError, setMoreError] = useState<string | null>(null);

  const page = first.data;
  const more = extra && extra.page === page ? extra : null;
  const loaded = [...(page?.items ?? []), ...(more?.items ?? [])];
  const items = edited && edited.page === page ? edited.items : loaded;
  const cursor = more ? more.cursor : (page?.next_cursor ?? null);

  const loadMore = async () => {
    if (!cursor) return;
    setLoadingMore(true);
    setMoreError(null);
    try {
      const next = await load(cursor);
      setExtra({ page, items: [...(more?.items ?? []), ...next.items], cursor: next.next_cursor ?? null });
      setEdited(null);
    } catch (err) {
      setMoreError(errorMessage(err));
    } finally {
      setLoadingMore(false);
    }
  };

  return {
    items,
    loading: first.loading && !page,
    error: first.error,
    reload: first.reload,
    hasMore: Boolean(cursor),
    loadMore,
    loadingMore,
    moreError,
    update: (fn) => setEdited({ page, items: fn(items) }),
  };
}
