import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import * as endpoints from "../../api/endpoints";
import { WorkspacePanel } from "./WorkspacePanel";

/* Несколько файлов загружаются разом — выбором в диалоге или перетаскиванием.
   Неподходящий файл в середине выбора не должен отменять загрузку остальных. */

afterEach(() => {
  vi.restoreAllMocks();
});

function setup() {
  vi.spyOn(endpoints, "fetchDatasets").mockResolvedValue({ datasets: [] } as never);
  render(<WorkspacePanel workspaceId="ws_test000000000000" onSelectDataset={() => {}} selectedDatasetId={null} />);
}

describe("загрузка файлов", () => {
  it("ошибка одного файла не останавливает остальные и называет файл", async () => {
    const upload = vi
      .spyOn(endpoints, "uploadDataset")
      .mockRejectedValueOnce(new Error("формат не поддерживается"))
      .mockResolvedValue({} as never);
    setup();
    await screen.findByText(/Пока ничего не загружено/);

    const input = document.querySelector('input[type="file"]') as HTMLInputElement;
    const files = [new File(["x"], "плохой.bin"), new File(["a,b\n1,2"], "хороший.csv")];
    fireEvent.change(input, { target: { files } });

    await waitFor(() => expect(upload).toHaveBeenCalledTimes(2));
    expect(await screen.findByText(/плохой\.bin/)).toBeInTheDocument();
    expect(screen.getByText("Не все файлы загрузились")).toBeInTheDocument();
  });

  it("файлы можно перетащить в раздел", async () => {
    const upload = vi.spyOn(endpoints, "uploadDataset").mockResolvedValue({} as never);
    setup();
    const empty = await screen.findByText(/Пока ничего не загружено/);

    const files = [new File(["a"], "один.csv"), new File(["b"], "два.csv")];
    fireEvent.drop(empty, { dataTransfer: { files, types: ["Files"] } });

    await waitFor(() => expect(upload).toHaveBeenCalledTimes(2));
  });

  it("крестик удаляет датасет после подтверждения", async () => {
    const dataset = {
      dataset_id: "ds_0000000000000001",
      workspace_id: "ws_test000000000000",
      name: "cities.csv",
      source: { file_name: "cities.csv", format: "csv", bytes: 10, sha256: "a".repeat(64) },
      schema: { columns: [] },
      row_count: 2,
      column_count: 2,
      status: "ready",
      status_reason: null,
      original_format: "csv",
      working_format: "csv",
      normalized: false,
      conversion: null,
      created_at: "2026-01-01T00:00:00+00:00",
    };
    vi.spyOn(endpoints, "fetchDatasets").mockResolvedValue({ datasets: [dataset] } as never);
    const remove = vi.spyOn(endpoints, "deleteDataset").mockResolvedValue();
    vi.spyOn(window, "confirm").mockReturnValue(true);
    const deleted = vi.fn();
    render(
      <WorkspacePanel
        workspaceId="ws_test000000000000"
        onSelectDataset={() => {}}
        selectedDatasetId={null}
        onDatasetDeleted={deleted}
      />,
    );

    fireEvent.click(await screen.findByRole("button", { name: "Удалить cities.csv" }));

    await waitFor(() => expect(remove).toHaveBeenCalledWith("ws_test000000000000", "ds_0000000000000001"));
    await waitFor(() => expect(screen.queryByText("cities.csv")).not.toBeInTheDocument());
    expect(deleted).toHaveBeenCalledWith("ds_0000000000000001");
  });
});
