import { useEffect, useState } from "react";
import { fetchAiStatus } from "../../api/endpoints";
import type { AiStatus } from "../../types/ai";

const MODEL_KEY = "dataarena.ai.model";

//#состояние локальной модели и выбранная пользователем модель. Статус запрашивается
//#один раз на компонент: недоступная модель просто прячет кнопки ИИ, ничего не ломая
export function useAiModel() {
  const [status, setStatus] = useState<AiStatus | null>(null);
  const [model, setModelState] = useState<string | null>(() => {
    try {
      return window.localStorage.getItem(MODEL_KEY);
    } catch {
      return null;
    }
  });

  useEffect(() => {
    let alive = true;
    fetchAiStatus()
      .then((result) => alive && setStatus(result))
      .catch(() => alive && setStatus(null));
    return () => {
      alive = false;
    };
  }, []);

  const available = Boolean(status?.enabled && status.reachable && status.models.length > 0);
  //#выбранная модель должна быть среди установленных: иначе берётся модель из настроек или первая
  const effective =
    model && status?.models.includes(model) ? model : (status?.model ?? status?.models[0] ?? null);

  const setModel = (value: string) => {
    setModelState(value);
    try {
      window.localStorage.setItem(MODEL_KEY, value);
    } catch {
      //#выбор не переживёт перезагрузку
    }
  };

  return { status, available, model: effective, setModel };
}
