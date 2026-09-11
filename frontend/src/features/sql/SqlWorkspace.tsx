import { useCallback, useMemo, useState } from "react";
import { Ban, Bookmark, Clock, Database, Play, Trash2 } from "lucide-react";
import { formatCount, formatDuration } from "../../utils/format";
import type { DatasetBinding, QueryRun } from "../../types/sql";
import { MAX_SQL_LENGTH, useSqlWorkspace } from "./useSqlWorkspace";

const STATUS_LABEL: Record<QueryRun["status"], string> = {
  succeeded: "выполнен",
  failed: "ошибка",
  timed_out: "время вышло",
  cancelled: "отменён",
};

export function SqlWorkspace({ workspaceId }: { workspaceId: string | null }) {
  const [text, setText] = useState("");
  const { state, execute, cancel, save, remove, dismissError } = useSqlWorkspace(workspaceId);

  const tooLong = text.length > MAX_SQL_LENGTH;
  const canRun = Boolean(workspaceId) && text.trim().length > 0 && !state.running && !tooLong;

  const run = useCallback(() => {
    void execute(text);
  }, [execute, text]);

  const onKeyDown = useCallback(
    (event: React.KeyboardEvent<HTMLTextAreaElement>) => {
      //#Ctrl+Enter — то, что нажимают не глядя во всех редакторах запросов
      if ((event.ctrlKey || event.metaKey) && event.key === "Enter" && canRun) {
        event.preventDefault();
        run();
      }
    },
    [canRun, run],
  );

  if (!workspaceId) {
    return (
      <div className="empty-state">
        <h2 className="empty-state__title">Рабочее пространство не выбрано</h2>
        <p className="empty-state__text">
          Откройте раздел Workspace: запросы выполняются к датасетам одного рабочего пространства.
        </p>
      </div>
    );
  }

  return (
    <div className="sql-layout">
      <section className="sql-main">
        <div className="sql-editor">
          <textarea
            className="sql-editor__input"
            value={text}
            spellCheck={false}
            placeholder="SELECT город, count(*) FROM продажи GROUP BY город"
            aria-label="Текст SQL-запроса"
            onChange={(event) => setText(event.target.value)}
            onKeyDown={onKeyDown}
          />

          <div className="sql-editor__actions">
            <button type="button" className="button button--primary" disabled={!canRun} onClick={run}>
              <Play size={14} aria-hidden />
              Выполнить
            </button>

            {/*#кнопка отмены показывается только когда отменять действительно есть что
               #и есть чем: метка запроса известна, и backend умеет его прервать*/}
            {state.cancellable && (
              <button type="button" className="button" onClick={() => void cancel()}>
                <Ban size={14} aria-hidden />
                Отменить
              </button>
            )}

            <SaveControl text={text} disabled={!text.trim() || state.running} onSave={save} />

            <span className="sql-editor__hint">
              {tooLong
                ? `Запрос длиннее ${formatCount(MAX_SQL_LENGTH)} символов`
                : "Ctrl+Enter — выполнить"}
            </span>
          </div>
        </div>

        {state.error && (
          <div className="banner banner--error" role="alert">
            <div>
              <p className="banner__title">Запрос не выполнен</p>
              <p className="banner__text">{state.error}</p>
            </div>
            <button type="button" className="button" onClick={dismissError}>
              Понятно
            </button>
          </div>
        )}

        {state.running && (
          <p className="sql-status" role="status">
            Запрос выполняется…
          </p>
        )}

        {state.response && !state.running && <ResultView response={state.response} />}
      </section>

      <aside className="sql-side">
        <BindingList bindings={state.bindings} onPick={(alias) => setText((current) => current + alias)} />
        <SavedList queries={state.saved} onOpen={setText} onDelete={remove} />
        <HistoryList runs={state.history} onOpen={setText} />
      </aside>
    </div>
  );
}

function ResultView({ response }: { response: NonNullable<ReturnType<typeof useSqlWorkspace>["state"]["response"]> }) {
  const { result, run } = response;

  return (
    <div className="sql-result">
      <div className="sql-result__meta">
        <span>{formatCount(result.row_count)} строк</span>
        <span>{formatDuration(result.elapsed_ms)}</span>
        {run.datasets.length > 0 && <span>датасеты: {run.datasets.join(", ")}</span>}
      </div>

      {/*#усечение обозначается всегда: срез, выданный за полный результат, — неверный ответ*/}
      {result.truncated && (
        <p className="sql-result__truncated" role="status">
          {result.truncated_by === "bytes"
            ? "Показана часть результата: ответ превысил предел по объёму."
            : `Показаны первые ${formatCount(result.row_count)} строк: их было больше.`}
        </p>
      )}

      {result.columns.length === 0 ? (
        <p className="sql-status">Запрос не вернул колонок.</p>
      ) : (
        <div className="sql-result__scroll">
          <table className="data-table">
            <thead>
              <tr>
                {result.columns.map((column) => (
                  <th key={column} scope="col">
                    {column}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {/*#ключом служит номер строки: у результата запроса нет собственного
                 #идентификатора, а строки заменяются целиком и никогда не переупорядочиваются*/}
              {result.rows.map((row, index) => (
                <tr key={index}>
                  {result.columns.map((column) => (
                    <td key={column}>{renderCell(row[column])}</td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>

          {result.rows.length === 0 && <p className="sql-status">Ни одной строки не найдено.</p>}
        </div>
      )}
    </div>
  );
}

function renderCell(value: unknown): string {
  //#пустое значение и пустая строка выглядят одинаково, если не показать разницу явно
  if (value === null || value === undefined) {
    return "—";
  }

  if (typeof value === "object") {
    return JSON.stringify(value);
  }

  return String(value);
}

function SaveControl({
  text,
  disabled,
  onSave,
}: {
  text: string;
  disabled: boolean;
  onSave: (name: string, sql: string) => Promise<boolean>;
}) {
  const [open, setOpen] = useState(false);
  const [name, setName] = useState("");

  if (!open) {
    return (
      <button type="button" className="button" disabled={disabled} onClick={() => setOpen(true)}>
        <Bookmark size={14} aria-hidden />
        Сохранить
      </button>
    );
  }

  return (
    <form
      className="sql-save"
      onSubmit={(event) => {
        event.preventDefault();

        void onSave(name, text).then((saved) => {
          if (saved) {
            setOpen(false);
            setName("");
          }
        });
      }}
    >
      <input
        className="sql-save__input"
        value={name}
        autoFocus
        maxLength={120}
        placeholder="Имя запроса"
        aria-label="Имя сохранённого запроса"
        onChange={(event) => setName(event.target.value)}
      />
      <button type="submit" className="button button--primary" disabled={!name.trim()}>
        Сохранить
      </button>
      <button type="button" className="button" onClick={() => setOpen(false)}>
        Отмена
      </button>
    </form>
  );
}

function BindingList({
  bindings,
  onPick,
}: {
  bindings: DatasetBinding[];
  onPick: (alias: string) => void;
}) {
  return (
    <section className="sql-panel">
      <h2 className="sql-panel__title">
        <Database size={14} aria-hidden />
        Доступно в FROM
      </h2>

      {bindings.length === 0 ? (
        <p className="sql-panel__empty">В рабочем пространстве пока нет датасетов.</p>
      ) : (
        <ul className="sql-panel__list">
          {bindings.map((binding) => (
            <li key={binding.dataset_id}>
              <button
                type="button"
                className="sql-chip"
                disabled={!binding.queryable}
                title={
                  binding.queryable
                    ? `${binding.name}: ${binding.column_count} колонок`
                    : "Датасет недоступен для чтения"
                }
                onClick={() => onPick(binding.alias)}
              >
                <span className="sql-chip__name">{binding.alias}</span>
                <span className="sql-chip__meta">
                  {binding.row_count === null ? "—" : formatCount(binding.row_count)} ×{" "}
                  {binding.column_count}
                </span>
              </button>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

function SavedList({
  queries,
  onOpen,
  onDelete,
}: {
  queries: { saved_query_id: string; name: string; sql: string }[];
  onOpen: (sql: string) => void;
  onDelete: (savedQueryId: string) => Promise<void>;
}) {
  return (
    <section className="sql-panel">
      <h2 className="sql-panel__title">
        <Bookmark size={14} aria-hidden />
        Сохранённые
      </h2>

      {queries.length === 0 ? (
        <p className="sql-panel__empty">Ни одного сохранённого запроса.</p>
      ) : (
        <ul className="sql-panel__list">
          {queries.map((query) => (
            <li key={query.saved_query_id} className="sql-panel__row">
              <button type="button" className="sql-link" onClick={() => onOpen(query.sql)}>
                {query.name}
              </button>
              <button
                type="button"
                className="sql-icon-button"
                aria-label={`Удалить запрос «${query.name}»`}
                onClick={() => void onDelete(query.saved_query_id)}
              >
                <Trash2 size={13} aria-hidden />
              </button>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

function HistoryList({ runs, onOpen }: { runs: QueryRun[]; onOpen: (sql: string) => void }) {
  const items = useMemo(() => runs.slice(0, 25), [runs]);

  return (
    <section className="sql-panel">
      <h2 className="sql-panel__title">
        <Clock size={14} aria-hidden />
        История
      </h2>

      {items.length === 0 ? (
        <p className="sql-panel__empty">Запросов ещё не было.</p>
      ) : (
        <ul className="sql-panel__list">
          {items.map((run) => (
            <li key={run.run_id}>
              <button type="button" className="sql-history" onClick={() => onOpen(run.sql)}>
                <span className="sql-history__sql">{run.sql}</span>
                <span className={`sql-history__status sql-history__status--${run.status}`}>
                  {STATUS_LABEL[run.status]}
                  {run.status === "succeeded" && run.row_count !== null
                    ? ` · ${formatCount(run.row_count)} строк · ${formatDuration(run.elapsed_ms)}`
                    : ""}
                </span>
              </button>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
