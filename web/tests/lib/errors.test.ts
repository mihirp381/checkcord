import { AxiosError, type InternalAxiosRequestConfig } from "axios";
import { describe, expect, it } from "vitest";
import { errorMessage } from "../../src/lib/errors";

const config = { headers: {} } as InternalAxiosRequestConfig;
const failed = (status: number, detail?: unknown) =>
  new AxiosError("failed", "ERR_BAD_RESPONSE", config, null, {
    status,
    statusText: "",
    data: detail === undefined ? {} : { detail },
    headers: {},
    config,
  });

describe("errorMessage", () => {
  it("explains an unreachable API", () => {
    expect(errorMessage(new AxiosError("Network Error", "ERR_NETWORK", config))).toMatch(/Cannot reach the Checkcord API/);
    expect(errorMessage(new AxiosError("timeout", "ECONNABORTED", config))).toMatch(/too long/);
  });

  it("maps sign-in and rights problems", () => {
    expect(errorMessage(failed(401, "Unauthorized: invalid API key"))).toBe("Your sign-in has ended. Sign in again.");
    expect(errorMessage(failed(403, "Forbidden: admin rights required"))).toBe("This needs admin rights.");
    expect(errorMessage(failed(403, "Forbidden: missing or invalid CSRF token"))).toMatch(/Reload the page/);
  });

  it("shows the API's own detail and validation messages", () => {
    expect(errorMessage(failed(404, "no such live record"))).toBe("No such live record");
    expect(errorMessage(failed(404))).toBe("Not found.");
    expect(errorMessage(failed(422, [{ loc: ["body", "name"], msg: "Field required" }]))).toBe("name: Field required");
    expect(errorMessage(failed(400, "PUBLIC_BASE_URL is not set"))).toBe("PUBLIC_BASE_URL is not set");
  });

  it("names a server failure without detail", () => {
    expect(errorMessage(failed(502))).toBe("The Checkcord API failed (status 502). Check its logs.");
    expect(errorMessage(failed(429))).toMatch(/Too many requests/);
  });

  it("says nothing for a canceled request", () => {
    expect(errorMessage(new AxiosError("canceled", "ERR_CANCELED", config))).toBe("");
  });
});
