import { useCallback, useEffect, useState } from "react";
import {
  Braces,
  ChevronDown,
  Database,
  FlaskConical,
  Layers,
  Moon,
  Scale,
  Stethoscope,
  Sun,
  Table2,
  Waypoints,
  Wand2,
} from "lucide-react";
import type { LucideIcon } from "lucide-react";
import { describeError } from "../api/client";
import { fetchHealth } from "../api/endpoints";
import { ErrorBoundary } from "../components/ErrorBoundary";
import { DatasetTable } from "../features/dataset-table/DatasetTable";
import { DatasetHealth } from "../features/health/DatasetHealth";
import { DatasetLineage } from "../features/lineage/DatasetLineage";
import { DatasetBuilder } from "../features/builder/DatasetBuilder";
import { DecisionLab } from "../features/decision/DecisionLab";
import { PipelineEditor } from "../features/pipeline/PipelineEditor";
import { SqlWorkspace } from "../features/sql/SqlWorkspace";
import { WorkspacePanel } from "../features/workspace/WorkspacePanel";
import { useWorkspaceId } from "../hooks/useWorkspaceId";
import type { Dataset } from "../types/dataset";
import type { Finding } from "../types/health";
import type { HealthStatus } from "../types/system";

//#навигация повторяет реальный порядок работы: открыть → посмотреть → спросить → проверить → собрать → повторить
//#разделы, за которыми ещё нет backend, помечены как недоступные, а не показывают пустые экраны
type Section = {
  id: string;
  label: string;
  icon: LucideIcon;
  subtitle: string;
  available: boolean;
};

const SECTIONS: Section[] = [
  {
    id: "workspace",
    label: "Файлы",
    icon: Database,
    subtitle: "Несколько датасетов разных форматов в одном рабочем пространстве.",
    available: true,
  },
  {
    id: "dataset",
    label: "Данные",
    icon: Table2,
    subtitle: "Исходный файл как есть: сортировка, фильтры, поиск. Результат шагов — в разделе «Конвейер».",
    available: true,
  },
  {
    id: "pipeline",
    label: "Конвейер",
    icon: Wand2,
    subtitle: "Шаги преобразования: исходный файл не меняется, результат строится заново.",
    available: true,
  },
  {
    id: "health",
    label: "Качество",
    icon: Stethoscope,
    subtitle: "Профиль и находки: у каждой — конкретные строки, а не только счётчик.",
    available: true,
  },
  {
    id: "builder",
    label: "Сборка",
    icon: Layers,
    subtitle: "Сборка нового датасета из нескольких источников: анализ ключей и прогноз размера.",
    available: true,
  },
  {
    id: "lineage",
    label: "Происхождение",
    icon: Waypoints,
    subtitle: "Откуда взялся датасет и что стало с каждой колонкой.",
    available: true,
  },
  {
    id: "decision",
    label: "Выбор модели",
    icon: Scale,
    subtitle: "Выбор модели по нескольким противоречивым критериям: TOPSIS, ограничения, Парето и устойчивость.",
    available: true,
  },
  {
    id: "recipes",
    label: "Рецепты",
    icon: FlaskConical,
    subtitle: "Рецепт сборки, применимый к новым версиям исходных файлов.",
    available: false,
  },
];

export function App() {
  //#этот компонент отвечает только за оболочку: навигацию, состояние backend и подключение разделов
  const [health, setHealth] = useState<HealthStatus | null>(null);
  const [healthError, setHealthError] = useState<string | null>(null);
  const [activeSection, setActiveSection] = useState<string>("workspace");
  const [selectedDataset, setSelectedDataset] = useState<Dataset | null>(null);
  //#находка, строки которой сейчас показаны в таблице. Живёт в оболочке, а не в таблице:
  //#из-за этого возврат в диагностику не теряет контекст, а повторный вход в таблицу
  //#по другой находке не тащит за собой прежний отбор
  const [openFinding, setOpenFinding] = useState<Finding | null>(null);
  const { workspaceId, error: workspaceError, retry: retryWorkspace } = useWorkspaceId();

  useEffect(() => {
    //#новое рабочее пространство — другие датасеты: выбранный прежде к нему не относится
    setSelectedDataset(null);
    setOpenFinding(null);
  }, [workspaceId]);

  const loadHealth = useCallback(async () => {
    try {
      setHealth(await fetchHealth());
      setHealthError(null);
    } catch (error) {
      setHealth(null);
      setHealthError(describeError(error, "Не удалось связаться с backend."));
    }
  }, []);

  useEffect(() => {
    void loadHealth();
  }, [loadHealth]);

  const section = SECTIONS.find((item) => item.id === activeSection) ?? SECTIONS[0]!;

  const { theme, toggleTheme } = useTheme();

  return (
    <div className="app-shell">
      {/*#навигация — «островок» наверху по центру, как у приложения на телефоне:
         #содержимому отдаётся вся ширина экрана без боковой панели и рамок*/}
      <header className="island">
        <span className="island__brand" title={health ? `DataArena ${health.version}` : "DataArena"}>
          DataArena
        </span>

        <nav className="island__nav" aria-label="Разделы">
          {SECTIONS.map((item) => {
            const Icon = item.icon;
            return (
              <button
                key={item.id}
                type="button"
                className="app-nav__item"
                aria-current={item.id === section.id ? "page" : undefined}
                //#раздел без реализации нельзя открыть: пустой экран вместо содержимого
                //#выглядит как поломка, а не как «этого ещё нет»
                disabled={!item.available}
                title={item.available ? item.subtitle : "Появится на следующих этапах"}
                onClick={() => setActiveSection(item.id)}
              >
                <Icon size={16} aria-hidden />
                <span className="app-nav__label">{item.label}</span>
              </button>
            );
          })}
        </nav>

        <BackendStatus health={health} error={healthError} />
        <button
          type="button"
          className="island__theme"
          onClick={toggleTheme}
          aria-label={theme === "dark" ? "Светлая тема" : "Тёмная тема"}
          title={theme === "dark" ? "Светлая тема" : "Тёмная тема"}
        >
          {theme === "dark" ? <Sun size={16} aria-hidden /> : <Moon size={16} aria-hidden />}
        </button>
      </header>

      <main className="app-main">
        <div className="app-content">
          <ErrorBoundary section={section.id}>
            {healthError ? (
              <div className="banner banner--error" role="alert">
                <div>
                  <p className="banner__title">Backend недоступен</p>
                  <p className="banner__text">{healthError}</p>
                  <button
                    type="button"
                    className="app-nav__item"
                    onClick={() => {
                      void loadHealth();
                      //#рабочее пространство, не открывшееся без backend, открывается вместе с ним
                      if (workspaceError) {
                        void retryWorkspace();
                      }
                    }}
                  >
                    Проверить снова
                  </button>
                </div>
              </div>
            ) : (
              <SectionContent
                section={section.id}
                health={health}
                workspaceId={workspaceId}
                workspaceError={workspaceError}
                onRetryWorkspace={() => void retryWorkspace()}
                selectedDataset={selectedDataset}
                openFinding={openFinding}
                onSelectDataset={(dataset) => {
                  setSelectedDataset(dataset);
                  setOpenFinding(null);
                  setActiveSection("dataset");
                }}
                onShowFindingRows={(finding) => {
                  setOpenFinding(finding);
                  setActiveSection("dataset");
                }}
                onBackToHealth={() => setActiveSection("health")}
                onClearFinding={() => setOpenFinding(null)}
                onDatasetDeleted={(datasetId) => {
                  //#удалённый датасет не может оставаться открытым в других разделах
                  if (selectedDataset?.dataset_id === datasetId) {
                    setSelectedDataset(null);
                    setOpenFinding(null);
                  }
                }}
              />
            )}
          </ErrorBoundary>
        </div>
      </main>
    </div>
  );
}

function BackendStatus({ health, error }: { health: HealthStatus | null; error: string | null }) {
  //#состояние сервера — точка без текста; подробности в подсказке и в баннере ошибки
  const state = error ? "down" : health ? "ok" : "pending";
  const label = error
    ? "Backend недоступен"
    : health
      ? `Backend работает, контракт ${health.contract.format}/${health.contract.version}`
      : "Проверка backend…";
  return <span className={`island__status island__status--${state}`} role="status" aria-label={label} title={label} />;
}

const THEME_KEY = "dataarena.theme";

function useTheme() {
  const [theme, setTheme] = useState<"dark" | "light">(() => {
    try {
      const stored = window.localStorage.getItem(THEME_KEY);
      if (stored === "dark" || stored === "light") return stored;
    } catch {
      //#хранилище недоступно: тема по умолчанию
    }
    return window.matchMedia?.("(prefers-color-scheme: light)").matches ? "light" : "dark";
  });

  useEffect(() => {
    document.documentElement.dataset.theme = theme;
    try {
      window.localStorage.setItem(THEME_KEY, theme);
    } catch {
      //#выбор просто не переживёт перезагрузку
    }
  }, [theme]);

  return { theme, toggleTheme: () => setTheme((previous) => (previous === "dark" ? "light" : "dark")) };
}

function SectionContent({
  section,
  health,
  workspaceId,
  workspaceError,
  onRetryWorkspace,
  selectedDataset,
  openFinding,
  onSelectDataset,
  onShowFindingRows,
  onBackToHealth,
  onClearFinding,
  onDatasetDeleted,
}: {
  section: string;
  health: HealthStatus | null;
  workspaceId: string | null;
  workspaceError: string | null;
  onRetryWorkspace: () => void;
  selectedDataset: Dataset | null;
  openFinding: Finding | null;
  onSelectDataset: (dataset: Dataset) => void;
  onShowFindingRows: (finding: Finding) => void;
  onBackToHealth: () => void;
  onClearFinding: () => void;
  onDatasetDeleted: (datasetId: string) => void;
}) {
  if (section === "workspace") {
    return (
      <WorkspacePanel
        workspaceId={workspaceId}
        workspaceError={workspaceError}
        onRetryWorkspace={onRetryWorkspace}
        onSelectDataset={onSelectDataset}
        selectedDatasetId={selectedDataset?.dataset_id ?? null}
        onDatasetDeleted={onDatasetDeleted}
      />
    );
  }

  if (section === "dataset") {
    if (!selectedDataset) {
      return (
        <div className="empty-state">
          <h2 className="empty-state__title">Датасет не выбран</h2>
          <p className="empty-state__text">
            Откройте раздел «Файлы» и выберите датасет, чтобы посмотреть его содержимое.
          </p>
        </div>
      );
    }

    return (
      <>
        <DatasetTable
          dataset={selectedDataset}
          finding={openFinding}
          onBackToHealth={onBackToHealth}
          onClearFinding={onClearFinding}
          //#диагностика проверяет результат конвейера, поэтому строки находки берутся
          //#оттуда же: иначе находка говорила бы об одном, а таблица показывала другое
          source={openFinding ? "pipeline" : "dataset"}
        />
        <SqlDrawer workspaceId={workspaceId} />
      </>
    );
  }

  if (section === "pipeline") {
    if (!selectedDataset) {
      return (
        <div className="empty-state">
          <h2 className="empty-state__title">Датасет не выбран</h2>
          <p className="empty-state__text">
            Откройте раздел «Файлы» и выберите датасет, чтобы собрать для него конвейер
            преобразований.
          </p>
        </div>
      );
    }

    return <PipelineEditor dataset={selectedDataset} />;
  }

  if (section === "decision") {
    //#Decision Lab не зависит от рабочего пространства: матрица приходит из ModelArena или файла
    return <DecisionLab />;
  }

  if (section === "builder") {
    return (
      <DatasetBuilder
        workspaceId={workspaceId}
        onDatasetCreated={onSelectDataset}
      />
    );
  }

  if (section === "lineage") {
    if (!selectedDataset) {
      return (
        <div className="empty-state">
          <h2 className="empty-state__title">Датасет не выбран</h2>
          <p className="empty-state__text">
            Откройте раздел «Файлы» и выберите датасет, чтобы посмотреть его происхождение.
          </p>
        </div>
      );
    }

    return <DatasetLineage dataset={selectedDataset} />;
  }

  if (section === "health") {
    if (!selectedDataset) {
      return (
        <div className="empty-state">
          <h2 className="empty-state__title">Датасет не выбран</h2>
          <p className="empty-state__text">
            Откройте раздел «Файлы» и выберите датасет, чтобы посмотреть его диагностику.
          </p>
        </div>
      );
    }

    return <DatasetHealth dataset={selectedDataset} onShowRows={onShowFindingRows} />;
  }

  return <StagePlaceholder health={health} />;
}


function SqlDrawer({ workspaceId }: { workspaceId: string | null }) {
  //#SQL — раскрывающаяся панель прямо под таблицей: запрос к данным не требует
  //#ухода в другой раздел, а закрытая панель не занимает места
  const [open, setOpen] = useState(false);
  return (
    <section className={`sql-drawer${open ? " sql-drawer--open" : ""}`}>
      <button type="button" className="sql-drawer__toggle" aria-expanded={open} onClick={() => setOpen((value) => !value)}>
        <Braces size={15} aria-hidden />
        SQL-запрос
        <ChevronDown size={15} aria-hidden className="sql-drawer__chevron" />
      </button>
      {open && (
        <div className="sql-drawer__body">
          <SqlWorkspace workspaceId={workspaceId} />
        </div>
      )}
    </section>
  );
}

function StagePlaceholder({ health }: { health: HealthStatus | null }) {
  //#этот экран честно говорит, что разделов ещё нет, вместо того чтобы показывать кнопки без действия
  return (
    <div className="empty-state">
      <h2 className="empty-state__title">Раздел ещё не реализован</h2>
      <p className="empty-state__text">
        Этот раздел появится на следующих этапах: в навигации он недоступен, чтобы
        в интерфейсе не было кнопок, за которыми ничего нет.
      </p>

      {health && (
        <div className="stage-note">
          <dl>
            <dt>Контракт</dt>
            <dd>
              {health.contract.format} {health.contract.version}
            </dd>
            <dt>Отпечаток</dt>
            <dd>{health.contract.fingerprint_algorithm}</dd>
            <dt>Workspace</dt>
            <dd>{health.workspace_root}</dd>
            <dt>Лимит загрузки</dt>
            <dd>{health.max_upload_mb} МБ</dd>
            <dt>ModelArena</dt>
            <dd>{health.modelarena_configured ? "настроена" : "не настроена — это не мешает работе"}</dd>
          </dl>
        </div>
      )}
    </div>
  );
}
