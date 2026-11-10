//#выбор локальной модели: короткое имя видно, полное — в подсказке
export function ModelPicker({
  models,
  value,
  onChange,
}: {
  models: string[];
  value: string | null;
  onChange: (model: string) => void;
}) {
  return (
    <select
      className="decision-input ai-model"
      aria-label="Локальная модель"
      title={value ?? undefined}
      value={value ?? ""}
      onChange={(event) => onChange(event.target.value)}
    >
      {models.map((model) => (
        <option key={model} value={model}>
          {model.split("/").pop()}
        </option>
      ))}
    </select>
  );
}
