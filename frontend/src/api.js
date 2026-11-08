// Wrapper around fetch() that parses JSON responses and formats FastAPI error details.
export async function apiRequest(path, options = {}) {
  let response;
  try {
    response = await fetch(path, options);
  } catch (error) {
    if (options.signal?.aborted) throw error;
    throw new Error("Could not reach the backend. Check that it is running on port 8000, then retry.");
  }
  const text = await response.text();
  let payload;
  try {
    payload = JSON.parse(text);
  } catch {
    throw new Error(`The backend returned an unreadable response (HTTP ${response.status}). Check that it is running on port 8000.`);
  }
  if (!response.ok) {
    const detail = payload.detail;
    const message = Array.isArray(detail)
      ? detail.map((item) => `${(item.loc || []).slice(1).join(".")}: ${item.msg}`).join("; ")
      : typeof detail === "string" ? detail : `Request failed (HTTP ${response.status})`;
    throw new Error(message);
  }
  return payload;
}
