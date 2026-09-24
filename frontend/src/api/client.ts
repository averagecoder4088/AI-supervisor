// The single place that talks HTTP to the backend.
//
// The base URL comes from the API_BASE_URL environment variable (see .env.example);
// the local default matches `uvicorn app.main:app` from the backend README. All calls
// currently run on the server (Server Components), so the browser never contacts the
// backend directly and no CORS configuration is needed.

import type { ApiErrorBody } from "./types";

export const DEFAULT_API_BASE_URL = "http://127.0.0.1:8000";
const REQUEST_TIMEOUT_MS = 10_000;

export function apiBaseUrl(): string {
  const configured = process.env.API_BASE_URL?.trim();
  return (configured || DEFAULT_API_BASE_URL).replace(/\/+$/, "");
}

/** A failed backend call. `status` is null when the backend could not be reached at all. */
export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number | null,
    readonly code: string | null,
    readonly url: string,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

export async function apiGet<T>(path: string): Promise<T> {
  const url = `${apiBaseUrl()}${path}`;
  let response: Response;
  try {
    response = await fetch(url, {
      cache: "no-store", // always live data
      headers: { accept: "application/json" },
      signal: AbortSignal.timeout(REQUEST_TIMEOUT_MS),
    });
  } catch (err) {
    const reason = err instanceof Error ? err.message : String(err);
    throw new ApiError(
      `Cannot reach the backend at ${apiBaseUrl()} (${reason}). Is it running, and is API_BASE_URL correct?`,
      null,
      "BACKEND_UNREACHABLE",
      url,
    );
  }

  if (!response.ok) {
    let body: Partial<ApiErrorBody> = {};
    try {
      body = (await response.json()) as Partial<ApiErrorBody>;
    } catch {
      // not JSON: fall back to the status line below
    }
    throw new ApiError(
      body.error ?? `Backend answered HTTP ${response.status} ${response.statusText}`.trim(),
      response.status,
      body.code ?? null,
      url,
    );
  }
  return (await response.json()) as T;
}
