# Исследовательские материалы Decision Lab

| Документ | Содержание |
|---|---|
| [RESEARCH_PROBLEM.md](RESEARCH_PROBLEM.md) | проблема, актуальность, цель, обзор литературы, гипотезы, что не является новизной |
| [MATHEMATICAL_MODEL.md](MATHEMATICAL_MODEL.md) | формализация, TOPSIS с проверочным примером, режим C, доказательство инвариантности, устойчивость |
| [METHOD.md](METHOD.md) | псевдокод, где метод в продукте, связь с ModelArena, воспроизводимость |
| [EXPERIMENT_PROTOCOL.md](EXPERIMENT_PROTOCOL.md) | данные, измерения, сценарии, заранее объявленные метрики, угрозы валидности |
| [RESULTS.md](RESULTS.md) | только измеренные результаты проверочного эксперимента |
| [ARTICLE_OUTLINE.md](ARTICLE_OUTLINE.md) | план статьи и что сделать до подачи |

Данные: `data/` (4 датасета scikit-learn) и `data_openml/` (15 датасетов OpenML) — матрицы
измерений моделей; `results/` — результаты сравнений (`*_all.json` — расширенный эксперимент
на 19 датасетах).
Скрипты: `scripts/research/measure_models.py`, `measure_openml.py`, `decision_experiment.py`,
`bcd_experiment.py`.
Матрицы из `data/` можно загрузить в раздел Decision Lab кнопкой «Загрузить файл».
