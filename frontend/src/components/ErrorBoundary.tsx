import { Component, type ErrorInfo, type ReactNode } from "react";

//#без границы ошибок одна исключительная ситуация в разделе гасит всё приложение до белого экрана
//#пользователь при этом теряет открытый workspace и не понимает, что произошло

type Props = { children: ReactNode; section?: string };
type State = { error: Error | null };

export class ErrorBoundary extends Component<Props, State> {
  state: State = { error: null };

  static getDerivedStateFromError(error: Error): State {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo): void {
    //#подробности остаются в консоли разработчика, наружу уходит короткое сообщение
    console.error("Ошибка в разделе интерфейса", this.props.section ?? "неизвестен", error, info);
  }

  private handleReset = (): void => {
    this.setState({ error: null });
  };

  render(): ReactNode {
    const { error } = this.state;

    if (!error) {
      return this.props.children;
    }

    return (
      <div className="banner banner--error" role="alert">
        <div>
          <p className="banner__title">Раздел не удалось отобразить</p>
          <p className="banner__text">{error.message}</p>
          <button type="button" className="app-nav__item" onClick={this.handleReset}>
            Попробовать снова
          </button>
        </div>
      </div>
    );
  }
}
