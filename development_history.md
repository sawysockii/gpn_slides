# История разработки GPN Restyler

Файл ведётся с 2026-10-05. После каждого этапа сюда записывается:
что реализовано, с какими проблемами столкнулись, чем закончились тесты,
какие были запросы пользователя, что предстоит реализовать.

Корень проекта: `/Users/wysockii/Documents/gpn_slides`
Код: `gpn-restyler`, ветка `gpn-restyler`, base `820782a5fb8cb8f251d7c0750e4299c321cd068c`
Спецификация: `GPN_PPTX_Restyler_OpenCode_Architecture_and_Prompt.md` v1.2

---

## Этап 0 — Bootstrap / корпус / зависимости / baseline (2026-10-05)

### Реализовано
- Развёрнут nested-clone layout: корпуса (`ontology/`, `slide_examples/`) в корне
  проекта, код в `gpn-restyler/`; CLI резолвит `project_root` явно, не через cwd.
- Зафиксированы корпуса: `GPN_Slide_Design_Ontology.json` v1.2.0
  (`d24be59d…`), `GPN_Slide_Design_Ontology.md` (`a9a2fe1c…`),
  `slide_examples.pptx`, 99 слайдов (`0c6d6c00…`).
- Установлены и зафиксированы зависимости (`DEPENDENCIES.lock.json`):
  uv 0.9.8, Python 3.12.9, python-pptx 1.0.2, lxml 6.0.2, pydantic 2.12.5,
  pytest 9.0.2, ruff 0.15.4, LibreOffice, Poppler, tesseract (rus/eng).
- Baseline полного сюита: **198 passed**.

### Проблемы
- Файл `project_root/config/gpn.toml` отсутствует (есть только
  `code_root/config/gpn.example.toml`) — принято как факт, не как сбой:
  `slides gpn` работает config-free с явным `--project-root`.
- GPN_DIN binary отсутствуют — честный блокер `FONTS/GPN_FONTS_MISSING`,
  `assets_ready=false`, `strict_output_ready=false`.

### Тесты
- Baseline: 198 passed, 0 failed. `ruff check .` clean.

### Запросы пользователя
- Исходное задание: `GPN_Restyler_Stage_1_2_Review_and_Implementation.md` v1.0 —
  выполнить Stage 1–2 по спецификации v1.2 до проверяемого результата.

### Предстоит
- Stage 1: модели/IR/импортёр/леджер.

---

## Этап 1 — Модели / импортёр / леджер / темы / ссылки (2026-10-05)

### Реализовано
- Pydantic-модели IR (`SourceDeckIR`, `ObjectIR`, `TextRunIR` + `FieldMetadata`,
  таблицы, графики, `WorkbookSnapshot`, леджер, манифесты).
- Импортёр PPTX→IR без потерь: каждый shape получает `ObjectIR` или явный issue;
  исходник никогда не переписывается.
- `a:fld` сохраняет `FieldMetadata` + порядок; сноски, soft breaks.
- Theme profiles послайдно (layout→master→theme, не глобальный theme1);
  наследование стилей с provenance (`b="0"` — явный false).
- `resolve_internal_links` (owner-scoped OPC), `compare_chart_bindings`
  (cache↔workbook, Decimal-сравнение, `1` vs `1.0` равны), blocking
  `Uncertainty`, support/link-артефакты.
- 1869 объектов реального корпуса: `source_import_complete`,
  `source_data_verified=true`, 0 blocking uncertainties.

### Проблемы
- `python-pptx` сериализация может не сохранить неподдержанные части —
  принято правило: потеря = отказ патча, не silent success (задел на Stage 3).
- ID точек графиков и ячеек merged-таблиц тогда не проверялись на
  уникальность — дубли обнаружены позже, на Этапе 3 (см. ниже).

### Тесты
- +58 новых Stage 1–2 тестов; итого **256 passed, 0 failed**
  (`tests/gpn`: 98 passed). `ruff check .` clean (попутно исправлен
  предсуществующий E501 в `tests/gpn/test_import.py:383`).

### Запросы пользователя
- Продолжение исходного задания Stage 1–2 (тот же документ).

### Предстоит
- Stage 2: компилятор онтологии, чекери правил, шрифты, шаблон, списки, pipeline.

---

## Этап 2 — Компилятор / правила / шрифты / шаблон / списки / CLI (2026-10-05)

### Реализовано
- `compile_ontology`: canvas, 10 ролей, палитра + conditional-цвета
  (sky с обязательным evidence), font policy, LS01-профили, 325 записей
  каталога, реестр 26 правил (C01–C22 hard + R01–R04 contextual).
- 11 детерминированных чекеров, остальные — честный deferred-unknown
  (не fake-pass). Инвентаризация шрифтов по internal names.
- Структурная деривация базового шаблона (переоткрывается, shared parts целы).
- LS01 binder + идемпотентный writer.
- `slides gpn doctor` (exit 0), `import`, `profile` (exit 3, единственный
  блокер `FONTS/GPN_FONTS_MISSING`).
- Прогоны `runs/stage1-stage2/2026-10-05-04` (doctor) и `-05` (profile,
  18 артефактов).

### Проблемы
- Конфликт-анализ считал `equivalent` по совпадению ID/текстов — строгая
  типизация отложена на Этап 3.
- Покрытие правил считалось без IR/леджера (vacuous `not_applicable`);
  `pipeline` перезаписывал реальный `rule_coverage.json` пустым `[]`.
  Обнаружено и исправлено на Этапе 3.
- Дубли ID атомов леджера на реальном корпусе (56) не детектировались —
  C05 всегда возвращал pass. Обнаружено и исправлено на Этапе 3.

### Тесты
- Итог этапа: **256 passed, 0 failed**; `tests/gpn` 98 passed; ruff clean.
- Негативы: conflict vs paraphrase, `1` vs `1.0`, fixed-blue vs buClrTx,
  14→12→10→9→8 до infeasible, filename-vs-internal-name шрифтов.

### Запросы пользователя
- То же задание Stage 1–2; отчёт принят, следующим запрошено Stage 3
  (`GPN_Restyler_Stage_3_Verification_and_Native_Operations.md` v1.0):
  адресная проверка Stage 1–2 + native-операции до сохранённого результата,
  без переписывания рабочих модулей и без повторной установки.

### Предстоит
- Stage 3: Unicode-поиск, autofit, адресные операции, GPN-guard, preflight,
  CLI-команды, сохранённый candidate + реимпорт.

---

## Этап 3 — Адресная проверка Stage 1–2 + native-операции (2026-10-05)

### Реализовано
**Проверка Stage 1–2 (адресно, без реимплементации):**
- Классификатор норм переписан на типизированный: `normalize_predicate` /
  `compare_predicates` (Decimal-границы, точные юниты, scope-overlap).
  `x≥8/x=8` — теперь `compatible_refinement` (было ложным contradiction),
  body/footnote — `disjoint_scope`, условный sky — `unresolved`/blocking.
  MD-извлечение берёт всю строку (condition+remedy) — раньше терялись 8pt
  (C18) и bold-флаг (C21).
- Чекеры C04/C05/C11/C13/C14/C20 получили детерминированные части
  (`checked_scopes`); биндинги: 8 implemented + 7 partial.
- Гиперссылки: identity occurrence (shared rId сохраняется), hover-fallback,
  `iter_hyperlink_occurrences`, `clrMapOvr` влияет на scheme-цвет.
- Doctor: scoped readiness + `--require-production-assets` (exit 3) /
  `--check-model`. Chart-конфликт → `needs_review`/exit 4 (подтверждено).
- Исправлены найденные баги Stage 1: plot-scoped ID точек (коллизии),
  одна ячейка на координату merged-таблицы (56 дублей леджера → 0),
  затирание `rule_coverage.json`, vacuous-покрытие без IR.

**Stage 3 (новое):**
- Unicode `find_text` (`[^\W_]+` + casefold; Ё/Е различны; сниппет из оригинала).
- Explicit autofit: `legacy_shrink` (default, совместимость) / `none`
  (ровно один `noAutofit`, только свой `bodyPr`).
- Typed операции `set_shape_geometry` / `set_shape_style` / `delete_shape`:
  адресация `(slide, group_path, shape_id)`, канонический хэш
  `c14n-exclusive-v1`, stale-отказ до мутации, транзакционный rollback,
  атомарный style-патч без потери rich-контента (uniform_runs создаёт
  минимальный rPr вместо silent no-op).
- `gpn/patching.py`: `authorize_gpn_edit` (политика/леджер/protected;
  «это декор» ничего не разрешает), `plan_shape_deletion`,
  `resolve_native_style` (без синтетического bold), `preflight_gpn_edits`
  (на копии, source immutable), `apply_gpn_edits` (превью → независимый
  реимпорт → леджер/пакетный гейт → атомарная запись; `committed_count=0`
  при отказе).
- CLI: `preflight-edits` / `apply-edits` (служебные, не режимы продукта);
  stdout — один JSON, диагностика — stderr; коды 2/3/4/6.
- Доки: `docs/SUPPORT_MATRIX.md`, `docs/IMPLEMENTATION_GUIDE.md`;
  discovery расширен `gpn-*` методами (аддитивно).
- Артефакты `runs/stage3/2026-10-05-02/`: фикстура (2 native-слайда, RU),
  батч (move + uniform-стиль), `diagnostic_candidate.pptx` (переоткрыт:
  1.5"/0.7" точный EMU, 14 pt `7E7E7E`, один `noAutofit`), реимпорт
  (0 missing atoms, данные равны, 0 removed parts), `profile/` (exit 3,
  реальное покрытие 26 правил: 7 pass / 7 fail контентных / 12 unknown).
- `IMPLEMENTATION_STATE.md` обновлён по всем блокам.

### Проблемы (все закрыты)
1. Новый классификатор дал 2 ложных `unresolved` (C18/C21) — причина:
   усечение MD-строк до condition-колонки. Исправлено full-row извлечением.
2. Ужесточённый C05 нашёл 56 дублей ID атомов на реальном корпусе —
   реальные баги reader'а (серии разных плотов, повторные covered-ячейки).
   Исправлено в reader'е; `test_table_merge_roundtrip` сначала упал
   (фикс был слишком агрессивным) — переделано на `emitted`-множество.
3. Style-патч молча не применялся без rPr — добавлен минимальный rPr.
4. Снапшот-гейт сравнивал ID буквально (source_hash меняется при реимпорте) —
   переведено на позиционные value-мультимножества.
5. `apply-edits` корректно отказал `OUTPUT_EXISTS` (exit 2) при повторе —
   скрипт чистит candidate перед прогоном; отказ записан честно.
6. 7 fail hardened-чекеров на демо-библиотеке — признаны контентными
   находками (демо-цвета/шрифты/пули/жирность), не регрессией.

### Тесты
- **284 passed, 0 failed** (full suite, ~60 c); `tests/gpn` **126 passed**
  (+28 новых `test_stage3_native.py` по матрице §§4–11).
- `ruff check .` clean.
- CLI-коды вживую: doctor 0, doctor+assets 3, find 0, preflight 0, apply 0,
  upstream `slides apply` 0, profile 3 (единственный блокер — фонты).

### Запросы пользователя
- Задание Stage 3 (документ v1.0) — выполнено полностью, без comprovisoв:
  запрещённые vol. 4 экспортер/рендер/шрифты не делались.
- По ходу: «продолжай» — работа продолжена с места остановки,
  контекст не потерян.
- Текущий запрос: создать этот файл и вести его после каждого этапа.

### Предстоит
- Stage 4: native exporter/transplant (таблицы/графики/заметки/ссылки)
  поверх контрактов Stage 3. Не стартовать автоматически.
- Позже: Stage 5 измерение/layout, Stage 6 индекс/планировщик, Stage 7 QA,
  Stage 8 документный workflow. Production-блокер неизменен: бинарники
  GPN-шрифтов (`strict_output_ready=false`).

---

## Этап 3.5 — Data-driven онтология + runtime LLM (harness) / спецификация (2026-10-05)

### Реализовано
- Спецификация `GPN_Restyler_Stage_3_5_Dynamic_Ontology_and_Runtime_LLM.md` v1.0:
  приоритет над предыдущими документами в вопросах источника нормативных значений,
  динамических наборов IDs, раннего подключения LLM и критериев Stage 3.5.
- Главный принцип зафиксирован: «Онтология определяет правила и их значения.
  Модель предлагает композицию. Код исполняет поддержанные правила и проверяет результат.»
- Data-driven онтология: согласованное изменение в изолированной копии JSON/MD
  меняет compiled profile, digest, validator и выдаваемые native properties
  без правки Python-кода.
- Runtime LLM через `harness`-провайдер (поправка 1.0): CLI пишет запрос
  в `model_requests/<request_id>.json` → модель харнесса генерирует ответ
  → `model_responses/<request_id>.json` → pipeline потребляет через тот же
  `generate()`-интерфейс с trace и валидацией; происхождение кандидата —
  `harness_model`.
- Начальный аудит hardcoded: алгоритм классификации (reads_ontology,
  normative_literal, normative_closed_set, format_constant, engine_policy,
  fixture_snapshot, schema_adapter); runtime callgraph.
- §2–§8 спецификации: конкретные контракты для `load_ontology_snapshot`,
  `compile_ontology`, `resolve_role_style`, `resolve_list_style`,
  `bind_rules`, `build_planning_view`, `select_planning_rules`,
  `retrieve_planning_examples`, `make_planning_packet`, LocalModelClient,
  LayoutIntent schema, system prompt.
- §9–§13: метамorphic-тесты онтологии (изменение JSON → новое поведение),
  диагностический bridge (LayoutIntent → Stage 3 edits), smoke run критерии,
  сохранность данных, два workflow, честные флаги готовности.

### Проблемы
- Корпоративный экспорт, трансплант неподдержанных объектов, точное измерение
  текста и финальная визуальная приёмка остаются последующими этапами.
- `strict_output_ready=false` — полный exporter/layout/render ещё не реализованы.
- Production-блокер неизменен: бинарники GPN-шрифтов (`production_assets_ready=false`).

### Тесты
- Спецификация содержит требования к метамorphic-тестам онтологии и
  transport/schema тестам для LLM-клиента; реализация тестов — впереди.

### Запросы пользователя
- Исходное задание Stage 3.5: `GPN_Restyler_Stage_3_5_Dynamic_Ontology_and_Runtime_LLM.md` v1.0 —
  data-driven онтология + runtime LLM до спецификации; на данном этапе не
  подключать локальный сервер, использовать harness-модель.

### Предстоит
- Реализация Stage 3.5: audit hardcoded → dynamic compiler → planner bridge →
  harness smoke run → метамorphic тесты онтологии.
- После: Stage 4 native exporter/transplant, Stage 5–8.

---

## Инфраструктура — весь рабочий код в git (2026-10-07)

### Реализовано
- Выяснено, что на GitHub (`sawysockii/gpn_slides`) были только 4 маркдауна:
  весь код лежал в `gpn-restyler/` с собственным вложенным `.git`
  (клон `mpuig/agent-slides`, ветка `gpn-restyler`), а вся работа
  Stage 1–3 (модели, импортёр, правила, patching, тесты, доки, скрипты)
  в нём даже не была закоммичена — висела uncommitted.
- История вложенного репозитория (base `820782a`, 125 файлов upstream)
  сохранена в `agent-slides-upstream-history.bundle` (31 МБ, локально,
  не в git). Вложенный `.git` удалён — `gpn-restyler/` теперь обычная
  папка единого репозитория.
- В git включён весь код: 177 файлов (`src/`, `tests/`, `config/`,
  `docs/`, `scripts/`, `pyproject.toml`, `uv.lock`, `DEPENDENCIES.lock.json`,
  `IMPLEMENTATION_STATE.md`, examples, website, skills и пр.);
  самый крупный файл 13.4 МБ (в пределах лимита GitHub).
- `.gitignore`: убран `gpn-restyler/`, добавлен `*.bundle`; корпуса
  (`ontology/`, `slide_examples/`) и `runs/` (553 МБ, файлы по 99 МБ)
  по решению пользователя остаются локальными.
- `AGENTS.md` обновлён под новую раскладку репозитория.

### Проблемы
- Уложено в существующий репозиторий без `reset`/`rebase`/`force push`:
  ветка `master` дополнена кодом, история markdown-коммитов сохранена.
- Связь `gpn-restyler/` с upstream `agent-slides` теперь только через
  bundle (восстановление: `git clone agent-slides-upstream-history.bundle`).

### Тесты
- Интеграционные тесты в этот прогон не запускались (изменены только
  git-раскладка и markdown); код не менялся. Последний известный прогон:
  284 passed (Этап 3).

### Запросы пользователя
- «мне нужно весь (абсолютно весь) рабочий код проекта сейчас включить
  в гит и запушить, потому что на гитхабе нет кода» + уточнение выбора:
  всё в один `master`, корпуса в git не включать.

### Предстоит
- При следующих работах — прогон тестов для подтверждения целостности
  после раскладки (284 passed ожидалось).
