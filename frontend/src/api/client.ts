// The single place that talks HTTP to the backend.
//
// The base URL comes from the API_BASE_URL environment variable (see .env.example);
// the local default matches `uvicorn app.main:app` from the backend README. Every call runs on
// the Next.js server (Server Components and Server Actions), so the browser never contacts the
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

async function request<T>(method: "GET" | "POST", path: string, body?: unknown): Promise<T> {
  const url = `${apiBaseUrl()}${path}`;
  let response: Response;
  try {
    response = await fetch(url, {
      method,
      cache: "no-store", // always live data
      headers: {
        accept: "application/json",
        ...(body !== undefined ? { "content-type": "application/json" } : {}),
      },
      body: body !== undefined ? JSON.stringify(body) : undefined,
      signal: AbortSignal.timeout(REQUEST_TIMEOUT_MS),
    });
  } catch (err) {
    if (err instanceof Error && err.name === "TimeoutError") {
      throw new ApiError(
        `The backend did not answer within ${REQUEST_TIMEOUT_MS / 1000} seconds.`,
        null,
        "BACKEND_TIMEOUT",
        url,
      );
    }
    const reason = err instanceof Error ? err.message : String(err);
    throw new ApiError(
      `Cannot reach the backend at ${apiBaseUrl()} (${reason}). Is it running, and is API_BASE_URL correct?`,
      null,
      "BACKEND_UNREACHABLE",
      url,
    );
  }

  if (!response.ok) {
    let errorBody: Partial<ApiErrorBody> = {};
    try {
      errorBody = (await response.json()) as Partial<ApiErrorBody>;
    } catch {
      // not JSON: fall back to the status line below
    }
    throw new ApiError(
      errorBody.error ?? `Backend answered HTTP ${response.status} ${response.statusText}`.trim(),
      response.status,
      errorBody.code ?? null,
      url,
    );
  }
  try {
    return (await response.json()) as T;
  } catch {
    throw new ApiError("The backend answered successfully but the response was not valid JSON.", response.status, "MALFORMED_RESPONSE", url);
  }
}

export function apiGet<T>(path: string): Promise<T> {
  return request<T>("GET", path);
}

/** POST a JSON body. Only ever called from Server Actions (never from the browser). */
export function apiPost<T>(path: string, body: unknown): Promise<T> {
  return request<T>("POST", path, body);
}
