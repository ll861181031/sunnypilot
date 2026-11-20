import { http } from '@kit.NetworkKit';
import { BusinessError } from '@kit.BasicServicesKit';

/**
 * One request's outcome. Requests never throw, so a view can always render
 * *why* it failed instead of showing an empty screen.
 */
export class ApiResult {
  ok: boolean = false;
  code: number = 0;
  body: string = '';
  error: string = '';

  constructor(ok: boolean, code: number, body: string, error: string) {
    this.ok = ok;
    this.code = code;
    this.body = body;
    this.error = error;
  }
}

/**
 * Minimal JSON-over-HTTP client. Deliberately not a generic REST layer: the two
 * services are plain BaseHTTPRequestHandler servers with no auth beyond an
 * optional shared-secret header, so there is nothing else to abstract.
 */
export class ApiClient {
  static async get(url: string, headers: Record<string, string> | undefined,
                   timeoutMs: number): Promise<ApiResult> {
    return ApiClient.send(url, http.RequestMethod.GET, headers, timeoutMs);
  }

  /**
   * POST with no body: every write endpoint on both services takes its arguments
   * in the query string (`/api/rolling?on=1`, `/api/source?mode=...`), which is
   * what their own web pages do.
   */
  static async post(url: string, headers: Record<string, string> | undefined,
                    timeoutMs: number): Promise<ApiResult> {
    return ApiClient.send(url, http.RequestMethod.POST, headers, timeoutMs);
  }

  /** Convenience for the common "call an action endpoint and expect {"ok":true}" case. */
  static async action(url: string, headers: Record<string, string> | undefined): Promise<ApiResult> {
    // Starting the stack shells out to the launcher and waits, so it needs a far
    // longer budget than a status poll.
    return ApiClient.post(url, headers, 30000);
  }

  /**
   * POST with a JSON body.
   *
   * The settings endpoint is the one write that takes a body rather than a query
   * string, because the value it carries is of mixed type: a boolean, an integer, or
   * null to unset the key. The caller supplies its own headers so the content type
   * and the console token are set in one place, at the call site.
   */
  static async postJson(url: string, headers: Record<string, string> | undefined,
                        body: string, timeoutMs: number): Promise<ApiResult> {
    return ApiClient.send(url, http.RequestMethod.POST, headers, timeoutMs, body);
  }

  private static async send(url: string, method: http.RequestMethod,
                            headers: Record<string, string> | undefined,
                            timeoutMs: number, body?: string): Promise<ApiResult> {
    const request = http.createHttp();
    try {
      const options: http.HttpRequestOptions = {
        method: method,
        connectTimeout: timeoutMs,
        readTimeout: timeoutMs,
        expectDataType: http.HttpDataType.STRING,
        header: headers
      };
      if (body !== undefined) {
        // `extraData` is the wire body; `body`/`queryParams` are not request fields.
        options.extraData = body;
      }
      const response = await request.request(url, options);
      const body = ApiClient.bodyToString(response.result);
      const ok = response.responseCode >= 200 && response.responseCode < 300;
      return new ApiResult(ok, response.responseCode, body,
        ok ? '' : `HTTP ${response.responseCode}`);
    } catch (err) {
      const be = err as BusinessError;
      // Coerced rather than compared: `message` is typed string, so `=== undefined`
      // would be a no-overlap comparison, yet a thrown non-Error really has none.
      const text: string = `${be.message}`;
      const message: string = text.length > 0 && text !== 'undefined' ? text : 'request failed';
      return new ApiResult(false, 0, '', message);
    } finally {
      // Every request owns its own HttpRequest; leaking them leaks sockets.
      request.destroy();
    }
  }

  private static bodyToString(result: string | Object | ArrayBuffer): string {
    if (typeof result === 'string') {
      return result;
    }
    if (result instanceof ArrayBuffer) {
      // The services only ever answer JSON, so a binary body is not a case worth
      // decoding; callers treat an empty body as "unparseable".
      return '';
    }
    return JSON.stringify(result);
  }
}
