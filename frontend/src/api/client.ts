//#это единственное место, где frontend знает про транспорт
//#без него fetch и разбор ошибок дублируются в каждом компоненте, и сообщения расходятся между экранами

export type ApiErrorPayload = {
  code: string;
  message: string;
  details: Record<string, unknown>;
};

export class ApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly details: Record<string, unknown>;

  constructor(status: number, payload: ApiErrorPayload) {
    super(payload.message);
    this.name = "ApiError";
    this.status = status;
    this.code = payload.code;
    this.details = payload.details;
  }
}

export class NetworkError extends Error {
  constructor(message = "Backend не отвечает. Проверьте, что он запущен на порту 8510.") {
    super(message);
    this.name = "NetworkError";
  }
}

const API_PREFIX = "/api";

async function parseError(response: Response): Promise<ApiError> {
  //#эта функция аккуратно читает ошибку backend даже тогда, когда сервер вернул не JSON, а обычный текст
  //#без неё браузер показывает техническое "Unexpected token", которое не помогает понять причину
  const text = await response.text();

  if (!text) {
    return new ApiError(response.status, {
      code: "empty_response",
      message: `Backend вернул пустой ответ (${response.status}).`,
      details: {},
    });
  }

  try {
    const payload = JSON.parse(text) as { error?: ApiErrorPayload; detail?: string };

    if (payload.error) {
      return new ApiError(response.status, payload.error);
    }

    return new ApiError(response.status, {
      code: "http_error",
      message: payload.detail ?? text,
      details: {},
    });
  } catch {
    return new ApiError(response.status, { code: "http_error", message: text, details: {} });
  }
}

async function isBackendUnreachable(response: Response): Promise<boolean> {
  //#в разработке запросы идут через прокси Vite, и остановленный backend приходит не сетевым сбоем,
  //#а ответом 5xx с пустым телом. Наш backend в такой ситуации не участвует вовсе: его обработчик
  //#ошибок гарантирует непустое тело на любом ответе, включая 500. Значит пустое тело при 5xx
  //#означает именно «до backend не дошли», и пользователю надо сказать это, а не «пустой ответ (500)»
  if (response.status < 500) {
    return false;
  }

  return (await response.clone().text()).trim() === "";
}

async function request(path: string, init?: RequestInit): Promise<Response> {
  //#эта функция отличает сетевой сбой от ошибки приложения: это разные проблемы с разными действиями
  let response: Response;

  try {
    response = await fetch(`${API_PREFIX}${path}`, init);
  } catch (error) {
    //#отменённый запрос — не сбой сети: показывать «backend не отвечает» здесь было бы ложью
    if (error instanceof DOMException && error.name === "AbortError") {
      throw new RequestCancelledError();
    }

    if (error instanceof TypeError) {
      throw new NetworkError();
    }

    throw error;
  }

  if (!response.ok) {
    if (await isBackendUnreachable(response)) {
      throw new NetworkError();
    }

    throw await parseError(response);
  }

  return response;
}

export class RequestCancelledError extends Error {
  constructor() {
    super("Запрос отменён.");
    this.name = "RequestCancelledError";
  }
}

export async function getJson<T>(path: string, signal?: AbortSignal): Promise<T> {
  //#signal нужен для защиты от устаревших ответов: при быстрой смене сортировки или страницы
  //#предыдущий запрос отменяется, и его результат не может перезаписать более новый
  const response = await request(path, signal ? { signal } : undefined);
  return (await response.json()) as T;
}

export async function getBlob(path: string): Promise<Blob> {
  const response = await request(path);
  return await response.blob();
}

export async function postJson<T>(path: string, body: unknown): Promise<T> {
  const response = await request(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  return (await response.json()) as T;
}

export async function postForm<T>(path: string, formData: FormData): Promise<T> {
  const response = await request(path, { method: "POST", body: formData });
  return (await response.json()) as T;
}

export async function deleteVoid(path: string): Promise<void> {
  //#удаление отвечает 204 без тела: разбор JSON здесь упал бы на пустом ответе.
  //#Прежняя версия функции разбирала тело и не имела ни одного вызова — то есть
  //#сломалась бы при первом же использовании
  await request(path, { method: "DELETE" });
}

export function isCancelled(error: unknown): boolean {
  return error instanceof RequestCancelledError;
}

export function describeError(error: unknown, fallback: string): string {
  //#эта функция превращает любую пойманную ошибку в одну понятную строку для баннера
  if (error instanceof ApiError || error instanceof NetworkError) {
    return error.message;
  }

  if (error instanceof Error) {
    return error.message;
  }

  return fallback;
}
