import axios, { InternalAxiosRequestConfig } from "axios";

export const apiBaseUrl = String(
  import.meta.env.VITE_API_URL ?? "http://localhost:8000/api/v1",
).replace(/\/$/, "");

export const api = axios.create({
  baseURL: apiBaseUrl,
  withCredentials: true,
  headers: { "Content-Type": "application/json", "X-LocalEC2-Client": "admin" },
});

type RetriableRequest = InternalAxiosRequestConfig & { _localEc2Retried?: boolean };
let refreshRequest: Promise<string> | null = null;

function refreshAccessToken() {
  if (!refreshRequest) {
    refreshRequest = axios.post<{ access_token: string }>(
      `${apiBaseUrl}/auth/refresh`,
      undefined,
      { withCredentials: true, headers: { "X-LocalEC2-Client": "admin" } },
    ).then(response => response.data.access_token).finally(() => { refreshRequest = null; });
  }
  return refreshRequest;
}

api.interceptors.response.use(undefined, async error => {
  if (!axios.isAxiosError(error) || error.response?.status !== 401 || !error.config) {
    return Promise.reject(error);
  }
  const request = error.config as RetriableRequest;
  const path = String(request.url ?? "");
  if (request._localEc2Retried || path.includes("/auth/login") || path.includes("/auth/refresh")) {
    return Promise.reject(error);
  }
  request._localEc2Retried = true;
  const token = await refreshAccessToken();
  api.defaults.headers.common.Authorization = `Bearer ${token}`;
  request.headers.Authorization = `Bearer ${token}`;
  return api(request);
});

export function errorMessage(error: unknown) {
  if (!axios.isAxiosError(error)) return "An unexpected error occurred";
  return String(error.response?.data?.detail ?? error.message);
}
