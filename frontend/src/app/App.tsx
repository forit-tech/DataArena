import { useCallback, useEffect, useState } from "react";
import {
  Braces,
  Database,
  FlaskConical,
  Layers,
  Stethoscope,
  Table2,
} from "lucide-react";
import type { LucideIcon } from "lucide-react";
import { describeError } from "../api/client";
import { fetchHealth } from "../api/endpoints";
import { ErrorBoundary } from "../components/ErrorBoundary";
import { DatasetTable } from "../features/dataset-table/DatasetTable";
import { DatasetHealth } from "../features/health/DatasetHealth";
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
    label: "Workspace",
    icon: Database,
    subtitle: "Несколько датасетов разных форматов в одном рабочем пространстве.",
    available: true,
  },
  {
    id: "dataset",
    label: "Dataset",
    icon: Table2,
    subtitle: "Таблица на миллион строк: сортировка, фильтры, поиск.",
    available: true,
  },
  {
    id: "sql",
    label: "SQL",
    icon: Braces,
    subtitle: "Запросы к открытым датасетам, история и сохранённые запросы.",
    available: true,
  },
  {
    id: "health",
    label: "Health",
    icon: Stethoscope,
    subtitle: "Профиль и находки: у каждой — конкретные строки, а не только счётчик.",
    available: true,
  },
  {
    id: "builder",
    label: "Builder",
    icon: Layers,
    subtitle: "Сборка нового датасета из нескольких источников через join.",
    available: false,
  },
  {
    id: "recipes",
    label: "Recipes",
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
  const { workspaceId } = useWorkspaceId();

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

  return (
    <div className="app-shell">
      <aside className="app-sidebar">
        <div className="app-brand">
          <span className="app-brand__name">DataArena</span>
          <span className="app-brand__version">{health ? `v${health.version}` : "—"}</span>
        </div>

        <nav className="app-nav" aria-label="Разделы">
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
                title={item.available ? undefined : "Появится на следующих этапах"}
                onClick={() => setActiveSection(item.id)}
              >
                <Icon size={15} aria-hidden />
                {item.label}
              </button>
            );
          })}
        </nav>
      </aside>

      <main className="app-main">
        <header className="app-header">
          <div>
            <h1 className="app-header__title">{section.label}</h1>
            <p className="app-header__subtitle">{section.subtitle}</p>
          </div>
          <BackendStatus health={health} error={healthError} />
        </header>

        <div className="app-content">
          <ErrorBoundary section={section.id}>
            {healthError ? (
              <div className="banner banner--error" role="alert">
                <div>
                  <p className="banner__title">Backend недоступен</p>
                  <p className="banner__text">{healthError}</p>
                  <button type="button" className="app-nav__item" onClick={() => void loadHealth()}>
                    Проверить снова
                  </button>
                </div>
              </div>
            ) : (
              <SectionContent
                section={section.id}
                health={health}
                workspaceId={workspaceId}
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
              />
            )}
          </ErrorBoundary>
        </div>
      </main>
    </div>
  );
}

function BackendStatus({ health, error }: { health: HealthStatus | null; error: string | null }) {
  if (error) {
    return (
      <span className="status-pill status-pill--down">
        <span className="status-pill__dot" />
        backend недоступен
      </span>
    );
  }

  if (!health) {
    return (
      <span className="status-pill">
        <span className="status-pill__dot" />
        проверка…
      </span>
    );
  }

  return (
    <span className="status-pill status-pill--ok">
      <span className="status-pill__dot" />
      {health.contract.format}/{health.contract.version}
    </span>
  );
}

function SectionContent({
  section,
  health,
  workspaceId,
  selectedDataset,
  openFinding,
  onSelectDataset,
  onShowFindingRows,
  onBackToHealth,
  onClearFinding,
}: {
  section: string;
  health: HealthStatus | null;
  workspaceId: string | null;
  selectedDataset: Dataset | null;
  openFinding: Finding | null;
  onSelectDataset: (dataset: Dataset) => void;
  onShowFindingRows: (finding: Finding) => void;
  onBackToHealth: () => void;
  onClearFinding: () => void;
}) {
  if (section === "workspace") {
    return (
      <WorkspacePanel
        workspaceId={workspaceId}
        onSelectDataset={onSelectDataset}
        selectedDatasetId={selectedDataset?.dataset_id ?? null}
      />
    );
  }

  if (section === "sql") {
    return <SqlWorkspace workspaceId={workspaceId} />;
  }

  if (section === "dataset") {
    if (!selectedDataset) {
      return (
        <div className="empty-state">
          <h2 className="empty-state__title">Датасет не выбран</h2>
          <p className="empty-state__text">
            Откройте раздел Workspace и выберите датасет, чтобы посмотреть его содержимое.
          </p>
        </div>
      );
    }

    return (
      <DatasetTable
        dataset={selectedDataset}
        finding={openFinding}
        onBackToHealth={onBackToHealth}
        onClearFinding={onClearFinding}
      />
    );
  }

  if (section === "health") {
    if (!selectedDataset) {
      return (
        <div className="empty-state">
          <h2 className="empty-state__title">Датасет не выбран</h2>
          <p className="empty-state__text">
            Откройте раздел Workspace и выберите датасет, чтобы посмотреть его диагностику.
          </p>
        </div>
      );
    }

    return <DatasetHealth dataset={selectedDataset} onShowRows={onShowFindingRows} />;
  }

  return <StagePlaceholder health={health} />;
}


function StagePlaceholder({ health }: { health: HealthStatus | null }) {
  //#этот экран честно говорит, что разделов ещё нет, вместо того чтобы показывать кнопки без действия
  return (
    <div className="empty-state">
      <h2 className="empty-state__title">Раздел ещё не реализован</h2>
      <p className="empty-state__text">
        Работают разделы Workspace, Dataset, SQL и Health. Builder и Recipes появятся
        на следующих этапах: в навигации они недоступны, чтобы в интерфейсе не было кнопок,
        за которыми ничего нет.
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
