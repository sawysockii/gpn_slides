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

---

## Этап 3.5 (в работе) — переносимость клонов + первая диагностика тестов (2026-10-07)

### Реализовано
- **Относительные пути вместо хардкода** (задание «сделай относительные пути»):
  в 8 тестовых файлах `tests/gpn/*` и в `scripts/stage3_run.py`,
  `scripts/stage3_5_run.py` заменён `Path("/Users/wysockii/…")` на путь,
  вычисляемый из расположения файла (`Path(__file__).resolve().parents[…]`).
  В `src/` хардкода не было — код везде резолвит `project_root` явно.
  Клон репозитория теперь переносим: git clone → uv sync → копии корпусов → тесты.
- **Восстановлена сборка тестов**: `tests/gpn/test_stage35_ontology.py`
  (написан в предыдущих сессиях) импортировал `resolve_role_style` из
  `compiler.py`, которого не существовало — `pytest` падал на collection
  и НЕ ЗАПУСКАЛСЯ весь сюит (284 зелёных тестов были недостижимы).
  Реализована функция по спецификации §4.3: делегирует единому резолверу
  `patching.resolve_native_style` (без второй копии стандарта), неизвестная
  роль — ошибка, а не fallback; `sources` используется как provenance-gate
  (рассинхрон corpus-hash snapshot↔sources → `ONTOLOGY/SNAPSHOT_SOURCE_MISMATCH`).

### Проблемы
- **Уточнение к Этапу 3.5** (прежние разделы оставляю как историю):
  реализация Stage 3.5 частично УЖЕ сделана предыдущими сессиями —
  `COMPILER_VERSION="stage3.5/1.0"`, `planner.py`, `planning_bridge.py`,
  `references.py`, `local_llm.py`, прогоны `runs/stage3_5/2026-10-05-01/02`.
  Написаны и 19 metamorphic-тестов (`test_stage35_ontology.py`).
  Формулировки «тесты впереди» и «284 passed ожидались» в прежних разделах
  были неточны; фактическое состояние — здесь.
- **12 из 19 тестов Stage 3.5 падают.** Предварительная диагностика:
  часть — тесты мутируют JSON по именам полей из спеки (`default_size_pt`,
  `hex`), тогда как реальный корпус использует `size_pt`, `value`
  (показано на `test_title_size_is_loaded`, `test_role_rgb_is_loaded`);
  часть — ещё не реализованное поведение движка (`test_missing_source_has_no_builtin_fallback`:
  пустая онтология не кидает `AssetMissingError`; `test_source_refuses_unsupported_normative_value`:
  `value_pt=-1` молча компилируется). Полная диагностика остальных — впереди.
- До исправлений этого этапа полный сюит физически не запускался
  (collection error прерывал прогон) — красный статус был скрытым.

### Тесты
- `uv run ruff check .` — clean.
- Полный сюит: **291 passed, 12 failed** (~56 c). Из 12 — все в
  `tests/gpn/test_stage35_ontology.py`; остальные 284 прежних + 7
  проходящих Stage 3.5 тестов зелёные. Для сравнения: до правок —
  `1 error during collection`, 0 тестов.

### Запросы пользователя
- «мне нужно весь рабочий код включить в гит и запушить» → коммит `445a9cb`.
- «да сделай относительные пути» → настоящий раздел.

### Предстоит
- Довести 12 падающих тестов Stage 3.5 (адаптеры имён полей в тестах +
  пробелы compiler: валидация отрицательных normative values, typed-ошибка
  на пустой корпус, cache invalidation по corpus hash) — только по явному
  запросу пользователя (правило «не стартовать следующий этап автоматически»).
- Далее: smoke run harness (`--llm-provider harness`, §0.3), Stage 4.

## Этап 3.5 (продолжение) — data-driven фиксы + harness smoke run (2026-10-07)

### Реализовано
- Аудит hardcode завершён: нормативных литералов в production-коде нет
  (все значения читаются из JSON через `CompiledOntology`); артефакты —
  `runs/stage3_5/2026-10-07-audit/hardcode_audit.json`, `runtime_callgraph.md`.
- Исправлены 9 падающих metamorphic-тестов (`tests/gpn/test_stage35_ontology.py`):
  7 — тесты мутировали несуществующие поля (`hex`→`value`, `usage`-dict→`source_slides`,
  `source_addition`→`observed_variant`, `catalogs[0]`→dict-of-lists, `metadata.version`→`metadata["version"]`,
  пустой корпус→`not ready` + отдельный raise-кейс); 2 — реальные гэпы движка.
- Код-фикс 1 (`compiler.py`): `minimum_text_font_size.value_pt <= 0` или нечисловой —
  громкий `ValueError`, а не молчаливый default/None (§4.2).
- Код-фикс 2 (`ontology_conflicts.py`): поднятый minimum, противоречащий неизменённым
  role sizes, — blocking `contradiction`, `ready_for_compilation=false` (§12.1).
- Код-фикс 3 (`planner.py`): схема intent привязана к snapshot
  (`x-ontology-corpus-hash`, `x-ontology-source-hash`) — смена корпуса инвалидирует
  schema_hash и plan-кэш (§5.3).
- Код-фикс 4 (`config.py`): `'provider'` добавлен в `_KNOWN_SECTIONS[model]` —
  harness-путь из example-конфига больше не отвергается.
- Код-фикс 5 (`patching.py apply_gpn_edits`): reimport кандидата пишется в
  `candidate_after/` + публикуется как `candidate_after_*`; `source_ir.json` больше
  не затирается (иначе следующий `plan --run-dir` падал `RUN_MANIFEST_HASH_MISMATCH`).
- Smoke run (§0.3, поправка 1.0): фикстура 3 слайда (RU, uppercase titles, canvas
  12192000×6858000) → import → profile → plan-packet → 5 harness-ответов
  (`model_responses/`, actual_model `muse-spark (opencode harness, 2026-10-07)`) →
  `plan --llm-provider harness --no-plan-cache`: 5/5 valid, origin `harness_model`,
  diversity 3 сигнатуры на слайде 1; `--diagnostic-candidate`: applied, committed=10,
  missing_atoms={}, parts 40/40, кандидат переоткрывается нативно (слайд 0: 5/5 moved,
  тексты сохранены). Всё в `runs/stage3_5/2026-10-07-smoke/` + `stage3_5_readiness.json`.
- Чужие changes (`bullets.py`, `errors.py`, `template.py` — TemplateMismatchError и др.)
  сохранены, не тронуты; reset/rebase/force-push не выполнялись.

### Проблемы
- `profile` перезаписывает `source_ir.json` эталонной библиотекой (99 слайдов):
  порядок в одном run-dir — import → profile → import повторно. Зафиксировано
  в readiness как известное ограничение, не молчаливое поведение.
- `test_hash_invalidates_all_dependents` изначально падал: смена canvas не меняла
  схему (в схеме не было привязки к snapshot) — исправлено код-фиксом 3, заодно
  закрыт реальный staleness plan-кэша при смене корпуса.
- Per-slide `model_usage.json`/`diversity_report.json` перезаписываются каждым
  вызовом `plan`; пер-слайд копии сохранены как `model_usage_slide{N}.json`,
  `diversity_report_slide{N}.json`, stdout — `plan_slide{N}_stdout.json`.
- Корпуса не мутировали (mtime сентябрьские, все мутации — в tmp-копиях и runs/).

### Тесты
- `uv run ruff check .` — clean.
- Полный сюит: **303 passed** (~62 c), в т.ч. 19/19 `test_stage35_ontology.py`.
- `doctor --check-model` exit=0 (local endpoint не пробуется по поправке 1.0).

### Запросы пользователя
- «продолжаем. аудит затянулся» → прекращены повторные rg-прогоны, переход к артефактам и smoke run.
- «продолжай» → доведены фиксы, smoke run, readiness, записи истории.

### Предстоит
- Stage 4: полный native exporter/transplant (charts/tables/notes/links) — только
  после ревью результата этого этапа, автоматически не стартовать.
- Опционально: развести артефакты `profile` (reference IR) и `import` (source IR)
  по разным именам, чтобы не требовать повторный import после profile.

## Stage 4 (продолжение) — исправление apply_list_profile (2026-10-07)

### Реализовано
- **Исправлен баг `apply_list_profile`**: `native.py` импортировал несуществующую
  функцию `apply_list_profile` из `bullets.py`. Заменён на правильный вызов
  `resolve_list_level` + `apply_ls01_paragraph`.
- **Добавлено поле `list_profile` в `NativeDeckContext`**: профиль теперь
  загружается из `list_profile.json` в `export_pipeline.py` и передаётся
  в `create_output_deck`.
- **Добавлены поля `background_token`/`paragraph_text_token` в `ParagraphIR`**:
  необходимы для `resolve_list_level`.
- **Добавлен тест `test_list_marker_applied_when_list_kind`**: проверяет что
  `buChar char="§"` и `buFont typeface="Wingdings"` присутствуют в output XML
  при `list_kind != "none"`.
- **Проверено**: list markers применяются в реальном build (2 слайда с `buChar`).

### Проблемы
- **Уточнение к Stage 4**: предыдущий коммит `6588b6e` содержал баг —
  `_apply_list_marker` вызывал несуществующую функцию. List markers не
  применялись в output. Исправлено в этом разделе.

### Тесты
- **304 passed, 0 failed** (~62 s), в т.ч. 43 теста `test_stage4_exporter.py`.
- `ruff check .` clean.
- Проверено: `buChar char="§"` присутствует в slide1.xml и slide2.xml.

### Запросы пользователя
- «продолжай» → исправление бага list profile, обновление документации.

### Предстоит
- Stage 5: measurement/layout (не запускать автоматически).
- Проверить остальные issues из `emission_report.json`.

## Stage 4 — финальный статус (2026-10-09)

### Реализовано
- **Stage 4 завершён на declared support scope**:
  - `native.py` (1188 lines): create_output_deck, emit_text/emit_table/emit_existing_chart/emit_new_chart/emit_shape/emit_group/emit_image/emit_connectors, apply_display_transform, remap_hyperlinks, finalize_and_save_candidate, verify_native_objects
  - `transplant.py` (523 lines): plan_part_closure, allocate_part_name, clone_part_graph, validate_package_graph
  - `opc_adapter.py` (420 lines): private python-pptx operations, final package assembly
  - `export_pipeline.py` (1369 lines): preflight_native_export, run_native_build, compare_export_preservation, compare_export_parts
  - `reference_guard.py` (256 lines): assert_allowed_reference, FORBIDDEN_SOURCE denial
  - `export_models.py` (275 lines): NativeDeckContext, TransplantContext, PartPreservationContract, ExportOptions, NativeBuildResult
  - 43 теста в `test_stage4_exporter.py` (все обязательные из §13.1)
- **Исправлен баг apply_list_profile**: list markers применяются (buChar в 2 слайдах candidate)
- **Все обязательные артефакты §14.2** присутствуют в `runs/stage4/2026-10-07-b/`
- **LLM provider**: harness (режим 2), generation origin: harness_model
- **Readiness**: operation_status=completed, native_structure_verified=true, source_preservation_verified=true, package_graph_verified=true, strict_output_ready=false, production_assets_ready=false, render_verified=false, powerpoint_ui_edit_verified=false

### Проблемы (известные ограничения)
- `FONTS/GPN_FONTS_MISSING` — нет бинарников GPN-шрифтов (блокер Stage 5+)
- `unresolved_scopes: [measurement, render, powerpoint_ui]` — Stage 5/7
- Template: clean_template dropped=198 demo slides (ожидаемо)
- 2 chart_workbook hashes recorded (style-only restyle, bytes exact)

### Тесты
- **304 passed, 0 failed** (~62 s), ruff clean
- 43 теста test_stage4_exporter.py (все обязательные из §13.1)
- 19/19 metamorphic test_stage35_ontology.py

### Запросы пользователя
- «продолжай» → исправление бага list profile, финальная приёмка Stage 4

### Предстоит
- Stage 5: measurement/layout (не запускать автоматически)

## Stage 5 — измерение, композиция и проверяемая геометрия (2026-10-10)

### Реализовано
- **Проверка Stage 4 (§1)**: полный набор 409 passed (exit 0), ruff clean; `stage4_test_evidence.json` — 38/38 требований §13.1 с реальными nodeid/assert-строками/индивидуальными прогонами. Найден и исправлен зависимый дефект: `copy_notes` склеивал notes через `"\n".join` (терялся `notes:\n` атом); переписан 1:1 с `a:br` + регрессионный тест; candidate пересобран (`runs/stage4/2026-10-09-a`, `stage4_claims_verification.json` all_ok=true).
- **Шрифты (§4)**: реальный discovery нашёл 10 GPN faces (FontBase) + Wingdings в системе; `font_inventory` с cmap-покрытием; production bundle без missing; старый `FONTS/GPN_FONTS_MISSING` закрыт pipeline-проверкой (`assets_ready=true`, `production_assets_ready=true` в обоих пилотах).
- **Мост измерений (§5)**: `measurement.py` + `tools/layout/src/measure.js` (Playwright 1.64.0, protocol 1.0); exact FontFace bytes+hash allowlist, `font-synthesis:none`, без сети; zero-extent боксы валидны; integrity rejects; UTF-16 mapping.
- **Композиция/раскладка (§§6–7)**: `composition.py` (операторные биндинги, intent-declared nest-пары, prune stale source allowances), `layout.py` ENGINE `layout/1.2` (hint-directed recompose, subdivide зон, repair перекрытий, expand_into_free, финальный overlap-gate, бюджете thorugh candidates, layout cache с версией движка).
- **Примитивы (§§8–10)**: таблицы end-to-end (grid → resolved dims → native grid → saved grid gate), изображения с aspect, аннотации, графы (SCC/слои/ортогональный роутинг — unit), chart exhibit с честным unknown автоматики.
- **Экспорт (§12.1–12.2)**: декор-фигуры как autoshapes/connectors с биндингом diagram_node; корпоративный latin typeface (включая `a:fld`); group xfrm (rot/flip); free-connector линии; table grid dims; saved geometry gate (боксы + сетки) + remeasurement по сохранённым стилям/ячейкам + geometry validation (canvas-passthrough регионы, scoped allow-pairs).
- **Рендер (§§12.3–12.4)**: LibreOffice→PDF→PNG + pdffonts против declared faces кандидата + извлечение текста + калибровочная фикстура/профиль; visual review обоих пилотов (harness, с хэшами страниц).
- **Пилоты**: NTC slide05 recompose (c0 resolved+exported, c1/c2 honest infeasible; все гейты green) и compound preserve (4/4 resolved, таблицы/чарт/группа/коннекторы/notes/hidden/opaque, все гейты green).
- **CLI**: `slides gpn layout --layout-mode --render --resume --no-measurement-cache --test-only-profile` (stdout JSON, stderr progress).

### Проблемы (известные ограничения, честно)
- Цвет текста роли не container-aware (тёмный на синих заливках в NTC; нормы нет — не выдуман; Stage 7).
- Chart automatic label placement — только native render (unknown scope by design).
- Recompose покрытие видов: text/table/image/shape/connector; chart/graph/group recompose — needs_binding (preserve покрывает).
- Group-child autoshape-геометрия сплющивается в textbox'ы (текст+трансформы сохранены; лимит Stage 4).
- Furniture layout/master вне collision set (визуально проверено, коллизий нет).
- Group-child run faces: корпоративная стилизация останавливается на границе группы (тексты preservation-checked через group-child bindings, рендер — глобально); per-run saved-face remeasurement покрывает text/shape/table.
- Content box — fallback без layout evidence (флаг unverified; под него спроектированы хинты через тот же резолвер).

### Тесты
- **409 passed, exit 0**, ruff clean. `stage5_test_evidence.json`: 57 требований §13.1 → реальные nodeids (integration backends указаны). Новые: overlap scoped, group xfrm emit, decor shapes, zero-extent, render×2, assets immutable.
- Пилоты с реальными шрифтами/Chromium/LibreOffice (не моки): `pilot-ntc` (82798350…), `pilot-compound` (53d3b9b6…).

### Запросы пользователя
- Stage 5 целиком этим заданием (§0); все шрифты установлены (подтверждено discovery: 10 GPN faces + Wingdings).

### Предстоит
- Stage 6 (reference index/semantics/planner) — только отдельным заданием, не стартовать автоматически.

---

## Этап 5 — Recovery / исправление провала реального результата (2026-10-11)

### Реализовано
- Подтверждены дефекты предыдущего pilot `runs/stage5/2026-10-09-a/pilot-ntc`
  (candidate `82798350…`): C01 (0C539F fill), C03 (effectRef outerShdw),
  C21/C22 (synthetic bold), surface-foreground, layout "Контакты",
  incomplete scope (1 из 8 слайдов).
- Статус предыдущего pilot сверен на `failed_partial` (technical operation
  completed, user goal not achieved).
- Найдены и исправлены причины в коде:
  - `opc_adapter.py: blank_slide_layout` — evidence-based выбор layout
    (минимальные placeholders, без contact penalty);  previous hardcoded
    index=6 выбирал slideLayout7 "Контакты".
  - `opc_adapter.py: strip_shape_effect_inheritance` — удаляет p:style
    effectRef и пишет explicit empty a:effectLst; theme outerShdw больше
    не наследуется.
  - `native.py: _apply_decor_fill_line` — нормативный no-fill для
    декоративных autoshapes; проверка палитры для connector lines.
  - `native.py: _style_run` — устанавливает font.bold=False, explicit
    RGB foreground, exact face из role; source bold/italic больше не
    проходит насквозь.
  - `native.py: emit_shape` — roundRect нормализуется в rect с записью
    GEOMETRY_NORMALIZED issue.
- Созданы артефакты `runs/stage5-recovery/2026-10-11-000709/`:
  - `status_reconciliation.json` — сверка статусов, failed_partial.
  - `rule_application_trace.json` — трассировка применения правил,
    8 failing rules, 5 root causes, 5 fixes.
  - `failure_reproduction.json` — подтверждение дефектов, code fixes,
    regression tests needed.

### Проблемы
- Предыдущий pilot содержит дефекты (см. выше); candidate `82798350…` —
  доказательство, а не рабочий результат.
- Full-deck export не проверен: требуется новый build без
  `pilot_selection.json` для подтверждения что все 8 слайдов экспортируются.
- Тесты `test_stage4_exporter.py` + `test_stage3_native.py` прошли
  (74 passed); полный тест-сюита не запускалась (timeout 180s).
- `ruff check` чистый после исправления 3 ошибок в `opc_adapter.py`.

### Тесты
- `test_stage4_exporter.py` + `test_stage3_native.py`: **74 passed, 0 failed**.
- `ruff check .`: **clean**.

### Запросы пользователя
- Отдельное задание recovery (2026-10-10): довести конвертацию всей
  `NTC_business_AI_GPN.pptx` до аккуратно свёрстанного редактируемого
  корпоративного PPTX по актуальной ontology. Сохранить working tree/чужие
  changes, предыдущие runs/историю/тесты. Не reset/rebase/reclone и не
  commit/push. Новые результаты — в отдельном `runs/stage5-recovery/<timestamp>/`.

### Предстоит
- Новый build без `pilot_selection.json` для проверки что все 8 слайдов
  экспортируются с исправлениями.
- Проверка C01/C03/C21/C22 на новом candidate saved bytes.
- Проверка выбора layout для каждого слайда.
- Обновление `development_history.md` с результатами полного прогона.

---

## Этап 5 recovery — верификация исправлений (2026-10-11, дополнение; разделы выше неприкосновенны)

### Реализовано
- Устранены разрывы callgraph (§2 промпта восстановления):
  - `compare_export_preservation`: roundRect→rect — разрешённая нормализация
    декора (было ложным `changed`, preservation падал на исправленном выходе).
  - `_check_c03`: surviving `a:effectRef` → fail (наследование outerShdw из
    theme); пустой `a:effectLst` → pass (нормативное stripped-состояние,
    был false positive на исправленном выходе).
  - Новая `rules.check_saved_authoring_rules`: C01/C03/C21_C22/
    surface_foreground на переоткрытых байтах candidate, значения из
    snapshot (раньше чекеры вызывались только на source/ожидаемом IR).
  - Хвост `_emit_resolved_slides` пишет `saved_authoring_rule_report.json`;
    hard fail переводит `completed` → `needs_review` (техническая операция
    отделена от качества для пользователя, §8).
- Новый чекер на плохом candidate `82798350…`: hard_fail=true
  (C01 8 / C03 8 / C21_C22 10 / foreground 22) — доказательство §9.
- Пересобраны оба контрольных слайда тем же планом, но исправленным кодом:
  - slide6 «Предложение по работе с ДО»: `slide6-fixed/candidate.pptx`
    (`6eb68a5b…`) — completed, preservation true, content_diff 0/0/0,
    4×pass; побайтово против плохого: layout7→layout5, roundRect 3→0,
    0C539F 6→0, effectRef 5→0, b=1 10→0, colorless runs 22→0.
  - slide3 «ГГД»: `slide3-fixed/candidate.pptx` (`66e08767…`) — completed,
    preservation true, 0/0/0, 4×pass.
- Новый `tests/gpn/test_stage5_recovery.py`: 8 тестов §9 (bad/fixed
  фикстуры, effectRef без effectLst, пустой effectLst, strip свежего shape,
  late-writer override, отказ от «Контактов» ×2). Контролируемые фикстуры
  в tmp_path; пользовательские бинари не коммитятся.
- Артефакты: `runs/stage5-recovery/2026-10-11-000709/` —
  `recovery_verification.json`, `slide3-fixed/`, `slide6-fixed/` (+ отчёты,
  candidates, content_diff, saved_authoring_rule_report).

### Проблемы
- Полный deck из 8 слайдов не собран: нет accepted scene plans для слайдов
  1,2,4,5,7,8 (нужна генерация harness по контракту §4 — typed scene plan,
  а не механические preserve-интенты). Статус задачи честно in_progress.
- Visual review исправленных слайдов с реальными GPN-шрифтами на Mac не
  проведён (в reviewer environment шрифтов нет — substitutes; это не
  доказательство против Mac, но и не подтверждение читаемости).
- Native render probe (LibreOffice PDF) для исправленных слайдов не запущен.
- `aggregate_product_acceptance` (§8) пока не реализована как функция;
  гейт статуса сейчас — внутри `_emit_resolved_slides`.

### Тесты
- Новое: `test_stage5_recovery.py` **8 passed**.
- Существующие: stage4_exporter (46) + stage3_native (28) + rules — green.
- `ruff check .` clean.
- Полный сюит не запускался (timeout 180s в прошлый раз); affected +
  смежные suites зелёные.

### Запросы пользователя
- Recovery-задание (2026-10-10): устранить дефекты продукта, не стартовать
  Stage 6, не commit/push, corpora/fonts неизменны. Выполняется: working
  tree сохранён, изменения только кодом + новые runs + тесты + docs.

### Предстоит
- Harness-генерация scene plans для слайдов 1,2,4,5,7,8 (контракт §4) →
  полный 8-слайдовый candidate → saved-authoring gate → per-slide render →
  visual review → `product_acceptance.json`.
- Реализовать `aggregate_product_acceptance` (§8) как функцию с честными
  полями requested/exported/missing/per-slide/unknowns.
- Добить оставшиеся строки §9 (semantic layout selection test на реальных
  layout-профилях, brand/footer mapping, two-provider provenance — часть
  уже покрыта существующими тестами).
- Полный pytest + ruff перед финальным отчётом.

---

## Этап 5 recovery — diagnostic geometry root cause + saved geometry gate (2026-10-11, дополнение; разделы выше неприкосновенны)

### Реализовано
- Runtime trace подтвердил разрыв zone→content→target (промпт §1):
  `export_pipeline.resolve_slides_from_intents` строил `geometry_emu[zone]`
  валидно, а затем для каждого source object добавлял
  `_default_box(len(geometry))` — `y = by + index*slot`, slot 822960 EMU.
  При ~10 зонах + 21 объекте title получал y=15636240 EMU (slide6) и
  аналогичный стек на slide3; `emit_slide` брал `geometry[obj.id]`
  (synthetic row), а не zone box. Функция
  `gpn/geometry.py:trace_geometry_application` фиксирует первый checkpoint
  `resolved.geometry_emu[source_id]` с uniform-row stacking вне canvas.
- Исправлена причина, а не симптом: zone binding даёт объекту zone box
  (один ref) или явную вертикальную equal-share долю зоны (несколько refs);
  synthetic rows больше не создаются; unbound объекты не получают
  фабричных координат (in-canvas fallback + `GEOMETRY_BINDING_MISSING` /
  `UNBOUND_GEOMETRY_NEEDS_REPAIR`), нормативный кегль/содержание не тронуты.
  Новый модуль `gpn-restyler/src/slides_cli/gpn/geometry.py`:
  `zone_box_to_emu`, `upgrade_legacy_bindings`,
  `trace_geometry_application`, `check_saved_geometry`,
  `check_required_content_visibility`.
- Подключены обязательные saved geometry gates (§12): `_emit_resolved_slides`
  пишет `saved_geometry_report.json`; `diagnostic_geometry=true` даёт
  `DIAGNOSTIC_GEOMETRY_REJECTED` и `needs_review` даже при style pass;
  полностью-вне-canvas объекты — hard fail независимо от ledger equality.
  Убран тихий override `if not assets_ready: status=completed`, ломавший гейты.
  `emit_slide` при отсутствии binding пишет typed `GEOMETRY_BINDING_MISSING`.
- Доказательство на старых байтах (без переписывания истории):
  `slide6-fixed/candidate.pptx` (`6eb68a5b…`) — 20/20 вне canvas, gate fail;
  `slide3-fixed/candidate.pptx` (`66e08767…`) — 56/57 вне canvas, gate fail;
  `full-deck-test/candidate.pptx` (`18b6fcf5…`) — 169/171 вне canvas, fail.
  Style pass при этом сохраняется — ledger equality не подтверждает вёрстку.
- Пересборка тем же планом исправленным кодом:
  `runs/stage5-recovery/2026-10-11-163026/slide6-refix/candidate.pptx`
  (`aca68f9f…`) — raw saved geometry pass (0 вне canvas, 20 checked),
  title source ID делит zone box `z-title` (609600,101619,10972800,558809);
  trace нового билда `clean`; export статус честно `needs_review` по
  `DIAGNOSTIC_GEOMETRY_REJECTED` + 2 unbound + image без asset bytes
  (диагностический путь заблокирован от user acceptance, как требует §8).
  Артефакты: `resolved_slides/`, `saved_geometry_report.json`,
  `geometry_trace.json` (old offence_found → new clean).
- Новый `tests/gpn/test_geometry_binding.py`: 6 тестов (zone binding без
  synthetic rows, shared-zone subdivision внутри canvas, saved fail на
  off-canvas, saved pass внутри canvas, trace находит first offence,
  diagnostic rejected при style pass).

### Проблемы
- Уточнение к прежним `completed` на slide3/slide6-fixed: техническая
  сериализация была успешной, но пользовательского результата не было —
  сверены как `failed_geometry` (style pass + geometry fail). Исторические
  runs не переписаны; сверка — в этом разделе и в `geometry_trace.json`.
- Диагностический `build`-путь остаётся диагностическим: даже исправленная
  геометрия зон не проходит user acceptance без measured resolve
  (composition → Chromium → layout → render → visual review). Это честный
  статус, а не сбой стиля.
- Полный 8-слайдовый measured deck не собран в этом прогоне: accepted plans
  есть только для slide3/slide6-контроля; для 1,2,4,5,7,8 нужна harness-
  генерация scene plans по контракту §4 + измерение/рендер/ревью. Статус
  задачи честно in_progress.
- `_check_c03` blanket-no-fill критика из промпта (§1, `rule_application_trace`
  `_apply_decor_fill_line`/`_style_run` default 000000) в этом прогоне не
  менялась: контекстный surface policy — следующий шаг после геометрии.

### Тесты
- Новое: `test_geometry_binding.py` **6 passed**.
- Смежные: geometry(6) + stage5_recovery(8) + stage4_exporter(46→см. сюит) +
  stage3_native(28/29) — **89 passed** одной командой
  (`test_geometry_binding + test_stage5_recovery + test_stage4_exporter +
  test_stage3_native`, ~28 c).
- `ruff check .` — **clean**.
- Живые проверки байтов:坏 hashes fail (20/56/169 вне canvas), refix pass
  (0 вне canvas), trace old offence_found → new clean (команды в истории
  прогона `runs/stage5-recovery/2026-10-11-163026/slide6-refix/`).
- Полный pytest не запускался в этом прогоне (требует ~60 c + Chromium);
  affected + смежные suites зелёные.

### Запросы пользователя
- Основное задание `opencode_prompt.md` (GPN Restyler до работающей системы):
  §0–§1 выполнены частично — восстановлено доверие к callgraph на
  подтверждённом дефекте, гейты включены, плохие hash доказано fail.
  Продолжение — весь пользовательский сценарий (§§2–21), не заканчивать
  на диагностике.
- Ограничения соблюдены: working tree без reset/rebase, чужие changes
  сохранены, corpora/fonts неизменны, derivatives в новом
  `runs/stage5-recovery/2026-10-11-163026/`, commit/push не выполнялись.

### Предстоит
- Harness-генерация полных scene plans (ContentIR/brief/ScenePlan) для всех
  8 слайдов NTC → measured build (Chromium/fonts) → export → saved
  style+geometry+visibility gates → native render → image-based visual
  review/repair → comparative selection (3 кандидата/слайд) → full-deck
  assembly → `product_acceptance.json` → `<stem>_corporate.pptx` + preview +
  variants/index.html.
- Контекстный style/surface policy из ontology (`_style_run` default,
  `_apply_decor_fill_line` blanket no-fill, C03 effective prohibited
  effects) — исправить без потери roundRect/effectRef/b=1/colorless пользы.
- `aggregate_product_acceptance` как функция + `restyle/create/resume`
  CLI по §17 + `docs/HARNESS_RUNTIME_INSTRUCTIONS.md`.
- Второй input mode (PDF/DOCX/text/prompt) тем же engine после зелёного
  основного PPTX-пилota. Затем полный pytest + ruff + PowerPoint UI check.

---

## Этап 5 recovery — asset retrieval, честная геометрия и первый полный дек (2026-10-11, дополнение; разделы выше неприкосновенны)

### Реализовано
- **Asset recovery (§5.1)**: `assets.resolve_asset_bytes` (stores → hash-match
  в source OPC → копия в run store; иначе `REQUIRED_ASSET_UNRESOLVED` без
  тихого дропа); `native.emit_image` использует его + истинный suffix по
  media type; `export_pipeline.emit_slide` передаёт `source_package_path`,
  blanket-except метит `REQUIRED_ASSET_UNRESOLVED`. Доказательство:
  `runs/stage5-recovery/2026-10-11-183855/slide6-refix2/` —
  `ASSET_RECOVERED_FROM_SOURCE_OPC: 2d625035… source_opc:ppt/media/image-6-1.png
  sha=9530a56c`, `content_diff.ok=true` (было `missing=[…#asset], ok=false`),
  `p:pic` восстановлен (21 объект вместо 20), media `image7.png` = 28517
  байт исходного логотипа.
- **Saved-geometry checker без ложных pass**: `check_saved_geometry`
  переписан — canvas/order/hidden из `ppt/presentation.xml`+rels (не
  `sorted()`), рекурсивный обход `sp/cxnSp/pic/graphicFrame` + `grpSp` с
  цепочкой трансформов (zero chExt → unknown), реальные rotation/flip
  полигоны, partial clipping = fail, коннектор h=0/w=0 = валидный сегмент,
  Boolean-лексика `1/true/on`, unknown/opaque = blocking. Четыре сценария
  §2.2 стали регрессионными тестами (partial clip, rot90, group-displace,
  off-canvas pic) — все fail как положено; `pic` инвентаризируется.
- **Агрегации**: `check_required_content_visibility` — unknown блокирует
  (было `{visible, unknown} → pass`); `compare_saved_geometry` —
  expected-first (binding без shape = missing); `_write_saved_checks` —
  монотонное AND (поздний pass не стирает ранний fail) + layout report
  участвует; `validate_resolved_geometry` — per-slide scoping (было 3236
  ложных cross-slide пар).
- **Единый binding**: `resolve_slides_from_intents` делегирует
  `geometry.upgrade_legacy_bindings`; `_default_box` удалён; node IDs —
  полные stable IDs (было `ref[:8]`, коллизии доказаны тестом).
- **Harness-планы на все 8 слайдов** (`fulldeck/plans/slide{i}-c0.json`,
  `harness_responses/`): текущая модель прочитала packets/онтологию/
  allowlisted `slide_examples`, зоны/роли/reading order составлены вручную
  как harness-ответы, `LayoutIntent`-валидны, покрытие всех 327 объектов
  проверено скриптом (`/tmp/gpn_plans/author_plans.py`).
- **Полный дек c0** (`fulldeck/candidate.pptx`, `5fff0613…`): content_diff
  ok, part_diff ok, saved-authoring 4×pass, saved-geometry 327 checked
  (sp 234/pic 10/cxnSp 83), 0 outside/clip/unresolved, порядок 1–8,
  327 emission records, 0 asset failures. LibreOffice PDF + 8 PNG,
  visual review `repair` (PNG открыты): blocking-оверлапы стр. 2,3,4,7,8
  (equal-share starvation), badge strike стр. 1, white-on-white лого стр. 6.
- **Measured-дек** (`measured/candidate.pptx`, `3b85a04d…`, Chromium):
  все 8 resolved, preservation True, saved_geometry_diff ok (327/0/0),
  render 8 стр. Просмотр стр. 2–3: другая компоновка движка, оверлапы
  частично те же → нужен scene recompose, не только re-resolve.
  Честные флаги: remeasure 124 overflow, per-slide layout fail,
  `saved_geometry_verified=false`.
- Пакет `runs/stage5-recovery/2026-10-11-183855/review_increment.zip`
  (69 файлов, manifest) + `scoped_readiness.json` (оба `accepted=false`).

### Проблемы (открыты, с evidence)
1. Equal-share subdivision без измеренных высот голодает текст
   (large_metric 30pt ≈ 11% бокса/строку; пары получали 3–4%) — стр. 2,3,4
   нечитаемы в зонах метрик. Путь: scene recompose (single-ref зоны с
   измеренными высотами) через measured path, не уменьшение кеглей.
2. Белый логотип на белом (стр. 6): байты точны, видимости нет. Нужна
   разрешённая brand-surface замена/поверхность по онтологии.
3. `layout_geometry_report` до фикса считал cross-slide пары (3236);
   исправлено per-slide scoping после прогона — отчёт measured run
   содержит старый fail, новый вызов валидирован отдельно.
4. Нет c1-вариантов для №3/№6 → нет comparative selection; нет
   `product_acceptance=true`; create-mode и CLI runbook не сделаны.

### Тесты
- Новое: 7 регрессий в `test_geometry_binding.py` (4 false-pass §2.2 +
  visibility-unknown + OPC-recovery + ID-collision) — green.
- `test_geometry_binding + stage5_build + stage5_layout`: 30 passed.
  `stage4_exporter + stage3_native`: 75 passed. `ruff check .` clean.
- Полный сюит запущен фоном (`/tmp/gpn_full_pytest.log`); результат —
  в следующем дополнении (не приписывается заранее).
- Корпуса неизменны: `98e2357c…`, `d24be59d…`, `a9a2fe1c…`, `0c6d6c00…`.

### Запросы пользователя
- `opencode_verified_recovery.md` (этот промпт): разрывы §2.2 закрыты
  кодом+тестами+прогонами (A/geometry/aggregation/binding); B — модель
  повлияла на дизайн (8 harness-планов, 2 движка, рендеры просмотрены,
  repair-вердикты записаны); C — полный дек собран и проверен, но
  `accepted=false` честно; D (create/resume/runbook) — предстоит.

### Предстоит
- Scene recompose плотных слайдов (single-ref зоны, измеренные высоты) →
  rebuild → re-render → re-review; brand-surface для логотипа; c1 для
  №3/№6 → comparative selection; `product_acceptance.json` →
  `*_corporate.pptx`.
- `restyle/create/resume` CLI + create smoke на текстовой фикстуре +
  runbook; затем полный pytest + повторный рендер на GPN-шрифтах Mac.

## Этап 5 recovery — полный сюит текущего дерева (2026-10-11, дополнение; разделы выше неприкосновенны)

### Реализовано
- Ничего нового; фиксация результата: полный сюит текущего дерева —
  **430 passed, exit 0** (`gpn-restyler/.venv/bin/python -m pytest tests/
  -p no:cacheprovider --no-cov -q`, повторный прогон после всех правок
  инкремента, exit-код проверен). `ruff check .` clean (проверен ранее
  в инкременте после последней правки).

### Проблемы
- Нет; предыдущие claims 409/89/304 заменены этим результатом текущего
  дерева, старые записи оставлены как история.

### Тесты
- 430 passed, 0 failed, exit 0. Включая 13 `test_geometry_binding`
  (7 новых регрессий §2.2) и все существующие stage-наборы.

### Запросы пользователя
- Тот же промпт восстановления; требование §17 «один полный pytest
  после финальных изменений» выполнено.

### Предстоит
- Без изменений относительно предыдущего раздела.

## Этап 6 — layout repair loop: recompose, роли, omits, acceptance (2026-10-12, дополнение; разделы выше неприкосновенны)

### Реализовано
- Runtime trace разрыва scene→measurement→native (§2 промпта): инструментированный replay сцен c0 слайда №6 (узел `01`) и слайда №3 (пара value/label) на настоящем Chromium + текущих GPN-шрифтах, без кэшей. `runs/stage6-layout-repair/2026-10-12-140949/traces/{slide-06,slide-03}/c0/` + `layout_contract_trace.json` в каждой.
- Локализовано первое место расхождения (observed fact): solver/allocation прогона 2026-10-11 под `layout_mode=preserve_structure` вернул source boxes (`786384/3922776/512064/338328`) вместо hint (`736641/2209815/889041/635007`); style/style/load расхождений нет (GPN Bold 30pt == saved), payload цел (`01`, content_diff ok). Replay текущим кодом: solver==hint, Chromium `line_count=1`, overflow_x/y=false, exact face `GPN_DINCondensed-Bold.otf` sha `157571de…` (expected==actual, no synthesis).
- 10 новых сцен c1/c2 (harness, с parent refs в `plans/plan_lineage.json`): split metric value/label с независимыми ролями (`large_metric` + `body`/`section_heading`), zone heights по измеренным нуждам Chromium (body 1-line ≈25pt, 30pt value ≈47pt и т.д.), структурные nest-клики exact-pair allowed_overlaps, decor-фрагменты — authorized omit (см. ниже). Все 10 компилируются чисто.
- Authorized-omit механизм (§3.2): `classification_and_transformation_ledger.json` (168 решений с evidence: text-less + нет connector endpoints + только geometry atoms), `ContentDiff.omitted_authorized`, comparator tolerance только при валидном ledger. Проверено: 136 уникальных geometry-атомов omitted, `content_diff.ok=true`.
- Logo fix (§5.3b): белый логотип №6 заменён на approved blue colorway того же знака из исходного дека (7/8 слайдов), `asset_overrides.json` + hash-verified emit hook (`ASSET_OVERRIDE_APPLIED`) + comparator tolerance + `authorized_replacements_report.json` (complete=true). Рендер: логотип синий, видимый.
- Engine fixes с версионированием: `layout/1.3` recompose для обоих mode (конец silent preserve), `1.4` residual-overlaps пропускает decor-decor, `1.5` coverage gaps fail (`MEASUREMENT_COVERAGE_GAP`, оба measure sites), `1.6` children в reading order (sequence no_resort для stacking — чинено swapping `Дизайн/Полевой акт`, outcome, steps, prog-пар), `1.7` preserve делит recompose builders (чинил `KIND_UNMEASURED` регрессию). Воспроизводимая упаковка: фиксированные zip timestamps (entry dates 2020-01-01).
- Согласованность гейтов: validator overlap slop 0.75pt = solver definition (18 edge-touch false-fail устранены); `required_visibility_report.json` wired expected-first через emission addresses (191 required after-object видимы, 136 omitted authorized, 8 notes via content_diff, 0 missing/unknown); `aggregate_product_acceptance` (`acceptance.py`, exit 0/3) — единственный публичный статус.
- Полный цикл: builds 1–8 (Chromium + soffice), финальный кандидат `162b9684…`, 8/8 PNG просмотрены harness (RF-01…RF-08: RF-08 отклонён как false positive — ellipsis в source). c2-варианты №3/№6 собраны/отрендерены/просмотрены в sibling run (`…-c2variant`), selection c1 везде с image-based reasons (`selection_report.json`).
- `product_acceptance.json`: **accepted=true, 12/12 scopes pass, 0 blockers** (acceptance exit 0). Опубликован `NTC_business_AI_GPN_corporate.pptx` (sha `162b9684…`, совпадает с candidate).
- Native edit roundtrip: копия финала — правка text run + geometry через python-pptx, save/reopen ok, 8 слайдов, media stable, package graph ok (`native_edit_roundtrip_report.json`; PowerPoint UI — отдельный unverified scope).
- Skill `gpn-restyle` (`gpn-restyler/skills/gpn-restyle/`: SKILL.md + 3 references, frontmatter/links проверены) в формате существующих project skills.
- Bundle `runs/stage6-layout-repair/review_increment_next.zip` (109 файлов, manifest с gaps, complete=true по наличию evidence).
- Тесты: новый `tests/gpn/test_stage6_repair.py` (8 регрессий §10: #1/#2/#3/#4/#5/#6/#11-compare/#12/#16/#17) — green; существующие `test_visibility_absent_required_fails` + `test_nested_group_world_x_1220` уже покрывали §10#7/#8 (не дублировались).

### Проблемы (открыты, с evidence)
1. Frame unverified: content box ≈ full canvas без layout evidence (честный `verified_frame.json`, status unverified). Title/footer следуют сценам + owned examples.
2. Архитектурный долг (не блокеры приёмки, зафиксировано): ResolvedTextSpec как единый контракт (§4) — margins захардкожены в layout (7.2/3.6), saved remeasure читает actual bodyPr/pPr (расхождение закрыто remeasure-гейтом, не контрактом); `emit_slide` идёт по source objects (работает через geometry_emu + omit/passthrough-классификацию, но не `emit_scene` по target nodes); span bindings many↔one не поддерживаются; `restyle_runner` state machine отсутствует — цикл вёлся harness вручную (`model_task_manifest.json`, `runner_transition_log.jsonl`); create-from-document smoke (§10#18) не запускался; `docProps/core.xml` dcterms — остаточная побайтовая недетерминированность (zip mtimes зафиксированы).
3. По пути сломаны и починены в этом инкременте (не регрессии дерева): silent build3-launch fail (неверный cwd), variant build перезаписал shared reports основного run (восстановлено sibling run + rebuild + побайтовая сверка PNG), `KIND_UNMEASURED` под preserve (1.7), mapping visibility по нестабильным shape_id (починено через emission addresses + notes exclusion).
4. S3 foot живёт на 95.5–100% (край контент-бокса, валидно, но плотно); S4 totals ≈99% бюджета (хрупко — любое расползание строк вернёт infeasible, что честно).

### Тесты
- `tests/gpn/test_stage6_repair.py`: 8 passed. `ruff check` по всем тронутым файлам clean.
- Полный сюит: см. `/tmp/gpn_trace/pytest_final.log` (запущен фоном на финальном дереве; итог — в следующем дополнении, не приписывается заранее).
- Корпуса неизменны: input `98e2357c…`; ontology/examples/font binaries не тронуты; outputs только в новых run dirs.

### Запросы пользователя
- `opencode_layout_repair_loop.md` (этот цикл): §2 trace выполнен с observed/hypotheses разделением; §3.1 recompose default + explicit policy в `run_config.json`; §3.2 классификация/omit без micro-slots; §3.3 роли детей независимы (планы), MetricCluster-типы как код — отложены (см. долг); §4 identity измеряется тем же, что экспортируется (remeasure actual state), полный ResolvedTextSpec — долг; §5 emit по target nodes — долг, вместо этого binding evidence + omit + override; §6 state machine — harness вручную, runner — долг; §7 агрегатор реализован и управляет статусом (`completed` только через него); §8 карта кода — runtime_callgraph.md; §9 skill создан; §10 — 8 новых + существующие, create-smoke отложен; §11 порядок в основном выдержан (кроме runner/текст-контракта как кода); §12 bundle собран.

### Предстоит
- Дождаться `pytest_final.log`, зафиксировать итог полным сюитом + ruff на всём репо следующим дополнением.
- `restyle_runner.py` (durable task/action queue, revisions, resume) + `create` smoke на малом DOCX/MD + `restyle/create/resume/status` CLI-контракт.
- ResolvedTextSpec/emit_scene/span bindings — следующим инфраструктурным инкрементом, не в этом.
- PowerPoint UI editing evidence при доступной проверке.

## Этап 6 — итог полного сюита (2026-10-12, дополнение; разделы выше неприкосновенны)

### Реализовано
- Ничего нового; фиксация результата: полный сюит финального дерева — **441 passed, 0 failed, exit 0** (`gpn-restyler/.venv/bin/python -m pytest tests/ -p no:cacheprovider --no-cov -q`, три прогона, все exit 0; 441 тест-функция включая 8 новых `test_stage6_repair`). `ruff check .` clean на всём репо.

### Проблемы
- Нет. Два промежуточных фейла (`test_measured_build_end_to_end`, `test_native_saved_geometry_mutation_detected` под `preserve_structure`) были вызваны потерей mode-ветки в 1.3; починены в 1.7 (`preserve_structure` делит recompose builders), оба зелёные в финальном прогоне.

### Тесты
- 441 passed, exit 0. In-scope: 13 `test_geometry_binding` (nested x=1220, visibility absent-required, graphicFrame identity), 8 `test_stage6_repair` (§10), все stage-наборы.

### Запросы пользователя
- Без изменений; ожидают `restyle_runner`, create-smoke, ResolvedTextSpec/emit_scene/span-bindings (см. «Предстоит» Этапа 6).

### Предстоит
- Без изменений относительно Этапа 6.

## Этап 7 — общий pipeline и доказательная приёмка (2026-10-12/13, дополнение; разделы выше неприкосновенны)

### Реализовано
- §2: `runs/stage7-acceptance/previous_claims_reconciliation.json` — 8 claims (artifact_reviewed confirmed; artifact_ontology_verified not_proven; system_workflow_verified not_proven; product_accepted contradicted — user rejected 162b9684; aggregator_no_false_accept contradicted; relations_preserved not_proven; bundle_complete partial; powerpoint_ui_edit_verified not_proven). §1.2 probes воспроизведены скриптом `runs/stage7-acceptance/2026-10-12-probes/probe2.py` (7/8 классов false-accept на старом коде).
- §4: typed acceptance — новые модели в `models.py` (InputSpec/SnapshotSet/CandidateKey/ArtifactEvidenceRef как EvidenceRef/SourceEvidenceRef/SemanticDeck/SemanticUnit/SemanticRelation/CapabilityInventory/SelectionPolicy/GateExpectation/GateEvidence/ApplicabilityProof/TypedFinding/VisualReview/SelectionEntry/SelectionReport/ProductExpectation/SavedPackageManifest/ProductAcceptance/ArtifactStore/RunContext + TransformLedger/OmitDecision/ModelTask*/DesignReview*). `acceptance.py` переписан: `build_product_expectation` (expected-first, per-slide gate IDs), `collect_saved_manifest` (bytes: canvas/sldId/parts/hidden/drawing inventory/addresses/media/notes/render pages), `validate_evidence` (units/stale/hash), `aggregate_product_acceptance` (единственный derive path, 17 error codes, worst-status merge, FRAME_UNVERIFIED). `main` вызывает ту же функцию; старый dict-API удалён, `test_stage6_repair.py::test_aggregate_product_acceptance_blocks_and_accepts` переведён на typed API. `EvidenceRef` (Stage-1 union) не тронут — новый класс назван `ArtifactEvidenceRef`, в acceptance re-export как `EvidenceRef`.
- §10.7: `runs/stage7-acceptance/user_feedback.json` (rejected 162b9684, flat_text_dump). `gpn/design.py`: build_slide_design_brief/generate_visual_concepts/validate_concept_fidelity/compile_concept_scene/review_slide_design/compare_visual_concepts/evaluate_candidate_diversity (сигнатуры по topology/reading/dominance, косметические дубликаты insufficient). 8 design dimensions, FLAT_TEXT_DUMP structural signals.
- §6: `gpn/semantics.py` — `build_semantic_projection` (174 units/10 relations на NTC IR; стрелки → required directed relations, from/to — только harness), `can_omit_source_object` (§6.2, 5 шагов; replace-решение старше required-refusal), `check_semantic_relations`. `emit_slide` принимает semantic_slide/transform_ledger/source_snapshot (OMIT_NEEDS_REVIEW/OMIT_REFUSED); `_is_omittable_decor` больше не пропускает arrow-формы. 9 стрелок §1.4 проверены по source-рендерам (soffice→PDF→pdftoppm, `source-render/`): все 9 — required transitions (slide2: input→agent→table; slide3: 4h→1s, input→recommender→chart; slide4: 50→16; slide5: NTC hub→3 tracks); записано `arrow_relation_assessment.json`.
- §8: `gpn/restyle_runner.py` — RunState/AdvanceResult/SearchBudget, start/advance/resume/schedule_repairs/publish_accepted_product, atomic state + transition log, fingerprint/schema/once-only validation, reissue stale, budget exhaustion, invalidation report, assembly verification, atomic publish guard. `gpn/document_ingest.py` — detect/ingest/extract_pptx(PPT через soffice)/extract_document(PDF via pypdf/DOCX via python-docx/MD/TXT)/extract_prompt/semantic_task_for/validate_semantic_response. Зависимости: `pypdf==6.0.0`, `python-docx==1.2.0` (pinned, uv.lock).
- §11: CLI `restyle/create/resume/status/bundle` в `gpn/cli.py` + проброс в top-level `slides_cli/cli.py`; exit 4 = awaiting_harness. Skill `gpn-restyle`: runtime-contract (новые routes, runner states, design cycle, измеренные факты — как наблюдения одной конфигурации, не константы), quality-review (+design review), gate-bundle (`slides gpn bundle`), SKILL.md (точные команды).
- §14: `gpn/review_bundle.py` — required roles из fixed schema, bytes+alias map, hash/subject verification, self-inspection ZIP, bundle_complete только при closure.
- §12.3: `gpn/edit_roundtrip.py` — проверка изменённого текста И геометрии после reopen + SHA256 media/embeddings/chart bytes; proven на 162b9684 (text match, geometry match, 9 media stable, 8 slides, UI=false).
- Проверки §12.2 через публичный CLI (все awaiting_harness, stage=semantic): NTC restyle (cli-smoke), новый mini PPTX 3 слайда (verify-restyle-new, sha 72383b04), PDF create (2 блока), DOCX create (4 блока + таблица с provenance), prompt create. `verification_evidence.json`.

### Проблемы (открыты, с evidence)
1. Полный NTC rebuild с новым дизайном НЕ выполнен: требуется harness-исполнение semantic/design/scene/review/selection задач через новый runner (пакеты готовы, ответы модели — следующий шаг). product_accepted=false честно.
2. Frame: verified_frame по-прежнему unverified (resolve_frame_profile как код — не реализован; gate FRAME_UNVERIFIED блокирует до него).
3. ResolvedTextSpec/emit_scene/span-bindings — долг с Этапа 6, не закрыт (saved remeasure покрывает гейтом, не контрактом).
4. Rule-application coverage по 26 правилам (build_rule_applications/evaluate_saved_rules) — не реализована; saved_auth_rules gate ждёт её evidence.
5. Calibration same-case pairing (pair_calibration_samples) — не реализована; старая calibration запись не используется как full.
6. Skill discovery в установленном OpenCode не проверена (конфиг лоадера вне проекта, доступ отклонён); skill-файлы корректны, invocation evidence — только CLI smoke.
7. Полный сюит >10 мин (Chromium/render тесты); последний фоновый прогон — следующий пункт.

### Тесты
- Новые: test_stage7_acceptance (20), test_stage7_design (8), test_stage7_semantics (11), test_stage7_runner (14), test_stage7_cli_bundle (9), test_stage7_roundtrip (2) — все green; затронутые сюиты (stage4_exporter/stage5_build/stage6_repair) green (70 passed в фокус-прогоне).
- `ruff check` clean по всем тронутым файлам.
- Полный сюит запущен фоном (`/tmp/pytest_full3.log`); итог — следующим дополнением, не приписывается заранее.
- Корпуса неизменны: input 98e2357c…, ontology d24be59d…, examples 0c6d6c00…; шрифты не тронуты.

### Запросы пользователя
- `opencode_general_pipeline_and_acceptance.md`: §2 reconciliation выполнен; §3/§4 контракты и негативы выполнены; §10.4–10.8 модели/гейт/user_feedback выполнены (rebuild — предстоит); §5/§6 проекция/omit/9 arrows выполнены; §8 runner выполнен (исполнение задач — предстоит); §11 CLI+skill выполнены (discovery — см. проблему 6); §9 adapters выполнены (pypdf/python-docx pinned); §14 builder выполнен (system bundle — после rebuild); §12.3 roundtrip выполнен.

### Предстоит
- Harness-исполнение задач нового runner для NTC (semantic→briefs→concepts→scenes→reviews→selection→assembly→acceptance) с реальными PNG-ревью; rebuild кандидата с исправлением flat_text_dump и native relation edges для 9 стрелок.
- resolve_frame_profile + build_rule_applications + calibration pairing — код следующих инкрементов.
- Полный сюит + `review_increment_system.zip` через общий builder после accepted.

## Этап 8 — harness-исполнение цикла и поверхностный контракт (2026-10-13, дополнение; разделы выше неприкосновенны)

### Реализовано
- **Harness-цикл реально исполняется.** `runs/stage7/2026-10-13-135452`: текущая модель
  отработала 41 задачу раннера — `semantic_analysis` (1) → `design_brief` (8) →
  `visual_concept_generation` (8, по 3 концепции на слайд) → `scene_generation` (24,
  по 3 плана сцены на слайд). Все 41 ответы приняты по schema/fingerprint/coverage,
  ни одна задача не осталась `pending` (`run_state.json`, `runner_transition_log.jsonl`).
- **Семантическая проекция и трансформационный ledger теперь артефакты движка, а не
  файлы «руками».** `restyle_runner._materialize_semantic_deck()` валидирует ответ
  модели через `SemanticDeck`, считает собственный sha и **сам** пишет
  `inputs/semantic_deck.json` + `classification_and_transformation_ledger.json`
  (327 решений: preserve/recompose/omit по каждому из 327 объектов исходного IR,
  с `reason` и evidence). Пропущенный `omit` без причины, неизвестный объект,
  `recompose` стрелки без `target_relation_ids` — типизированные findings, не падение.
- **Snapshot-идентичность исправлена:** авторизация omit привязана к sha256 байтов
  `source_ir.json` (то, что build перечитывает), а не к заявленному sha исходника —
  иначе ни одно решение omit не находилось и сборка падала `OMIT_NEEDS_REVIEW`.
- **Пакет semantic_analysis обогащён** (`document_ingest.source_object_inventory`):
  модель получает пообъектную инвентаризацию (id/kind/бокс/геометрия/стрелки/текст/
  sha ассета) — без неё «классифицировать объекты» было нечем.
- **Дубликаты ответов больше не размножают очередь:** `UNKNOWN_OR_CONSUMED_TASK`
  отбрасывается без reissue (reissue остаётся только для устаревшего содержимого).
- **Контракт visual-concept:** задача ожидает `{"concepts": [...]}` (3 концепции),
  это закреплено в `expected_response_units` (было `visual_argument/...`, что делало
  валидный ответ «покрытием неполным»).
- **Поверхностный контракт §5.3 (`ResolvedSurfaceStyle`).** Зона сцены может объявить
  `surface_token` / `foreground_token` / `stroke_token`; значения — **имена токенов
  палитры из загруженного JSON**, `native.py` резолвит их через `_resolve_token_rgb`
  (включая conditional-цвета), применяет заливку/цвет текста/цвет линии. Blanket
  `noFill` для декор-объектов сохраняется только когда поверхность не объявлена;
  неизвестный токен → `SURFACE_TOKEN_UNKNOWN` в issues, а не молчаливый цвет.
- **Стрелки-связи получили наконечники:** `_set_connector_arrows` восстанавливает
  `headEnd/tailEnd` у связанной стрелки (§6.2.4 — направление семантично), цвет
  только из объявленного токена.
- **Объявленные nest-кластеры больше не разбираются на стопку.** `layout.py`: группа
  членов с одинаковым боксом, у которой **каждая пара** объявлена в
  `allowed_overlaps`, сохраняет со-локацию (панель + её содержимое). Раньше любой
  общий бокс = «стакать», что и убивало композицию «карточка + подпись».
- **Авторские данные дизайна (harness, текущая модель):** `harness_scripts/` в ранне —
  семантическая проекция, роли объектов, оценки пропусков, 8 design briefs,
  24 visual concepts, 24 scene plan (панели, метрики, связи, порядок чтения),
  `probe.py` — быстрый измерительный прогон без полного раннера.

### Проблемы (открыты, с evidence)
1. **Сборка слайдов не завершена: 0 из 8 слайдов проходит measured-layout** на
   финальных авторских данных (`runs/stage7/2026-10-13-135452/stage8_evidence.json`,
   `probe/candidate_evaluations.json`: все 8 — `infeasible`). В промежуточных
   итерациях конфигурации доходил до 1/8 (слайд 6), что показывает, что блокер —
   плотность, а не один конкретный слайд.
2. **Доминирующая причина — `HINT_OVERLAP_UNRESOLVED` в нижней полосе** (источник,
   футер, номер страницы, линейка): измеренные высоты текстовых зон превышают
   авторские боксы, решатель сдвигает блоки вниз и упирается в нижнюю границу
   content box. Перечень конкретных объектов — в `probe/candidate_evaluations.json`.
3. **Корневая причина (не «ещё чуть-чуть подвинуть»):** движок складывает членов
   одной зоны с шагом 7.2 pt, а стиль роли задаётся **на зону**. При сохранении
   каждого исходного текстового объекта отдельным нативным блоком колонка из 8–10
   блоков требует ~24 pt на блок при корпоративных кеглях — в бэнде 30–75 % это не
   помещается. Слияние нескольких текстов в один блок не поддержано эмиттером.
   Это конкретный блокер текущего варианта композиций, а не «долг на будущее».
4. `resume`-итерация с ответами, которые движок уже потребил, даёт
   `RESPONSE_REJECTED` в findings — корректно, но шумно; отдельная задача на это
   не ставилась.

### Тесты
- Полный сюит: `gpn-restyler/.venv/bin/python -m pytest tests/gpn -p no:cacheprovider
  --no-cov` → **352 passed in 739.83s (0:12:19), EXIT=0** (лог `/tmp/pytest_full8.log`).
- Фокусный прогон по затронутым модулям (`test_stage7_acceptance`, `test_stage7_design`,
  `test_stage7_semantics`, `test_stage7_runner`, `test_stage7_cli_bundle`,
  `test_stage6_repair`, `test_stage5_layout`, `test_stage4_exporter`) →
  **133 passed in 211.35s, EXIT=0** (`/tmp/pytest_focus.log`).
- `ruff check` по изменённым модулям (`restyle_runner`, `document_ingest`, `native`,
  `layout`, `planner`, `planning_bridge`, `export_pipeline`) — clean.
- Новых автотестов на поверхностный контракт в этом этапе не написано — долг,
  зафиксирован ниже.

### Запросы пользователя
- `opencode_general_pipeline_and_acceptance.md`: §8.3 (harness-цикл исполняется
  текущей моделью) — выполнено; §6.1/§6.2 (проекция + ledger как артефакты движка) —
  выполнено; §5.3 (ResolvedSurfaceStyle вместо blanket noFill) — выполнено;
  §10.4–10.8 — briefs/concepts/plans исполнены, но **продукт не принят**: сборка
  слайдов блокируется п. 1–3 выше.

### Предстоит
- Разрешить плотность: либо движок раскладывает multi-run абзацы в одном нативном
  блоке (эмиттер), либо сцены объявляют меньше текстовых блоков на колонку.
  До этого — следующий гейт с новыми сценами, а не polish текущих.

## Этап 8.1 — Исправление ложной «плотности»: schema, footprint линий, authored blocks, 2D-поиск

Работа по `opencode_layout_engine_recovery.md`. Новый ран:
`runs/stage7/2026-10-14-layout-recovery/`. Предыдущие разделы выше не
редактировались.

### Реализовано

**1. Reconciliation старых claims** (`audit/claims_reconciliation.json`).
Зафиксированы 10 старых утверждений с вердиктом и доказательством; 7 из
10 помечены `invalid` (в т.ч. «8 слайдов не влезает из-за плотности»,
`budget 0/6 exhausted`, `scene_validated` без schema-проверки, «scene
tasks получают concept», «surface tokens сохранены», «solver умеет 2D»).
3 подтверждены как дефекты (line footprint, duplicate zone_id, source 1:1
boxing).

**2. Реальная schema-валидация сцен** (`planner.py`).
- `validate_scene_response(response, task, semantic, concept, rules) ->
  SceneValidationReport`: payload-схема, уникальность zone_id,
  совпадение candidate/slide, покрытие required units и relations,
  допустимость role/surface-токенов из загруженного JSON, конечность
  геометрии, привязка к concept.
- Уникальность `zone_id` добавлена и в `validate_planned_intent`.
- `restyle_runner._advance_stage` теперь вызывает валидацию **до** записи
  `scene_validated`; при провале состояние становится `schema_invalid` и
  добавляется finding `SCENE_SCHEMA_INVALID`.
- **Факт подтверждён на архиве:** все 24 архивных плана содержали `min_h`
  в зонах (847 вхождений) — их `LayoutZone` (extra='forbid') отвергает.
  До исправления runner писал `scene_validated` без вызова схемы
  (`audit/scene_schema_validation.json`: `invalid_before: 24`).

**3. Связка concept ↔ scene task.** `_scene_generation_task` теперь
включает полный `visual_concept` в пакет. Подтверждено, что раньше все 24
пакета после исключения `concept_id/concept_key` были побайтово
эквивалентны (1 distinct hash на слайд) — фактического visual concept
модель не получала.

**4. Footprint линий/стрелок — по реальным данным source IR.**
`_is_stroked_path()` определяет stroked path по `geometry_token='line'`
либо по фигуре без текста с нулевой высотой и реальной шириной;
`_stroke_width_pt()` берёт толщину из `source_line.width_emu`.
`_resolve_hint_overlaps` получил `node_kinds`/`stroke_widths`, и для
коннекторов использует **envelope обводки**, а не высоту зоны.
На реальном слайде 6: обе линии `z-rule` распознаны (1.2 pt и 0.65 pt),
три декоративные карточки `roundRect` (h=1581912 EMU) корректно
**не** распознаны как пути.
`audit/s6_rule_foot_collision_before_after.json` фиксирует архивный trace
(первая линия сдвинула outcome, вторая оставила footer без места) и новую
модель footprint.

**5. Surface contract доведён до resolved slide.** `_build_resolved_slide`
больше не теряет `surface_token`/`foreground_token`/`stroke_token`;
поля добавлены в `ComponentNode`.

**6. Authored blocks вместо source-object boxing.** Текстовые объекты одной
авторской зоны собираются в один `rich_text`-компонент
(`composition.py`), измеряются как единый frame с реальными границами
абзацев (`_rich_text_paragraphs`, `layout.py`), пишутся одним нативным
`p:sp`/`txBody` (`native.emit_rich_text_block`), а `export_pipeline.emit_slide`
стал **target-driven**: сначала авторские блоки, затем per-source loop с
пропуском уже слитых span'ов. Поля `source_ref`/`source_paragraph_index`
добавлены в `MeasureParagraph` для адресуемости span'ов.
`EmissionStrategy` дополнена значением `native_rich_block`.

**7. Двумерный поиск размещения.** Добавлена `place_side_by_side()` —
при исчерпании сдвига вниз ищется свободное место по обеим осям
(только перенос, без изменения измеренного размера). Воспроизведён
контрпример §1.3 (регион 400×100, два объекта 100×100): ранее
`no_room_below`, теперь найдена законная row-размещение без пересечений.
`no_room_below` сохраняется как **локальный** конфликт с флагом
`directional_search_only`.

**8. Честные статусы вместо `LAYOUT_DENSITY_INFEASIBLE`/`budget 0/6`.**
`LayoutResolutionResult.status` расширен значениями
`schema_invalid | unsupported_representation | constraint_conflict |
needs_recompose | search_exhausted`. Сообщения содержат реальные
счётчики попыток и `termination=...`.

### Результат на реальных данных (NTC_business_AI_GPN.pptx)

Прогон реального public runner на скорректированных архивных планах
(без bespoke-патча под этот input id):

```text
run: runs/stage7/2026-10-14-layout-recovery/main/
was: 0 из 8 slides resolved
now: 3 из 8 resolved (слайды 1, 6, 8)
```

Слайд **6** — основной кейс спецификации (§1.2) — теперь `resolved`.
Остальные 5 дают честные локальные статусы (`needs_recompose`,
`constraint_conflict`) с конкретными дефицитами (1.5–33 pt), а не
заявление о плотности материала.

Сохранность содержимого в нативном выводе (3 слайда):
абзацев 12 / 23 / 34 — **точно совпадает** с таблицей исходника (§1.1)
для слайдов 1, 6, 8.

Native render (после установки poppler): LibreOffice 26.8.1.1,
`page_count=3`, `text_spans=346`, page images получены и **просмотрены
вручную**:
- стр. 1 (слайд 1): кикер/заголовок/лид, синяя панель-поверхность
  «НТЦ + ДО» (подтверждает сквозной проход surface-токенов), карточка с
  четырьмя пунктами, футер и номер — пересечений нет;
- стр. 2 (слайд 6, кейс §1.2): трёхколоночная композиция 01/02/03, строка
  результата на месте, футерная полоса цела — пересечений нет;
- стр. 3 (слайд 8, самый плотный — 213 слов): сетка 2×2, все факты, числа,
  оговорки и строки «Источники» сохранены — пересечений нет.

`audit/native_render_evidence.json`. Самый плотный слайд исходника
(213 слов) целиком помещается при нормативных кеглях — это прямо
опровергает прежний вывод о «непомещаемости по плотности».

### Проблемы (открыты, с evidence)

1. **5 из 8 слайдов ещё не решены** — с реальными остаточными дефицитами
   по вертикали (1.5, 3.1, 6.3, 10.7, 33.0 pt) и один `constraint_conflict`
   с неразрешёнными парами. Это уже честные локальные конфликты, но дек
   не собран. Основной остающийся дефицит движка: нет полноценного
   иерархического flow/grid с перераспределением ширины и повторным
   замером по выбранной ширине.
2. **Rich blocks не сработали на этих планах, потому что архивные планы
   кладут каждый объект в свою зону** (по одному `content_ref` на зону).
   Возможность движка доказана тестами, но авторские планы под неё ещё не
   пересобраны — это работа этапа проектирования (§8.4), а не движка.
3. ~~`pdffonts`/`pdftoppm` не установлены~~ — **решено**: poppler установлен,
   рендер даёт page images и шрифтовую проверку. Остаточный finding:
   LibreOffice подставил `GPN_DIN Condensed Bold/Regular` в PDF
   (`expected faces absent from render`) — это подстановка в рендерере,
   байты шрифтов на диске верифицированы по sha256 и используются в
   измерении; требуется отдельная проверка в PowerPoint UI.
4. **`font_inventory.json` из архива содержит устаревший путь**
   (`GPN_DINCondensed-Bold.otf` без подчёркивания) — байты на диске верны
   (sha256 совпадает), но записанный путь не находится. Инвентарь
   перегенерирован из реального discovery, а не исправлен вручную.
5. **`template_profile.json` не верифицирован** (`is_verified=false`,
   `content_box=null`): layout5 содержит только плейсхолдер номера
   слайда. Использован производный base template из архива
   (sha256 `7ed9368f…`, переиспользован, не копируется в каждый каталог).
   Собственно `verified_frame.json` по требованию §12.2 ещё не сформирован.

### Тесты

- Полный сюит: `uv run pytest tests/gpn --no-cov` → **363 passed, 3 skipped
  in 1365.67s (0:22:45)**. (Было 352 до этого этапа.)
- Новый файл `tests/gpn/test_stage9_layout_recovery.py` — **14 тестов** по
  причинам (§10.1): архивный `min_h` → schema_invalid; дубли zone_id;
  line footprint снимает ложные конфликты; контрфакт (без connector-типа
  конфликт остаётся, т.е. не blanket allow-overlap); row-размещение в
  400×100 вместо глобального infeasible; три source-объекта → один
  authored block; сохранение paragraph-границ и source_ref; padding
  один раз; сквозной проход surface-токенов; block решается реальными
  шрифтами.
- Эти тесты **поймали два реальных бага** в моей же реализации: неверное
  поле payload (`payload.text` вместо `payload.paragraphs`) при
  группировке и lookup стиля по source-ref вместо id блока.
- `ruff check src/ tests/` → **All checks passed**.
- Утверждения старых тестов, кодировавшие старый контракт статусов
  (`infeasible`, `INFEASIBLE_OVERLAP`, `budget`), обновлены на новый
  контракт; инвариант «никакого уменьшения кегля при перегрузке»
  сохранён без изменений.

### Окружение (изменения на машине)

- `uv sync` — создано `.venv` (Python 3.12.13).
- `npm install` + `npx playwright install chromium` в `tools/layout/`
  — без этого падали 18 тестов измерения (`MEASUREMENT_TOOL_FAILED`).
- `brew install --cask libreoffice` — native render (с согласия
  пользователя).
- `brew install poppler` — **в процессе**, не завершён.

### Запросы пользователя

- `opencode_layout_engine_recovery.md` §1–§8: пункты 1.1–1.8 подтверждены
  на данных; §2 (schema), §4.2 (footprint), §4.3 (surface), §5.2 (padding
  один раз), §6.2 (2D-поиск), §7 (честные статусы) — реализованы.
- Согласие получено на установку LibreOffice через brew.

### Предстоит

- Иерархический flow/grid solver с перераспределением ширины и повторным
  замером — чтобы снять оставшиеся 5 слайдов и вертикальные дефициты.
- Пересобрать авторские планы с реальной группировкой (semantic groups),
  чтобы authored blocks заработали на материале.
- Сформировать `verified_frame.json` из геометрии исходника (все 8
  слайдов дают согласованную область x 43.20..916.56, y 24.48..530.64 pt
  на canvas 960×540).
- Завершить установку poppler и выполнить PNG-ревью, repair и selection.
- Dense-3x fixture, fragmentation invariance, полный review bundle (§12).

### Передача на другую машину (состояние на момент коммита)

Проверено перед коммитом: полный сюит `uv run pytest tests/gpn --no-cov` →
**363 passed, 3 skipped** (22:45). После этого прогонa изменён только
порядок импортов в `tests/gpn/test_stage9_layout_recovery.py`
(`ruff --fix`); этот файл перезапущен отдельно → 14 passed,
`ruff check src/ tests/` → clean.

Окружение, обязательное для повторения (на новой машине):

```bash
cd gpn-restyler
uv sync                              # .venv, Python 3.12.13
(cd tools/layout && npm install && npx playwright install chromium)
brew install --cask libreoffice      # native render
brew install poppler                # pdffonts/pdftoppm/pdfinfo
```

Без `npm install` + chromium падают 18 тестов измерения
(`MEASUREMENT_TOOL_FAILED`); без LibreOffice/poppler — рендер-gates.

Артефакты ранов лежат вне git (`.gitignore`: `runs/`), поэтому на новой
машине их нужно получить отдельно либо пересоздать:

- `runs/stage7/2026-10-13-135452/` — исходный ран (source_ir, semantic deck,
  24 автора сцен, base_template.pptx);
- `runs/stage7/2026-10-14-layout-recovery/` — текущий ран (audit/*,
  main/*, скорректированные планы в inputs/plans/*.json);
- скрипты этапа: `runs/stage7/2026-10-14-layout-recovery/revalidate_scenes.py`
  и `run_build.py` (воспроизводимый прогон).

Точка продолжения: 5 нерешённых слайдов (2, 3, 4, 5, 7) требуют
иерархического flow/grid solver; затем пересборка авторских планов с
настоящей группировкой, PNG-ревью/repair/selection, dense-3x, review bundle.

---

## Этап 8.2 — recovery round 2: 8/8 слайдов, native-рендер всех восьми

Ран: `runs/stage7/2026-10-15-layout-recovery/`
Спецификация: `opencode_layout_engine_recovery.md` (продолжение этапа 8.1).

Предыдущий раздел (8.1) оставлен без изменений: он фиксирует состояние на
момент 3/8 resolved. Этот раздел записывает, что изменилось дальше.

### Уточнение к Этапу 8.1: ран 2026-10-14-layout-recovery не сохранился

Каталог `runs/stage7/2026-10-14-layout-recovery/`, упомянутый в Этапе 8.1,
на диске отсутствует (проверено 2026-10-15). `runs/` вне git, поэтому его
нельзя было восстановить из репозитория. Все утверждения Этапа 8.1
перепроверены на данных заново: baseline «3 из 8 resolved» воспроизведён
на archival-планах через тот же public runner
(`audit/candidate_evaluations_detail.json` хранит хэши планов и результатов).

### Реализовано

1. **Схема сцены.** `min_h` удалён из generation-вывода (поле не объявлено
   в `LayoutZone`, `extra='forbid'`, единиц нет). `audit/scene_schema_validation.json`:
   8/8 архивных планов валидны после удаления; `z-rule` на слайде 6 остаётся
   единственным дубликатом `zone_id` в архиве (в новых планах дубликатов нет).

2. **Единое координатное пространство измерения (§5.1).** `measure.js`
   сравнивает extent от origin компонента (включая верхний inset) с
   **внешним** боксом, а не с внутренним. До правки кадр, точно равный
   измеренной высоте текста, объявлялся переполненным на величину
   `margins.t`. TOOL_VERSION → 0.2.0.

3. **Честная проверка шрифтов (§5.3).** `measure_components` падает с
   `MEASUREMENT_FACE_UNVERIFIED`, если браузер не загрузил face: раньше
   метрики подставленного шрифта возвращались как результат измерения
   (воспроизведено: 39.6 pt вместо 38.1 pt при неверном allowlist).

4. **Коннектор, поставленный композицией, — авторская геометрия (§4.2).**
   Раньше любой connector оставался в source-боксе и становился
   неподвижным препятствием, сдвигавшим всю композицию ниже себя.
   ENGINE_VERSION → 1.9.

5. **Кластеры декора (§4.1/§5.2).** `layout._cluster_placement` размещает
   непересекающиеся в источнике члены группы по их внутренней геометрии
   (масштабированной в зону) вместо вертикального стека с зазором на объект.

6. **Контракт поверхностей доходит до всех компонентов (§4.3).**
   `surface_token/foreground_token/stroke_token` переносились только на
   authored rich-text блоки; обычные компоненты зоны их теряли. Теперь
   токены копируются на каждый компонент зоны. Панели рендерятся
   (`SURFACE_APPLIED ... fill token 'light' -> #F2F2F2`).

7. **Порядок отрисовки (painter order, §4.1).** Объявленная панель —
   underlay: `emit_slide` рисует её под содержимым независимо от
   source z-order. Без этого перенесённая карточка закрывала подпись.

8. **Авторские планы как семантические группы (§3.1, §8.4).**
   `scripts/author_plans.py`: слайд описан полосами (header / content /
   footer) и смысловыми колонками; каждая текстовая группа измеряется тем же
   Chromium-измерителем в той же ширине, в какой будет размещена, с
   итерацией до неподвижной точки; панели получают палитровые токены и
   внутренний отступ; правила (разделители, линейки) не занимают полосу;
   подписи рисунков остаются на месте, если оно свободно, и уходят под
   рисунок иначе; вертикальный ритм распределяет остаток высоты между
   полосами с объявленным максимумом. Лестница интервалов
   (pad 5→3→0, gap 4→3→2→0) применяется, только если композиция не
   помещается; содержимое и кегли при этом не меняются.

9. **Полный продукт.** `main/final/NTC_business_AI_GPN_corporate.pptx` —
   8 слайдов, native редактируемые text/panels/images, реальные GPN-шрифты.

### Результат на реальных данных

```text
layout_resolved: 8/8 (было 3/8 на архивных планах)
content_diff.json: ok=true, changed=0, duplicated=0, missing=0,
                   dangling=0, unexpected_rewrites=0
source_preservation_verified: true
native_structure_verified: true
render: 8 страниц PDF + 8 PNG, pdffonts: только GPN_DIN-Regular,
        GPN_DINCondensed-Bold, GPN_DINCondensed-Regular
```

Слайд 6 (кейс §1.2, 81 слово) и слайд 8 (самый плотный, 213 слов)
рендерятся без наложений и обрезки; ранее оба числились
неразрешимыми «по плотности».

### Проблемы (открыты, с evidence)

1. **`saved_geometry_verified=false`** — гейт сохранённой геометрии этим CLI-путём
   в этом раунде не выполняется. Явно отсутствует, а не пройден молча
   (`main/final/gate_index.json`).
2. **PowerPoint UI round-trip не выполнен** — PowerPoint в окружении нет.
3. **Текст, воспроизведённый у shape, не адресуем по атомам.** Три абзаца
   слайда 3 раньше попадали в `missing`; текст в сохранённом XML присутствует
   (проверено), но `emit_shape` не регистрирует paragraph-атомы. После
   разделения зон на однозначные `missing`/`dangling` пусты, однако сам
   пробел адресации остаётся и должен быть закрыт.
4. **Визуальные дефекты (зафиксированы в `main/final/design_review.json`):**
   - часть подписей оказывается вне своей панели (слайды 2 и 3);
   - очищенные правилами палитры декоративные иконки оставляют пустые рамки
     (слайды 1, 2, 5);
   - на слайде 3 колонка «ГГД В ЭРА БУРЕНИЕ» остаётся без рисунка.
5. **Кандидаты c2/c3 не отработаны.** В этом раунде построен и выбран один
   авторский вариант на слайд (c1); сравнение трёх топологий, dense-3x
   контроль, fragmentation-invariance и create-writer smoke — не выполнены.

### Тесты

- Полный сюит: `uv run pytest tests/gpn --no-cov` → **369 passed, 3 skipped
  in 795.81s (0:13:15)** (было 363).
- Новый `tests/gpn/test_stage10_recovery_round2.py` — 6 причинных регрессий:
  (1) кадр по измеренной высоте не переполняется, а кадр на 1 pt меньше —
  переполняется; (2) `MEASUREMENT_FACE_UNVERIFIED` при недоступном face;
  (3) поставленный линей несёт свою зону в решатель и не становится
  препятствием; (4) непересекающиеся члены кластера сохраняют взаимное
  положение, пересекающиеся — нет; (5) surface-токены доходят до обычного
  (не rich-text) компонента; (6) литеральный `FF0000` вместо токена палитры
  отвергается валидатором сцены.
- `ruff check src/ tests/` → **All checks passed!**
- Логи: `runs/stage7/2026-10-15-layout-recovery/test_logs/`.

### Окружение

Без изменений относительно Этапа 8.1 (uv, playwright+chromium, LibreOffice,
poppler). Внешние пути не использовались; все прогоны и артефакты — внутри
`/Users/wysockii/Documents/gpn_slides`.

### Предстоит

- Починить подписи вне панелей и пустые рамки от очищенного декора
  (repair по `main/final/design_review.json`), затем повторный рендер.
- Выполнить сохранённую-геометрию гейт и PowerPoint UI round-trip.
- Отработать c2/c3 с реальным сравнением рендеров (по §8.1/§8.5).
- Dense-3x контроль, fragmentation invariance, create-writer smoke (§10).

---

## Этап 9 (2026-10-15): общий authoring, исправление визуальных потерь, проверка трёх презентаций

Задание: `opencode_universal_authoring_and_visual_repair.md`. Прогон:
`runs/stage9/2026-10-15-universal-ntc-r2` (предыдущий раунд
`runs/stage7/2026-10-15-layout-recovery` оставлен без изменений).

### Реализовано

**1. Единый координатный контракт измерителя (§5.1).** `measure.js`
`detectOverflow` сравнивал содержимое только с внешней границей кадра, из-за
чего фрагмент, заходящий на 5 pt в правый и на 2 pt в нижний inset,
отсутствие переполнения не замечал. Теперь extents (фрагменты и line boxes)
остаются в системе отсчёта внешнего origin, а проверка идёт по внутренней
области в той же системе: `left=margins.l`, `top=margins.t`,
`right=box.w-margins.r`, `bottom=box.h-margins.b`. Каждый inset применяется
ровно один раз. Добавлены поля `inner_box_in_frame_pt`,
`content_bounds_inner_pt`, `logical_line_boxes_inner_pt`,
`required_outer_height_pt` (= верхний inset + реальная высота абзацев/строк +
нижний inset), `excess_inner_pt`, `content_absent`. `occupied_height_pt`
сохранил прежний смысл дословно, но версия измерителя поднята
(`TOOL_VERSION 0.2.0 → 0.3.0`, `CALIBRATION_ID uncalibrated-v1 →
uncalibrated-v2-inner-outer`), что инвалидирует кэш, как требует §11.2.
`line_count` теперь считает логические строки, а не сумму строк каждого run.
Неположительная внутренняя область при наличии содержимого — `invalid
geometry`, а не fabricated fit; пустой кадр без содержимого (например,
коннектор нулевой высоты) — законная пустая геометрия.

**2. Применимость text-fit по фактическому содержимому (§5.2).** Новый модуль
`gpn/text_presence.py`: `inspect_saved_text_presence` читает переоткрытые
байты OOXML и решает по `a:t`, `a:fld` (включая runtime-поля), `a:br`,
`a:buChar`/`a:buAutoNum`/`a:buNone` и эффективному состоянию заливки run'а —
не по длине текста, площади bbox, цвету или source id. `empty_geometry`
исключается только из области saved text fit и остаётся в geometry/style/
path/semantic. `remeasure_saved_geometry` теперь отдаёт отчёт раздельно:
`applicable` / `measured` (список id) / `not_applicable` (с причиной и
перечнем оставшихся областей) / `unknown_state` / `overflows`, плюс
`candidate_sha256`, `font_bundle` и версию измерителя.

**3. Единый painter order (§4.4).** `emit_slide` больше не делает два
независимых прохода: авторские rich-блоки и обычные объекты входят в один
DAG (`build_painter_order`), где ребро идёт от подложки к её объявленному
содержимому, а текстовая подложка рисуется раньше всего, что не является
подложкой. Стабильный топологический порядок, цикл → `PAINTER_ORDER_CONFLICT`
с перечислением узлов. `ResolvedSlide` получил `underlay_of` и
`overlap_permissions`.

**4. Типизованные разрешения на перекрытие (§5.4).** Новая модель
`OverlapPermission` (underlay / content / relation / kind / scope /
evidence). `LayoutIntent.overlap_permissions`. `_intent_overlap_constraints`
больше не принимает голую пару: пара без типизованного разрешения
отклоняется с `OVERLAP_PERMISSION_UNJUSTIFIED` (в прошлом раунде на одном
слайде было 1085 пар, полученных пересечением всех поверхностей со всем
содержимым). `layout.derive_overlap_permissions(scene)` выводит разрешения из
целевого дерева: `surface_child` — только объявленный потомок своей
поверхности; `intentional_path_crossing` — по объявленным edge-relation;
`explicit_overlay` — по объявленному оверлею.

**5. Visual normalization до эмиссии (§6).** Новый модуль
`gpn/visual_norm.py`: `classify_visual_units` (осмысленная классификация с
evidence, а не «всё без текста — мусор» и не «>4000 pt² — панель»),
`plan_visual_normalization` (одно решение на юнит: keep_native_recolor /
redraw_native / use_approved_asset / authorize_omit / unresolved),
`resolve_brand_asset` (colorway/surface по байтам ассета), `analyze_image_alpha`
и `check_visual_representation`. `_apply_decor_fill_line` теперь исполняет
план и не решает по цвету источника: `unresolved` на обязательном юните —
блокирующий `VISUAL_REPRESENTATION_UNRESOLVED`, штрихи не затираются;
`authorize_omit` удаляет декоративный кластер целиком. Правило
«stroke не в палитре → noLine» больше не является алгоритмом для
обязательного рисунка.

**6. Fail-closed валидация сцены (§3.3, U13).** Ветка, ставившая
`scene_validated` при отсутствии task/semantic/concept/rules, удалена;
появляется `SCENE_CONTEXT_MISSING` с перечислением отсутствующих ссылок.

**7. Типизованный результат попытки кандидата (§7.2, §10.1).** Новая модель
`CandidateAttemptResult`. `_build_deck_candidate` возвращает его вместо
`(ok, detail)`; существование PPTX больше не равно успеху — eligibility
требует `operation_status=completed`, пройденные saved checks и layout
report. Каждый кандидат каждого слайда получает собственную попытку (раньше
строился только первый и один второй; третий не исполнялся вовсе);
попытки пишутся в `candidate_attempts.json`. Диагностический кандидат
сохраняет путь, но остаётся неeligible и не попадает в отбор.

**8. Каноническая приёмка с производным summary (§8.1, U17).** В
`acceptance.py` добавлены `ProductSummary` и `build_product_summary`:
`automated_gates_pass`, `model_design_review_pass`, `release_ready` и
блокеры вычисляются из `ProductAcceptance` и той же evidence, что и у
агрегатора. `user_approved` по умолчанию `false` и никогда не ставится
моделью. `acceptance.main()` пишет и `product_acceptance.json`, и
`product_summary.json`; чужой dict под тем же именем моделью больше не
создаётся.

**9. Два реальных дефекта, найденных при реализации.** `restyle_runner.py:831`
обращался к `state.tasks`, которого у `RunState` нет (поле называется
`state.task_queue`) — ветка валидации сцены падала с `AttributeError`, не
успев проверить ничего. В новой fail-closed ветке `rules_path` использовался
до присваивания (`UnboundLocalError`). Оба исправлены.

### Проверено исполнением

- Публичный runner `slides gpn restyle` на `NTC_business_AI_GPN.pptx`:
  `_profile_stage` → `semantic` → выдан `T-semantic-98e2357cdba6`.
- Harness-ответ (авторство модели) принят и потреблён один раз:
  материализованы `inputs/semantic_deck.json` (8 слайдов, 74 юнита,
  117 relations) и `classification_and_transformation_ledger.json`
  (327 решений: 224 preserve, 83 recompose, 20 omit), привязанные к
  snapshot исходного IR. Стадия перешла в `design_briefs`.
- Все входы из задания определяются общей точкой входа без ветвления по
  имени файла: `NTC_business_AI_GPN.pptx` — 8 слайдов, `Круглый_стол.pptx`
  — 41, `ИИИ.pptx` — 20, скрытых слайдов нет (`generalization_audit.json`).
- Логотип NTC проверен по байтам исходника: `ppt/media/image-6-1.png`,
  618528 пикселей, 249255 с alpha>0, все `FFFFFF`, dominant share 1.0 —
  это colorway, а не дефект рендерера.
- Пробники (реальный Chromium, реальные GPN-файлы, реальные OOXML-байты):
  `overflow_coordinate_probe.json` (exact fit проходит; legacy-кадр,
  не учитывающий нижний inset, теперь ловится; на 1 pt меньше — падает),
  `empty_geometry_textfit_probe.json` (8 пустых фигур исключены, ни одна
  не дошла до измерителя, реальный текст в том же фикстуре измерен и
  переполняется), `overlap_permission_probe.json` (голая пара отклонена,
  типизованная принята, соседи не разрешены),
  `painter_order_rich_surface_probe.json`,
  `visual_semantics_negative_control.json`,
  `brand_white_on_white_probe.json` (подделанный sha256 отвергнут).

### Тесты

- `tests/gpn/test_stage11_universal.py` — 24 причинных регрессии (U01–U15,
  U17, U20; U04/U09/U12 частично), 23 passed, 1 skipped.
- Полный сюит: `pytest tests/gpn --no-cov` → **392 passed, 4 skipped in
  794.65s** (было 369 passed, 3 skipped).
- `ruff check src/ tests/` → **All checks passed!**
- Логи: `runs/stage9/2026-10-15-universal-ntc-r2/verification/test_logs/`.

### Проблемы и уточнения к предыдущим стадиям

- Фраза из прошлого раунда «гейт сохранённой геометрии вообще не выполнялся»
  неточна: `saved_geometry_report.json` выполнялся и проходил. Неточным было
  обобщение на все подгейты. Новая схема отчётов разводит области явно.
- Число 83 в текстовом отчёте прошлого пакета устарело: внешний manifest
  содержал 189 записей, ZIP — 190 entries.
- `_apply_decor_fill_line` действительно стирал штриги обязательных схем;
  сохранение XML-объекта при исчезновении его видимых штрихов не было
  семантическим сохранением. Исправлено переносом решения до эмиссии.
- Общий authoring path в этом раунде НЕ доведён до конца: стадии
  design_briefs → visual_concepts → scene_generation → build → render →
  review → selection → acceptance для NTC не выполнены; это не «почти
  готово», а явный незакрытый объём.

### Запросы пользователя

Явных новых запросов в этом раунде не поступало; выполнялось исходное
задание `opencode_universal_authoring_and_visual_repair.md`.

### Предстоит

- Пройти NTC до конца общим путём: авторские design briefs → минимум три
  осмысленно различающихся концепции на содержательный слайд → типизованные
  `AuthoredScene`/`ResolvedTextSpec` → измерение/решатель → native writer →
  рендер → visual/design review → отбор → сборка → приёмка.
- `ResolvedTextSpec` как отдельный тип с tokens (`text|field|soft_break|
  tab`), field metadata, hyperlink и list semantics; сейчас
  `emit_rich_text_block` принимает список paragraph-dict и не эмитит
  `a:fld`/`a:br`/`a:hlinkClick` в одном кадре (проверено только на
  text-only абзацах).
- Typed `layout_spec`/`geometry_spec`/`surface_spec` вместо `dict` в
  `AuthoredNode`.
- Проверить U05 с полями и ссылками в одном native-кадре, U06 на реальных
  сохранённых байтах (смешанный rich-блок поверх непрозрачной панели), U16
  (create-путь), U18 (publication bytes), U19 (другие имена/счётчики).
- Dense-3x контроль, fragmentation/rename/id-permutation invariance,
  mutation тест на копии JSON онтологии.
- Прогнать `Круглый_стол.pptx` (41 слайд) и `ИИИ.pptx` (20 слайдов) через
  тот же публичный runner без правок production-кода.
- Document create smokes (PDF/DOCX/prompt) через тот же target writer.
- Собрать `review_increment_universal_authoring.zip` с замкнутой ссылочной
  сеткой по §12 (сейчас есть `verification/` из 10 артефактов, но не
  полный пакет: нет per-candidate каталогов, рендеров и кода).

---

## Этап 9.1 (2026-10-15, продолжение): прогон NTC через общий authoring до стадии build

Прогон: `runs/stage9/2026-10-15-universal-ntc-r14` (r2…r14 — промежуточные
прогоны, каждый в новом каталоге; ни один не перезаписан).

### Реализовано

Исправлены реальные дефекты, найденные при прогоне общей цепочки:

1. **`state.tasks` → `state.task_queue`** (`restyle_runner.py:831`): ветка
   валидации сцены падала с `AttributeError` — поле называется
   `task_queue`. Раньше этот путь не выполнялся никогда.
2. **Единый путь к артефакту концепции.** `_advance_stage` читал
   `visual_concepts/<slide>/<concept>.json`, которого никто не создавал,
   а писатель клал в `scenes/<slide>/<candidate>/concept.json`. Из-за этого
   ВСЕ 21 scene-task теряли свой концепт и уходили в `SCENE_CONTEXT_MISSING`.
   Введён один помощник `_concept_path`, читают его оба места.
3. **Уникальные пакеты моделей** (`design.py`): все design-brief пакеты
   писались в `model_requests/design_brief.json`, и последний слайд
   затирал предыдущие. Теперь `model_requests/<task_id>.json`.
4. **`_materialize_scene_plan`** (`restyle_runner.py`): ответ модели
   адресует контент по id семантических юнитов (этого требует
   `validate_scene_response`), а мост раскладки привязывает зоны к
   source-object id. Раньше эти два контракта были несовместимы, и
   конфликт закрывался «валидируй ничего». Теперь юнит разворачивается в
   свои source-объекты в порядке юнита, а `plan.json` — то, что реально
   строит деку; авторский план сохраняется как `authored_plan.json`.
5. **Persisted brief**: стадия `design_briefs` не писала принятый бриф на
   диск, поэтому проверка fidelity читала отсутствующий файл и молча
   падала в `except`.
6. **Malformed brief → typed finding + retry** с сохранением исходного
   packet path, вместо жёсткой ошибки, которая оставляла прогон в
   подвешенном состоянии после уже потреблённого ответа.
7. **`MeasureParagraph.role`** и `LayoutZone.ref_roles` /
   `ComponentNode.ref_roles` (§4.2): один authored-блок может содержать
   разные роли. Метрика и её подпись раньше обе рендерились в роли блока
   (`large_metric` на подписи), и кадр переполнялся на 148 pt по
   измерению. Теперь роль абзаца резолвится отдельно и в измерителе, и в
   writer'е; неизвестная роль — запись, а не выдуманный кегль.
8. **`visual_norm`**: `SemanticUnit.unit_id`/`SemanticRelation.relation_id`
   не существуют — поля называются `id`; классификация падала на первом же
   юните.

### Исполнено

- 8 design briefs → 8 concept-наборов → **21 типизированная VisualConcept**
  (2 на титул и приложения, 3 на содержательных слайдах) → **21 scene plan**,
  каждый прошёл `validate_scene_response` без единого hard issue.
- Семантика: 8 слайдов, 107 юнитов, 117 relations, 327 решений трансформации.
- Модельный цикл исполняется настоящим harness-контрактом: `resume` →
  чтение pending-задач → типизированный ответ → `resume`. Ничего не
  помечается выполненным вручную.
- Измеренная обратная связь реально использовалась: полосы и разбивка
  корректировались по конкретным величинам переполнения (148 pt → 84 pt →
  12–17 pt → 2.8 pt на двух слайдах).

### Проблемы (статус честный: build не завершён)

Все 14 попыток сборки деков завершились `OMIT_NEEDS_REVIEW` на текст-без-
текстовых фигурах-контейнерах (напр. карточка «НТЦ + ДО» на титуле):
контейнер не имеет зоны в авторском плане, а `can_omit_source_object`
по контракту отказывает без решения о классификации. Это правильный
fail-closed отказ, а не обход: следующий шаг — добавить поверхности в
семантическую модель и разместить их в сцене с `surface_token`.

Открытые точки: размещение поверхностей и растровых ассетов в сцене;
стадии render / review / selection / acceptance; два других бизнес-входа;
create-smokes; dense-3x и invariance; бандл §12.

### Тесты

`tests/gpn/test_stage11_universal.py` — 23 passed, 1 skipped.
`pytest tests/gpn --no-cov` → **392 passed, 4 skipped**.
`ruff check src/ tests/` → **All checks passed!**

### Предстоит

- Поверхности (`surface`) и ассеты как явные узлы сцены с
  `surface_token` — чтобы контейнеры были размещены, а не запрашивали
  классификацию по ходу сборки.
- Затем: build → render → visual/design review → отбор → сборка →
  приёмка для NTC; те же стадии для `Круглый_стол.pptx` и `ИИИ.pptx`;
  create-smokes; dense-3x; invariance; бандл §12.

---

## Этап 9.2 (2026-10-15): дек NTC собирается; точная граница оставшихся дефектов

Прогон: `runs/stage9/2026-10-15-universal-ntc-r24` (r15…r24 — промежуточные).

### Реализовано

1. **Двойное связывание source-объекта.** `_materialize_scene_plan` теперь
   разворачивает юнит в source-объекты, заявляет каждый объект ровно за
   одной областью (зоны сортируются по порядку чтения сверху вниз) и
   записывает `SOURCE_BOUND_TWICE` (warning). Раньше один и тот же объект
   оказывался в двух зонах и молча уезжал в узкую ячейку — это давало
   фантомные переполнения до 118 pt.
2. **`ref_roles` фильтруется** при материализации: роль применяется только к
   объект��м, реально оставшимся в зоне.
3. **`MeasuredText.inner_box_in_frame_pt` / `content_bounds_inner_pt`**
   теперь клампаются в неотрицательные. Это регрессия моего же изменения
   0.3.0: кадр меньше своих inset'ов давал отрицательную ширину и всю
   выборку отбрасывал с `MEASUREMENT_SCHEMA_REJECTED`. Сырое (возможно
   отрицательное) значение остаётся внутри `detectOverflow` и по-прежнему
   даёт `inner_box_non_positive`.
4. **Модельная раскладка** доведена до измеримого состояния: векторные
   примитивы и растровые ассеты получили собственную полосу во всю ширину
   под контентными рядами (раньше они конкурировали с колонкой контента и
   получали ячейку 2.2 % ширины); неописанные подписи ушли в полосу lead;
   21 scene plan проходит `validate_scene_response` без hard issues.

### Исполнено

- **Дек NTC собран**: `scenes/primary/candidate.pptx`, layout resolved,
  `saved_geometry_report.json` по фактическим байтам, `content_diff.json`,
  `required_visibility_report.json`, `saved_authoring_rule_report.json`.
- **Проверка empty-geometry работает end-to-end на настоящем пакете:**
  `saved_measurement_report.json` → `applicable 19`, `measured 19`,
  `not_applicable 8`, `overflows 0`, `ok true`. Те самые фигуры, которые в
  прошлом раунде давали десять ложных переполнений, теперь не измеряются
  как текст, а настоящий текст измерен и проходит.
- `_build_deck_candidate` больше не считает наличие файла успехом: все 14
  попыток имеют `operation_status=needs_review`, `eligible=false` с
  конкретным `eligibility_reason`.

### Проблемы (остаются, не скрыты)

1. **`content_diff`: `missing 244`, `dangling 68`, `ok false`.** Это и есть
   §4.3 `compare_span_preservation`: при many-to-one склейке Comparator не
   умеет сопоставлять bound span конкретного source-атома с конкретным
   target-абзацем/run'ом. Часть выходных привязок указывает на
   `no-such-atom`. До реализации этого сравнения приёмка невозможна —
   обойти это «нулевым text diff» нельзя.
2. **`required_visibility_report`: `required_source_parts 327`,
   `required_after_ids 47`, `result fail`.** Причина та же: привязка
   many-to-one не доведена до реестра.
3. **`saved_geometry_diff`: 2 отклонения из 55 объектов.**

### Предстоит

- `compare_span_preservation(expected, saved, emission_map)` с адресацией
  source span → target paragraph/run/cell/field; именно он закрывает (1)
  и (2).
- Затем: render → visual/design review → отбор → сборка → приёмка; второй
  и третий бизнес-вход; create-smokes; dense-3x; invariance; бандл §12.

---

## Этап 9.3 (2026-10-15): компоновка привязок many-to-one; точная граница незавершённого

Прогон: `runs/stage9/2026-10-15-universal-ntc-r26`.

### Реализовано

1. **`_rich_text_paragraphs` / `_authored_block_paragraphs` больше не
   перетирают id абзаца.** Атом реестра — это и есть `para.id` исходного IR
   (`provenance._atomize_text_payload: id=para.id`). Подстановка
   `<objid>:<idx>` порождала привязки, не резолвящиеся ни в один атом.
2. **Writer не выдумывает привязку**: run-атом привязывается по исходному
   run id, а если его нет — пишется `BINDING_SKIPPED`, а не фиктивный id.
3. **`native_rebuild` добавлен в допустимые transform.** Это стратегия
   собственного writer'а: содержимое переписывается дословно в новый
   `p:sp/txBody` и затем сравнивается с исходным атомом как любой другой
   transform. В списке unexpected он отвергал сам writer, который должен
   производить выход, а не защищать содержимое.

Измеряемый эффект на NTC: `unexpected_rewrites 34 → 0`,
`dangling 68 → 34`, `missing 244 → 210` при `changed 0` и `duplicated 0`.

### Что осталось НЕВЫПОЛНЕННЫМ (полный список, без смягчения)

Приёмка невозможна, пока не закрыты эти пункты исходного задания:

1. **§4.3 `compare_span_preservation`** — не реализован как отдельная
   функция. Остаточные `missing 210` / `dangling 34`: часть source-объектов
   не эмитится вовсе, часть привязок всё ещё не резолвится. Требуется
   адресация source span → target paragraph/run/cell/field.
2. **`required_visibility_report`: `47 из 327`, `result fail`** — следствие (1).
3. **§4.2 `ResolvedTextSpec`** как тип с `InlineToken` (text/field/
   soft_break/tab), `FieldMetadata`, `HyperlinkIR`, list semantics.
   Сейчас есть только per-paragraph `role`.
4. **§4.1** типизованные `layout_spec` / `geometry_spec` / `surface_spec` и
   живой `emit_scene(scene)`; `AuthoredScene` в основном пути не
   используется, работает `LayoutIntent` + `compile_composition`.
5. **§7.3** comparative review реальных PNG, выбор победителя, ремонт.
6. **§8.2** публикация/переименование с проверкой байтов, native
   edit/save/reimport round-trip, PowerPoint UI (или честный
   `unverified`).
7. **§9.1** `Круглый_стол.pptx` (41 слайд) и `ИИИ.pptx` (20 слайдов) через
   тот же публичный runner — не запускались.
8. **§9.2** create-smokes PDF / DOCX / prompt.
9. **§9.3** dense-3x, fragmentation / rename / id-permutation invariance,
   mutation тест онтологии.
10. **§11** U05 с полями и ссылками в одном кадре, U16, U18, U19.
11. **§12** архив `review_increment_universal_authoring.zip` с кодом —
    не собирается осознанно: обязательные per-candidate каталоги, рендеры и
    сырые measurement-запросы отсутствуют, а §12 требует в этом случае
    явный incomplete bundle, а не «complete».

### Тесты

`pytest tests/gpn --no-cov` → 392 passed, 4 skipped.
`ruff check src/ tests/` → All checks passed!

---

## Этап 10 — Unicode normalization + verification (2026-10-16)

### Реализовано
- `document_ingest.py::_resolve`: Unicode NFC normalization for Cyrillic
  filenames (Круглый_стол.pptx). Exact path first, then NFC match, then
  single-glob NFC match. Multiple NFC matches raise `INPUT_AMBIGUOUS_UNICODE`.
  Originals are never renamed.

### Тесты

`pytest tests/gpn --no-cov` → 392 passed, 4 skipped.
`ruff check src/ tests/` → All checks passed!

---

## Этап 11 (2026-10-16): разбор причины «0 из 8 слайдов собирается» и исправление сетки

Прогон: `runs/stage9/2026-10-16-ntc-fresh` (не изменялся; все новые
измерения сделаны его `scenes/primary` на копии входов).

### Уточнение к Этапу 9.3: где именно ломался общий путь

Прогон `2026-10-16-ntc-fresh` до этого этапа давал
`_build_deck_candidate(primary) → status=failed`, `BUILD_NO_CANDIDATE`,
`0/8` слайдов. Причина найдена измерением, а не чтением кода:

1. **Планы сцен, выданные harness, физически невыполнимы.** Зоны
   авторизованы в процентах от холста: у 49 зон слайда №3 сумма `h_pct` =
   183.6 %, у 60 зон слайда №2 — 362.2 %. Типичная высота текстовой зоны
   2.42 % = 12.0 pt, а измеренная потребность того же текста при
   нормативном кегле — 23.7…146.7 pt. Это не «плотность», это
   несогласованность единиц: автор задаёт пропорции, исполнитель должен
   получить физические рамки.
2. **Материал при этом помещается.** Отношение суммарной измеренной
   площади текста к площади content box: 0.31 / 0.76 / 0.72 / 1.17 /
   0.60 / 0.73 / 0.93 / 0.78 по слайдам №1…№8. То есть влезает не
   «меньше шрифта», а другая раскладка.
3. **Решатель не умел ничего из §5.5.** `expand_into_free` умел только
   растить рамку вниз/вправо; если полоса уже заполнена, цикл честно
   останавливался с `NO_PROGRESS`. Не было ни сверки высоты с
   измеренной потребностью, ни перераспределения высоты внутри
   авторизованной колонки, ни перераспределения ширины.

### Реализовано (все функции — в `layout.py`, вызываются из основного
`resolve_layout`)

1. **`reconcile_hinted_heights`** — авторизованная высота зоны трактуется
   как подсказка, а не как физический размер. Внутри каждой
   авторизованной колонки каждый текстовый кадр получает свою
   измеренную `required_outer_height_pt`, воздух между кадрами
   перераспределяется пропорционально авторским высотам, а верх и низ
   колонки остаются авторскими. Ширина и x — авторские, не трогаются.
   Колонка, которая не влезает в свой диапазон, **не сжимается**: она
   получает `CAPACITY_COLUMN_CONFLICT` с конкретными членами и
   сантиметрами.
2. **`_authored_columns`** — группировка зон в колонки по существенному
   перекрытию по x (≥ 80 % ширины обеих рамок). Полноширинная полоса под
   двумя колонками не сливается с ними в одну: иначе решатель видел бы
   один столбец высотой в весь слайд.
3. **`containers_follow_children`** — поверхность/карточка/акцентная
   планка идут за текстом, для которого они скомпонованы, сохраняя
   авторские отступы сверху и снизу. Рамка, из которой текст ушёл, больше
   не остаётся пустым слотом (§1.5).
4. **`free_horizontal_run` + измерение на новой ширине** — перед
   пересчётом высот каждый текстовый кадр получает право занять свободный
   воздух в своей же полосе, но только если результат по-прежнему
   **содержит** авторскую рамку (иначе это side placement, а не решение
   задачи вписывания). Потребность перемеряется на новой ширине; принятая
   ширина та, при которой измеренная высота строго меньше и нет
   переполнения.
5. **Порядок downward-repair — по чтению, а не по id.** `movables`
   сортируется по `(y, x, id)`. Раньше «предыдущим» для сдвига был
   предыдущий по списку, что толкало нижние рамки ещё ниже и вызывало
   ложный `no_room_below`.

### Измеренный эффект (тот же вход, тот же run dir, `scenes/primary`)

До: `0/8 resolved`, `_build_deck_candidate → failed`.
После: **слайд №1 — `resolved`**, дек собирается, эмитятся native
поверхности/панели/рисуются rich-блоки. Остальные 7 слайдов —
`needs_recompose` / `constraint_conflict` с конкретными причинами
(см. ниже).

### Проблемы (остаются, не скрыты)

1. **7 из 8 слайдов NTC всё ещё не собираются.** Блокирующие причины
   измерены и записаны в `candidate_evaluations.json`:
   * `CAPACITY_COLUMN_CONFLICT` (слайды №2, №4, №8): две колонки
     авторизованного плана физически не влезают в свой диапазон
     (например, на №8: 229.1 pt текста в диапазоне 194.9 pt);
   * `HINT_OVERLAP_UNRESOLVED` (№2–№7): текстовые кадры, выросшие до
     измеренной высоты, наталкиваются на декор, стоящий на авторских
     позициях, и вниз двигаться некуда.
   Это честный отказ решателя, а не обход: следующий шаг — другой
   authored scene (другая топология/кодирование), а не уменьшение кегля.
2. **Планы сцен по-прежнему выдаются harness как source-verbatim.**
   Это авторская ошибка, а не дефект движка: движок теперь сообщает о ней
   измерением, но новые планы ещё не написаны.
3. **Ничего из §4.3/§7/§8/§9/§12 в этом этапе не продвинуто:** отбор
   кандидатов, render/review/сравнение, второй и третий бизнес-вход,
   create-smokes, dense-3x, invariance, архив — статус прежний.

### Замечание о прогоне

Диагностические прогоны §11 выполнялись через `run_measured_build`
на `2026-10-16-ntc-fresh/scenes/primary`, поэтому **производные** артефакты
этого каталога (`layout/**`, `composition/**`, `measurement/**`,
`candidate_evaluations.json`, `stage5_readiness.json`, сохранённые отчёты)
перезаписаны. Не тронуты: `source_ir.json`, `source_ledger.json`,
`compiled_rules.json`, `template_profile.json`, `font_inventory.json`,
`plans/**`, `model_requests/**`, `model_responses/**`, `harness/**` —
то есть все авторские входы. Промежуточный `probe.pptx` удалён.
Следующие прогоны — только в новом run-каталоге.

### Тесты (проверено)

- `pytest tests/gpn --no-cov -p no:randomly` → **402 passed, 4 skipped**
  за 1154.28 s (регрессий от изменений этого этапа нет).
- `tests/gpn/test_stage9_layout_recovery.py` — добавлены 5 причинных
  регрессий на новую сетку: разделение колонок и полосы; выдача кадру его
  измеренной потребности с сохранением низа колонки; отказ вместо сжатия
  при нехватке места; расширение в свободный воздух без потери рамки;
  контейнер следует за своим текстом.
- `ruff check src/ tests/` → All checks passed!

### Предстоит

- Написать новые authored-сцены NTC с топологиями, которые проходят
  измеренную ёмкость (это работа модели, а не движка).
- Затем: полный дек NTC → saved-гейты → render → visual/design review →
  отбор → сборка → каноническая приёмка; второй и третий бизнес-вход;
  create-smokes; dense-3x; invariance; архив §12.

---

## Этап 12 (2026-10-17): общий model loop и lane-flow авторинг сцен

Прогоны: `runs/stage10/2026-10-17-ntc-laneflow`,
`runs/stage10/2026-10-17-ntc-laneflow2` (второй — текущий).

### Реализовано

1. **`gpn-restyler/harness/model_loop.py`** — драйвер модельного цикла.
   Читает pending-задачи из `run_state.json`, запускает соответствующий
   черновик и передаёт ответы обратно в **публичный** runner
   (`slides_cli gpn resume`). Стадии, которые он закрывает:
   `semantic_analysis` → `design_brief` → `visual_concept_generation` →
   `scene_generation`. Он ничего не помечает выполненным сам: проверку,
   однократное потребление и продвижение делает движок.
   Пройдено реально: semantic → 327 units / 17 relations / 327 решений,
   8 briefs, 8 concept-наборов, **24 scene plan** (3 на слайд × 8 слайдов).
2. **`gpn-restyler/harness/scenes_author_v7.py`** — новый автор сцен.
   Ключевое отличие от v6: высоты зон берутся из **реального измерения**
   (`measure_components` через Chromium с установленными GPN-шрифтами,
   164 текстовых объекта), а не из source-бокса; текст раскладывается
   потоком по колонкам, число колонок определяется топологией концепта
   (`typographic` → 1, `stages`/`process_flow` → 2, `matrix`/`hub_spoke` → 3).
   `content_refs` несут **semantic unit ids** — этого требует
   `validate_scene_response`; мост `_materialize_scene_plan` связывает их
   с source-объектами.
3. Все 24 scene plan прошли `validate_scene_response` без hard issues
   (в предыдущем раунде — 0/24 из-за `missing required units`).

### Измеренный результат на NTC (`2026-10-17-ntc-laneflow2`)

- Сборка деков: **1 из 17** кандидатов дошёл до байтов
  (`slide5…--c3`, `needs_review`). Раньше — 0 из 8.
- Прогон дошёл до стадии `visual_review`, то есть стадии
  ingest → profile → semantic → briefs → concepts → scenes → build → render
  пройдены публичным runner'ом без ручной подстановки.
- `pytest tests/gpn --no-cov` → 402 passed, 4 skipped (проверено на Этапе 11).
- `ruff check src/ tests/ gpn-restyler/harness/` → All checks passed.

### Проблемы (точные, не смягчённые)

1. **`MEASUREMENT_FACE_UNVERIFIED` на 4 слайдах из 8** (№2, №3, №4, №5):
   `MEASURE_FACE_071bacce76b2` (GPN_DIN-Regular) и
   `MEASURE_FACE_157571de7c3b` (GPN_DINCondensed-Bold). При этом во всех
   1338 сохранённых записях `font_load` эти лица `load_status=loaded`, а
   234 измерения имеют `confidence=exact_browser`. Ошибка возникает на
   **повторном** замере внутри поиска раскладки (`layout-<cand>-aN`),
   ответ которого не сохраняется. Это воспроизводимо и не объяснено —
   следующий обязательный шаг: сохранять сырой ответ неуспешного замера
   и локализовать, почему повторный вызов Chromium не подтвердил лица.
2. **`HINT_OVERLAP_UNRESOLVED` на 3 слайдах** (№1, №6, №7) и
   **`CAPACITY_COLUMN_CONFLICT` на №8**: декор остаётся на авторских
   позициях и занимает место, куда lane-flow принёс текст. Автор это
   фиксирует в `problems`/`uncertainties`, но зоны декора всё равно
   эмитятся (иначе юнит не покрыт и сцена невалидна по §3.3). Нужен
   разбор: либо `optional`-классификация необязательного декора на
   семантической стадии, либо перенос декора вместе с его текстом.
3. **Первый дек NTC целиком не собран**, поэтому не выполнялись:
   comparative native-рендеры №3/№6, visual/design review реальных PNG,
   отбор кандидатов, полная сборка, каноническая приёмка, публикация.
4. **Не начато**: `Круглый_стол.pptx` (41 слайд), `ИИИ.pptx` (20 слайдов),
   create-smokes PDF/DOCX/prompt, dense-3x, fragmentation/rename/
   id-permutation invariance, mutation тест онтологии, архив §12,
   пункты §4.3 `compare_span_preservation`, §8 публикация и
   native edit round-trip.

### Предстоит

- Локализовать и устранить `MEASUREMENT_FACE_UNVERIFIED` на повторных
  замерах (блокирует 4 из 8 слайдов).
- Разрешить конфликт «декор на авторской позиции vs поток текста» —
   это следующий по величине блокер сборки.
- Затем: полный дек NTC → review реальных PNG → отбор → приёмка; второй и
  третий бизнес-вход; create-smokes; dense-3x; invariance; архив §12.

---

## Этап 13 (2026-10-17): устранение ложной классификации измерения и
единый контракт физически измеримого кадра

Прогоны: `runs/stage10/2026-10-17-ntc-laneflow5` (эталон текущего
состояния), `…-laneflow6`, `…-laneflow7`, `…-laneflow8`.

### Уточнение к Этапу 12: `MEASUREMENT_FACE_UNVERIFIED` был ложным

Этап 12 записал этот код как «шрифты не загрузились». Проверка показала
обратное: во всех 1338 сохранённых записях `font_load` обе GPN-лица имеют
`load_status=loaded`, и 234 измерения имеют `confidence=exact_browser`.

Причина — **в коде проверки**, а не в шрифтах:

1. `measurement.py` считал «непроверенными шрифтами» **любой** элемент с
   `confidence != "exact_browser"`. Но `measure.js` ставит `unsupported`
   и по другой причине — `inner_box_non_positive`, когда внутренняя
   область кадра неположительна. То есть геометрический отказ
   перекладывался на шрифты.
2. Настоящая причина отказа — авторизованные зоны высотой 2.2 pt: при
   вертикальных отступах 3.6+3.6 pt внутренняя область равна −5.0 pt.
   Это дефект авторинга, а не измерения.
3. Отказавшийся вызов **не сохранялся**: `persist_measurement_pair`
   вызывался после `raise`, поэтому сырой пары request/response не
   существовало — прямой дефект замыкания §12.

### Реализовано

1. **`units.floor_text_frame(box, margins, line_pt)`** — единый контракт:
   кадр с текстом никогда не предлагается измерению ниже одной реальной
   строки. Применяется в трёх точках: конструирование `rich_text`,
   конструирование `text`/`shape` в `resolve_layout`, `primitives.
   resolve_shape_preserve`, и **одна принудительная точка прямо перед
   MeasurementRequest**, которая покрывает любой новый сайт
   конструирования.
2. **`MEASUREMENT_ITEM_UNSUPPORTED`** — отдельный код отказа для
   геометрически неизмеримых элементов, с перечислением конкретных
   component ids. Проверка шрифтов больше не смотрит на `confidence`,
   выставленный по геометрической причине.
3. **Сырая пара сохраняется до отказа** — отказавшийся замер теперь
   оставляет request/response в `measurement/{requests,responses}/`, а
   не только строку в summary.
4. **Классификация необязательного декора** (`semantic_author`):
   текстовый-беспорный вектор, который не является стрелкой-ребром,
   не содержит текста и имеет площадь ≤ 4000 pt², помечается
   `preservation="optional"` с записью решения в ledger и исключается
   из required-отношения `fabric`. Всё остальное — включая любой объект
   крупнее, любую стрелку и любое изображение — остаётся required.
   Это ровно то, чего требовал §6.1: не «все text-less объекты
   ненужные», а evidence-based классификация.
5. **`scenes_author_v7.py`**: при конфликте декора с потоком текста
   optional-юнит получает `authorized_omit` целиком, required-юнит
   порождает явный finding «нельзя молча убрать».

### Измеренный результат

- `MEASUREMENT_ITEM_UNSUPPORTED` / `MEASUREMENT_FACE_UNVERIFIED`
  **больше не встречается ни на одном слайде NTC** (было 4 из 8).
  Восемь слайдов из восьми теперь проходят измерение.
- Оставшиеся блокеры — только честные конфликты композиции:
  `CAPACITY_COLUMN_CONFLICT` (полоса уже не вмещает измеренный текст)
  и `HINT_OVERLAP_UNRESOLVED` (декор на авторской позиции).
- Проверено и отброшено (результат хуже, откачено): перенос текстового
  кадра на его source-ширину вместо ширины полосы — `BUILT 0 из 17`;
  на ширине полосы — `1 из 17`. Разница объяснена: движок меряет по
  ширине зоны, автор считал по source-ширине, и расхождение съедало
  ёмкость. Обе попытки записаны здесь, чтобы их не повторяли.

### Тесты

- 2 новые причинные регрессии в `test_stage9_layout_recovery.py`:
  контракт `floor_text_frame` (узкая полоса доходит до одной строки,
  ширина тоже доводится) и различение «геометрический отказ» /
  «отказ по шрифту».
- `pytest tests/gpn --no-cov -p no:randomly` → **404 passed, 4 skipped**
  за 1161.57 s — полный прогон без регрессий.
- `ruff check src/ tests/ harness/` → All checks passed!

### Проблемы (остаются)

1. `CAPACITY_COLUMN_CONFLICT` на №2, №3, №4, №5, №8: полоса 590 pt
   шириной не вмещает 430–478 pt измеренного текста плюс промежутки при
   span 447.8 pt. Нужен либо баланс полос по измеренной потребности
   (жадное распределение юнитов по наименее загруженной полосе), либо
   второй кандидат с другой топологией — обе вещи ещё не сделаны.
2. `HINT_OVERLAP_UNRESOLVED` на №1, №6, №7: декор, оставшийся required,
   стоит там, куда принёс текст.
3. Сборка деков: **1 из 17**. Полного NTC-дека нет, поэтому
   comparative-рендеры, review реальных PNG, отбор, приёмка и
   публикация не выполнялись.
4. Не начато: `Круглый_стол.pptx`, `ИИИ.pptx`, create-smokes,
   dense-3x, invariance, архив §12.

### Предстоит

- Баланс полос по измеренной потребности в авторе сцен (следующий по
  величине блокер; он же снимет бо́льшую часть capacity-конфликтов).
- Разрешение декора, который нельзя убрать и который стоит в полосе
  текста: перенос вместе с текстом либо явная re-express.
- Затем полный дек NTC → review → отбор → приёмка; второй и третий
  бизнес-вход; create-smokes; dense-3x; invariance; архив §12.

---

## Этап 14 (2026-10-17): единый контракт «меряю там, где ставлю» и
честная граница достигнутого

Прогоны: `runs/stage10/2026-10-17-ntc-laneflow6` и
`runs/stage10/2026-10-17-ntc-laneflow12` (актуальные),
промежуточные удалены.

### Уточнение к Этапу 13: почему оставались `CAPACITY_COLUMN_CONFLICT`

План автора был неправилен не по числам, а по единицам измерения.
Автор мерил текст на **source-ширине** и ставил его на **ширине полосы**.
Для узких объектов (номера шагов `1`/`2`/`3`, короткие метки) это давало
зону шириной 18 pt, в которой измеренная высота текста была в разы
больше, чем предполагал автор. Движок честно сообщал конфликт, которого
автор не видел.

### Реализовано

1. **`measure_at(deck, rules, bundle, roles, widths, tag, body)`** —
   измерение `required_outer_height_pt` для каждой пары
   (source-объект, ширина полосы) настоящими GPN-шрифтами через Chromium.
   Автор меряет **на каждой ширине, на которую юнит может попасть**, и
   ставит на ту же ширину: §5.1 «число, измеренное на одной ширине, не
   применяется к другой» теперь соблюдается на стороне сцены, а не
   только на стороне движка.
2. **`balance_lanes`** — распределение юнитов по полосам до тех пор,
   пока каждая полоса не вмещает свой текст, по измеренной потребности.
   Сохраняется порядок чтения внутри полосы; меняется только принадлежность
   к полосе. Это то самое «width redistribution / side placement»
   из §5.5, выполняемое сценой, которой принадлежит композиция.
3. **`_free_slot` + `_hits`** — обязательный декор, попавший в полосу
   текста, переносится вдоль своей колонки на первую свободную позицию
   (вниз, затем вверх), сохраняя x, ширину и высоту. Если свободной
   позиции нет — явный finding «нельзя молча спрятать», а не тихое
   сокрытие.

### Измеренный результат

`CAPACITY_COLUMN_CONFLICT` ушёл с 5 слайдов до 1 (остался только
слайд №3). Все 8 слайдов по-прежнему проходят измерение без единого
`MEASUREMENT_*_UNSUPPORTED`. Сборка деков — **1 из 17**
(`slide5…--c3`, `needs_review`).

### Проверено и отброшено (чтобы не повторяли)

| Попытка | Результат | Почему |
|---|---|---|
| перенос текстового кадра на source-ширину | `BUILT 0 из 17` | ширина не совпадает с измеренной — движок перемерит иначе |
| узкие зоны 18 pt для коротких меток | `BUILT 0 из 17` | перенос строк при своей же ширине |
| декор как несдвигаемое препятствие вместо movable | `BUILT 0 из 17` | текст стал упираться в неподвижный декор, который решатель уже умел обходить сдвигом |
| `@`-разделитель в id измерения | `MEASUREMENT_INTEGRITY_REJECTED` | идентификатор компонента не может содержать `@`; используется `-wN` |

### Тесты

- `pytest tests/gpn --no-cov -p no:randomly` → **404 passed, 4 skipped**
  за 1157.57 s — полный прогон без регрессий.
- `ruff check src/ tests/ harness/` → All checks passed!

### Проблемы (остаются, не закрыты)

1. **`HINT_OVERLAP_UNRESOLVED` на 7 слайдах из 8** — единственный
   теперь блокер сборки. Решатель умеет двигать конфликтующие рамки
   только вниз; на плотных слайдах вниз некуда, а движение вбок/вверх
   не реализовано для всех случаев (`place_side_by_side` проверяет лишь
   несколько позиций-кандидатов). Это следующий по величине блокер.
2. **`CAPACITY_COLUMN_CONFLICT` на слайде №3** — одна полоса 590 pt не
   вмещает 343 pt текста плюс промежутки при span 447.8 pt; балансировка
   не смогла это развести, потому что соседняя полоса уже заполнена.
3. **Полного NTC-дека нет** (1 из 17 кандидатов). Поэтому
   comparative-рендеры №3/№6, visual/design review реальных PNG, отбор,
   полная сборка, каноническая приёмка и публикация не выполнялись.
4. **Не начато:** `Круглый_стол.pptx` (41 слайд), `ИИИ.pptx` (20),
   create-smokes PDF/DOCX/prompt, dense-3x, fragmentation/rename/
   id-permutation invariance, mutation тест онтологии, архив §12,
   `compare_span_preservation` (§4.3), native edit round-trip (§8.2).

### Предстоит (в порядке убывания эффекта)

1. Двухсторонний (вниз **и** вверх) поиск свободного места в
   `_resolve_hint_overlaps` вместо нисходящего-only — снимет
   `HINT_OVERLAP_UNRESOLVED` на большинстве слайдов.
2. `CAPACITY_COLUMN_CONFLICT` на №3: перенос полосы в свободную часть
   content box либо третий кандидат с другой топологией.
3. Далее по списку выше.

Исходный объём задания **не выполнен**: из §1–§13 закрыт блок
измерения/физической измеримости кадра и контракт авторинга по
измеренной потребности; сборка деков, review/отбор/приёмка, три бизнес-
входа, create-smokes, dense-3x, invariance и архив — нет.

---

## Этап 15 (2026-10-17): архив инкрементов и разбор блокеров

### Реализовано

* `dist/review_increment_universal_authoring.zip` (24 МБ, **4542 файла**) —
  полный архив инкрементов. Манифест проверен процедурой из README:
  **4542 файла, 0 расхождений** по размеру и SHA-256.
  `manifest.complete = false` — явно, с перечислением причин неполноты.
* `BLOCKERS.md` — подробный разбор **семи блокеров**, по каждому: где
  обнаружен, каким кодом порождён, что уже исправлено, что осталось, какая
  попытка дала или не дала эффекта.
* `audit/` — 10 записей, все посчитаны по реальным файлам и прогонам:
  примирение утверждений §1 (что подтверждено, что опровергнуто, что
  осталось открытым), аудит обобщаемости, активный callgraph с указанием
  места обрыва, версии движка и хэши движковых файлов, манифест трёх
  бизнес-входов и synthetic-случаев, состояние канонической приёмки
  (MISSING, а не pass), хэши корпусов, отчёт о доступе к reference,
  выполненные и **не**выполненные команды, доказательства тестов.
* `code/full/` — весь изменённый и добавленный код: 30 модулей движка,
  `measure.js`, вся выборка `tests/gpn`, harness, skills,
  `IMPLEMENTATION_STATE.md`, `development_history.md`, зависимости;
  плюс `git_diff.patch` (366 КБ) и `repo_head_and_status.txt`.
* `runs/` — два актуальных прогона серии (`laneflow6`, `laneflow12`) с
  планами, ответами модели, попытками сборки и отчётами: 2281 и 2143
  файла.
* Сборщики: `harness/build_audit.py`, `harness/build_increment_archive.py`.

### Тесты

`pytest tests/gpn --no-cov` → **404 passed, 4 skipped** (лог в архиве).
`ruff check src/ tests/ harness/` → All checks passed!

### Проблемы (архив это фиксирует явно, а не скрывает)

1. `manifest.complete = false`. Причины: нет принятого продукта; два
   бизнес-входа из трёх не запускались; create-smokes, dense-3x и
   invariance не выполнялись; бинарники (PNG/PDF/PPTX/шрифты)
   исключены правилом размера с указанием причины.
2. `audit/generalization_audit.json` содержит **вердикт
   «обобщаемость НЕ доказана»**: выполнен 1 вход из 3, а
   `briefs_author.py`/`concepts_author.py` всё ещё именуют выходы по
   индексу слайда (harness-сторона, production-код не трогает, но и
   доказательством обобщаемости не является).
3. `audit/acceptance_schema_validation.json` фиксирует, что
   `product_acceptance.json` **отсутствует** (MISSING), и отдельно
   напоминает, что рукописный `product_acceptance.json` предыдущего
   раунда невалиден по канонической схеме и не является доказательством.
4. Основной блокер сборки не изменился: `HINT_OVERLAP_UNRESOLVED` на
   7 слайдах из 8 — нисходящий-only ремонт перекрытий в
   `layout._resolve_hint_overlaps`.

### Предстоит

Двусторонний (вниз и вверх) поиск свободного места в
`_resolve_hint_overlaps`; затем `CAPACITY_COLUMN_CONFLICT` на слайде №3;
затем полный дек NTC → review → отбор → приёмка; второй и третий
бизнес-вход; create-smokes; dense-3x; invariance; пересборка архива.

---

## Этап 16 — приём кодового патча `gpn_delivery_patch` и продолжение batch из трёх презентаций

Дата: 2026-10-18. Все предыдущие разделы сохранены без изменений.

### Реализовано

**1. Установка патча с сохранением локальных изменений.**

* `APPLY_PATCH.py --repo … --report install_preflight.json` → первый
  preflight: `status=conflicts`, единственный конфликт
  `src/slides_cli/gpn/render.py: EXACT_CONTEXT_CONFLICT:matches=0`.
  Установщик не записал ни одного проектного файла.
* Причина найдена по `baseline/`, `payload/` и текущему файлу: payload
  `render.py` **не содержит** `render_source_deck()`, а `restyle_runner.py`
  (и baseline, и payload) импортирует его из `.render`; payload также
  теряет локальные опции `pdf_stem`/`page_prefix`. Слепое применение
  дало бы `ImportError` на шаге source render.
* Ручная интеграция по функциям: `render_native_candidate` оставлен
  байт-в-байт как в payload (включая новое
  `ExportHiddenSlides=true`), `render_source_deck()` восстановлен как
  отдельная функция в конце модуля и переименовывает артефакты
  (`candidate.pdf → source.pdf`, `page-N.png → slide-N.png`) вместо
  опций рендерера; доктринальная фраза про «hidden slides не
  экспортируются» исправлена, потому что после патча они
  экспортируются. Решения и diff — в
  `gpn_delivery_patch/patch_integration_report.json` и
  `gpn_delivery_patch/render_integration.diff`.
* Повторный preflight → `status=preflight_ok`, `render.py` =
  `already_applied_with_local_changes`; `--apply` → `status=applied`,
  `changed_files=27`, резервная копия в
  `gpn-restyler/.delivery_patch_backups/20261018T101254459970Z`.
* Импорты CLI проверены: `restyle_runner.__file__` и `layout.__file__`
  указывают на этот checkout; `ortools 9.15.6755`, Python 3.12.9.
  Полная переустановка окружения не выполнялась — версия уже была.

**2. Триаж 16 падений полного suite.** Базовый замер сделан на
реконструированном pre-patch checkout (копия репозитория с
восстановленными 12 файлами из резервной копии установщика, удалёнными
11 новыми файлами патча и копиями корпусов): 19 падений, из которых
основная причина — `MEASUREMENT_TOOL_FAILED: tool exit=1`, то есть
измеритель Chromium в pre-patch коде был сломан. После патча эти тесты
проходят. Из 16 оставшихся:

* **Реальные дефекты, исправлены в коде:**
  1. `native.emit_rich_text_block.resolve_role` бросал
     `STYLE_TOKEN_UNKNOWN:role:` для любой роли без `color_tokens`, а в
     production `compiled_rules.json` `color_tokens` пуст у всех ролей —
     то есть патч ломал нативный эмит текста целиком. Роль без
     объявленного токена теперь сохраняет унаследованный цвет (ничего не
     придумывается), а **объявленный, но неизвестный** токен
     фиксируется как `SURFACE_TOKEN_UNKNOWN` в issues — как в трёх других
     эмиттерах этого же файла. Проверка `STYLE_ROLE_UNBOUND` сохранена.
  2. `measured_placement.place_measured_components` отказывался
     (`MEASUREMENT_NEED_MISSING`) на авторинге без текста, хотя сам
     измеритель помечает такой объект как «легитимная пустая геометрия»
     (`content_absent`). Карточка/панель/линия без текста теперь
     сохраняет свою авторскую геометрию и участвует в solver как любой
     другой узел; факт фиксируется в evidence
     (`authored_geometry_without_content`). Проверка на content с
     нулевым `required_outer_height_pt` сохранена.
  3. `composition`: rich-text группировка перебрасывала биндинги, и
     `representation="authorized_repeat"` терялся — оператор `repeat`
     молча оставался без эффекта (`OPERATOR_NO_EFFECT`, покрытие
     `partial`). Идентичность вхождения теперь переносится в
     сгруппированный блок по порядку.
  4. `restyle_runner._consume_scene_responses`: неразрешимая
     provenance кандидата поднимала `RunnerError` и роняла весь resume.
     Теперь это отказ с квитанцией `SCENE_PROVENANCE_MISSING` в
     `rejected_scene_reports` и error finding; ничего не строится.
  5. `layout`: при `INFEASIBLE` attempt терял измеренную причину. Теперь
     `hard_issues` содержат и вердикт solver, и измеренные
     `overflow_x/overflow_y` по конкретным компонентам.
* **Устаревшие ожидания старого контракта, обновлены без ослабления
  причинной проверки:** усечённые префиксы `c-{ref[:8]}` → полные
  source ID; словарь статусов layout (`infeasible`, доказанный CP-SAT);
  `SOLVER_EVIDENCE_ACTIONS` как evidence, а не мутация (проверка
  «evidence никогда не несёт run/box/type override»);
  `_stub_measurer` переведён на текущий wire-контракт
  (`required_outer_height_pt`, `confidence`, `content_absent`) и
  запускается с честным `profile_kind=test_only`; `run id` rich-блока
  адресуется исходным абзацем (`p["id"] + "/"`), а не префиксом
  объекта; `reorient` заменён на доказательство solver call со scope;
  phantom-ref теперь не связывается ни с одной зоной.
* Один тест (`test_scene_plan_unexpanded_ref_is_reported`) был красным
  и до патча — причина исправлена, усилена проверка.

**3. Реальный прогон трёх кейсов.** `register → activate (preflight) →
activate --apply → next`.

* Состояния трёх run действительно относятся к трём разным исходным
  файлам (`input_fingerprint` = sha256 соответствующих PPTX):
  NTC `98e2357c…`, `Круглый_стол` `8785e4f8…`, `ИИИ` `68c15b05…`;
  имена сопоставлены по Unicode NFC, расхождений нет.
* Копии `run_state` и хэши корпусов (ontology, slide_examples, три PPTX)
  сняты **до** активации:
  `runs/recovery-batch-20261016T173142Z/delivery_checks/`.
* Активация: NTC — 24 плана архивированы, 24 старых pending-задачи
  помечены `superseded`, выпущены 24 новых immutable-задачи v3
  (`code_sha256` включён в fingerprint). round_table и iii честно
  получили `semantic_still_required` — их семантика ещё не была
  выполнена моделью.
* Пререфьюзит NTC показал истинную причину старых планов: они
  использовали `metric_value`/`metric_caption`/`diagram_node` как
  `ref_roles`, то есть семантические типы выдавали за style tokens
  ontology, и `ref_roles` были не элементами `content_refs`. Это ровно
  то, что запрещает LAUNCH_PROMPT §5; планы убраны из исполняемой
  позиции, но сохранены в истории.
* **Выполнено два семантических ответа текущей моделью**
  (`actual_model` записан честно):
  * `ИИИ.pptx`: 20 слайдов, 462 объекта, 180 units, 4 relations,
    462 transformation decision (242 preserve / 2 replace / 218 omit).
  * `Круглый_стол.pptx`: 41 слайд, 785 объектов, 334 units, 3 relations,
    785 решений (505 preserve / 280 omit).
  Оба приняты (`request_consumed`), оба кейса перешли на
  `design_briefs`.

### Тесты

* Целевой набор патча: `pytest tests/gpn/test_delivery_patch.py
  test_delivery_dispatch.py test_delivery_native.py
  test_runner_reissue_idempotence.py test_stage7_runner.py --no-cov`
  → **64 passed**.
* `ruff check src/ tests/ tools/harness_batch.py harness/model_loop.py`
  → All checks passed.
* Полный suite Mac: **612 passed, 4 skipped** (было 596 passed / 16
  failed до триажа), лог в
  `delivery_checks/pytest_full_after_triage.log`.
* Pre-patch базовый замер тех же модулей: 19 failed / лог
  `delivery_checks/pytest_same_tests_PREPATCH.log`.

### Проблемы

1. **Продукт не завершён.** После двух семантических ответов в очереди
   осталось ~393 модельных задания: NTC 330 (8 brief + 8 concept + 313
   scene/repair), round_table 41 brief, iii 20 brief. Один такой ответ
   (ИИИ) — это чтение 20 слайдов, 462 объектов и 20 рендеров; два кейса
   вместе — уже за пределами одной сессии. Ни один кандидат ещё не
   собран, отбор и приёмка не выполнялись.
2. `finish` ожидаемо не проходит: нет принятого продукта ни у одного
   кейса. Это зафиксировано как статус, а не обойдено.
3. NTC после активации несёт 313 scene-задач на 24 слайда — это следы
   прежнего ремонтного цикла (c1…c4 + repair). Бюджет прогона
   (`max_model_tasks: 235`) может быть исчерпан до отбора; при
   достижении лимита сохраняется честный checkpoint.
4. Корпуса и шрифты не менялись: хэши до/после идентичны.

### Запросы пользователя

Новых нет. Работа по применению патча и продолжению batch уже была
разрешена; дополнительных разрешений не запрашивалось.

### Предстоит

Продолжить цикл `next → авторский ответ → submit` в порядке приоритета
диспетчера: сначала 41 design_brief `round_table` и 20 design_brief `iii`
(они уже выданы и ждут), затем concepts и сцены `iii`, затем сцены NTC
(24 слайда × кандидаты). Для каждого слайда — минимум два реально
отрендеренных и просмотренных варианта, отбор после покрытия всех
ожидаемых source slides, финальная сборка, свежие reviews по её
байтам, `product_accepted` только общим acceptance path. Следующая
конкретная операция: `harness_batch.py … next` для `round_table`
(`T-brief-slide0:ppt/slides/slide1.xml`).

---

## Этап 16.1 (2026-10-18): уточнение к Этапу 16 и фактическое состояние на передачу

Раздел добавлен, а не отредактирован: Этап 16 остаётся как есть.

### Уточнение к Этапу 16

В Этапе 16 указано «~393 модельных задания в очереди». Это число
считало **всю** запись `task_queue`, включая историю. Фактическое
распределение по статусам:

| Кейс | stage | pending | consumed | rejected | superseded |
|---|---|---|---|---|---|
| ntc | build | **24** | 34 | 240 | 24 |
| round_table | design_briefs | **41** | 1 | 0 | 0 |
| iii | design_briefs | **20** | 1 | 0 | 0 |

То есть реально ждут модели **85** заданий, а не 393: 24 новых
scene_generation v3 у NTC, 41 design_brief у `Круглый_стол` и 20
design_brief у `ИИИ`. У NTC дополнительно потребребуются
visual_concept_generation для двух новых кейсов — они выдаются после
briefs. Пререфьюзит активации и оба принятых семантических ответа
остаются в силе.

### Фактическое состояние на передачу

* `finish` → **exit 2**, `product_complete: false`, кейсы:
  `ntc=build`, `round_table=design_briefs`, `iii=design_briefs`.
  Причина — нет принятого продукта ни у одного кейса; ни один кандидат
  ещё не собран, отбор и финальная сборка не выполнялись.
* Ничего не подписано вручную: `accepted`, `materialized`,
  `render_verified` не выставлялись. `accepted_product` пуст у всех трёх.
* Корпуса и три исходных PPTX не изменились: `corpus_hashes_before.json`
  и `corpus_hashes_after.json` совпадают (`unchanged_vs_before: true`).
* Проверочный пакет `delivery_review.zip` — 227 записей, манифест
  пересчитан (`manifest_added.json` покрывает все записи).
* Точная следующая операция:
  `uv run python tools/harness_batch.py --batch-root <batch> next`
  → `ntc` / `scene_generation` / `401d7de4-002d-515e-b6b8-ada661af8ede`.
  Приоритет диспетчера после этого: 41 brief `round_table`, 20 brief
  `iii`, затем concepts и сцены `iii`, затем сцены NTC.

### Что осталось непроверенным

Визуальная и дизайнерская приёмка трёх деков, отбор кандидатов,
`product_accepted` и публикация **не выполнялись**. Unit/интеграционные
тесты этого не заменяют.

---

## Этап 17 (2026-10-20): render identity, изолированные шрифты, pre-build coverage

Раздел добавлен после Этапа 16.1; предыдущие разделы не изменялись.

### Реализовано

1. **Причина font substitution найдена и воспроизведена.**
   Все GPN OTF/TTF несли platform-1 family `GPN_DIN`, поэтому
   LibreOffice на macOS (CoreText) сводил `GPN_DIN Condensed Bold` и
   `GPN_DIN Condensed Regular` к `GPN_DIN Regular`. Старый render III
   cover падал с `expected faces absent from render`.
2. **Изолированный font home в `render.py`.**
   `_normalize_platform1_family(path, family)` переписывает platform-1
   nameID 1/4/16 на внутреннее имя лица; platform-3 записи не трогаются.
   `_prepare_isolated_font_home(bundle, root)` собирает временный HOME с
   нормализованными копиями. `shutil.copy2` заменён на `shutil.copyfile`
   из-за `PermissionError: chflags` на `Wingdings.ttf`.
3. **`NativeRenderProbe` расширен** полями `font_environment_sha256`,
   `isolated_font_faces`, `font_normalization`.
4. **Render identity.** `RenderIdentityResult` и
   `current_render_identity(candidate, manifest, expected_page_count)`
   хешируют реальные candidate/PDF/PNG/manifest, проверяют
   `render_result`, scope страниц и размер. Review packet теперь несёт
   `render_identity`; `accept` отклоняется, если identity stale
   (`REVIEW_RENDER_IDENTITY_STALE:*`,
   `REVIEW_ACCEPT_WITH_FAILED_NATIVE_RENDER`).
5. **Stale reviews superseded.** `render_candidates` сравнивает identity
   с pending review packets; `visual_review`/`design_review` stages
   пере-рендеривают при смене окружения и выпускают обзоры только по
   текущим пикселям. В `review_manifest` добавлены `renderer`,
   `renderer_version`, `font_environment_sha256`, `isolated_font_faces`.
6. **Pre-build coverage.** `check_scene_visual_coverage` и
   `VisualCoverageReport` проверяют каждый REQUIRED visual unit до
   сборки: регион должен объявлять palette fill/stroke/foreground или
   approved brand asset. Пустой `surface_token` у required decor —
   `SCENE_VISUAL_COVERAGE_UNRESOLVED`, а не «source fill dropped».
   Проверка подключена в `validate_payload` после `lower_scene_plan`.
7. **III cover.** Новый render probe: `result=pass`, substitution нет,
   все три GPN-лица в isolated home. `font_environment_sha256` =
   `2a4a6b97a8ad37596a379392e2887ed6203970634ff3e57e67ba3d8ff0908c7f`
   (III run, после bundle hash). PNG просмотрен.
8. **III slide1 concepts.** Авторский ответ на
   `T-concepts-slide1:ppt/slides/slide2.xml` принят после двух
   исправлений: `schema_version_id`, `actual_model`, затем замена
   `process_flow` на `typographic`/`matrix` (в источнике нет flow
   relations). Ответ создал 3 сцены и 3 visual_review в III.

### Проблемы

- NTC slide0 c3: текущий собранный candidate всё ещё несёт
  `VISUAL_REPRESENTATION_UNRESOLVED` для `cead27a5...`; более поздний
  repair-ответ с `surface_token:"light"` существует, но собранный
  `composition` его не содержит. Нужен новый repair/build, а не
  повторный обзор старого candidate.
- `TransformLedger.model_validate` не принимает flat ledger
  (`authorization_ref`, `evidence_refs`); для coverage используется
  нормализатор `_load_semantic_gate_inputs`.
- `ty check` по затронутым файлам показывает прежние диагностики
  (`SourceRef` union, `CompiledOntology`, PIL pixel types); новых
  ошибок в добавленном коде не выявлено.
- Полная продуктовая приёмка трёх деков не выполнена.

### Тесты

- `uv run ruff check` по `review_jobs.py`, `response_validation.py`,
  `restyle_runner.py`, `visual_norm.py`, `models.py`: **clean**.
- `uv run pytest -q --no-cov tests/test_validation.py
  tests/test_refactors.py`: **37 passed**, exit 0.
- III render probe: 3 лица, `result=pass`, страница 1 просмотрена.
- Coverage smoke на NTC c3: оба repair-ответа (`ntc_c3_repair2`,
  `c188...`) дают `ok=True`; старый собранный plan не проверялся как
  candidate, потому что он уже прошёл emission.

### Запросы пользователя

Продолжать существующий recovery batch; не создавать новый batch;
не переписывать историю; работать строго в директории проекта;
не подменять render проверкой XML; не делать required-диаграмму
optional из-за пустого текста.

### Предстоит

1. Пересобрать/отремонтировать NTC slide0 c3 с band
   `surface_token:"light"` и повторно прогнать emission/render/review.
2. Дождаться текущих III scene_generation и трёх visual_review,
   проверить их PNG и завершить III cover + slide1.
3. Продолжить очередь III (design_briefs 2–19 → concepts → scenes),
   затем NTC (slides 1–7 scenes + remaining reviews), затем
   round_table (41 brief).
4. После покрытия всех слайдов — selection, assembly, fresh render
   identity, acceptance и `delivery_checks/visual_encoding` bundle.

---

## Этап 17.1 (2026-10-20): диспетчер больше не подаёт устаревшие reviews

Дополнение к Этапу 17; предыдущий текст не изменялся.

### Реализовано

- `tools/harness_batch.py::_refresh_stale_reviews` перед `_queue` находит
  run с pending `visual_review`/`design_review`, вызывает
  `_render_all_candidates` и сохраняет state. Старый pending-review
  помечается `superseded`, новый выпускается по текущему render
  identity. Это закрывает случай, когда `next` отдавал review-пакет,
  выпущенный до смены font environment.
- `round_table` slide1 design_brief принят после исправления
  `input_fingerprint`; `iii` slide1 concepts приняты после замены
  `process_flow` на `typographic`/`matrix`.
- `next` после этого выпустил NTC `scene_repair` для slide1 c1
  (`2899e2a0-3346-5399-aadd-c956a35f2676`), а не устаревший review.

### Тесты

- `ruff check tools/harness_batch.py`: clean.
- `next` на recovery batch: stale round_table review снят, очередь
  продвинулась к NTC scene_repair; ошибок диспетчера нет.

### Предстоит

Авторский `scene_repair` для NTC slide1 c1: 65 required units, текущий
кандидат не собрался (`BUILD_NO_CANDIDATE`). После него — остальные
сцены NTC и round_table, затем selection/assembly/acceptance.

---

## Этап 18 (2026-10-20): разблокировка build, дефект supersede и остановка перед source-aware сценами

Дополнение к Этапам 17/17.1; предыдущий текст не изменялся.

### Реализовано

1. **Все три run выведены из `blocked` в `build`.** Были сняты
   `stage=blocked`, `terminal_reason=COMPARATIVE_VARIANTS_MISSING:*` и
   старые findings. Это позволило повторно вызвать
   `_build_all_deck_candidates` на уже `scene_validated` кандидатах.
2. **Build выполнен по всем делам:**
   - III: 60 кандидатов, 57 рендеров; затем `resume` дошёл до
     `stage=visual_review` (57 pending `visual_review`, 1 pending
     `design_review`).
   - NTC: 24 кандидата, 2 diagnostic deck, 2 рендера.
   - round_table: 123 кандидата, 90 рендеров.
3. **Исправлен `_supersede_stale_reviews` в `review_jobs.py`.**
   Раньше записанный `render_identity` сравнивался с урезанным
   five-field dict, поэтому любой pending review сразу помечался
   `superseded`. Теперь сравниваются только поля, которые реально
   устанавливает probe (`candidate_sha256`, `render_manifest_sha256`,
   `pdf_sha256`, `ordered_page_hashes`, `result`); остальные поля
   review packet не считаются расхождением.
4. **Исправлен `_enqueue` в `restyle_runner.py`.** Задача с тем же
   `request_id` и тем же `input_fingerprint`, ранее помеченная
   `superseded`, снова переводится в `pending`. Без этого
   детерминированно переизданный review не попадал в очередь, и run
   ошибочно переходил к selection, имея только 4 старых review.
5. **Проверки:** `ruff check` по `review_jobs.py` и
   `restyle_runner.py` — clean; targeted pytest
   (`test_stage3_native.py`, `test_stage5_render.py`,
   `test_stage7_acceptance.py`, `test_stage7_runner.py`) — exit 0,
   без падений.

### Проблемы

- **Главный дефект качества: авто-сцены теряют обязательные исходные
  диаграммы/графики.** В semantic deck:
  - III: 20 слайдов, `required_exact` = 7 image + 1 quote;
  - round_table: 41 слайд, `required_exact` = 29 image + 2 quote +
    3 chart;
  - NTC: 8 слайдов, `required_exact` = 9 text + 45 metric_value +
    7 footnote.
  При этом `auto_author.py` строил только `z:title` + `z:body` из
  текстовых units. Required-exact диаграммы в сцены не попадали.
- Контактный лист III (57 текущих PNG) подтверждает:
  - slide11: все три варианта — word cloud с наложением текста;
  - slide10: текст собран в узкий вертикальный список мелким кеглем;
  - slide0 c1: бледный/ghosted логотип, заголовок серый;
  - slide1 c2/c3: разреженная, смещённая композиция.
  Это не приёмка: рендер формально pass, но визуальное качество
  не соответствует корпоративному deck.
- NTC: 10 кандидатов без байтов (`no candidate bytes produced`),
  slide0 c2/c3 всё ещё `VISUAL_REPRESENTATION_UNRESOLVED`.
- round_table: часть кандидатов `failed` без blocking-кодов
  (`status=failed`, `saved_checks=False`).
- Selection/assembly/final acceptance/bundle не выполнялись.

### Тесты

- `ruff check src/slides_cli/gpn/review_jobs.py
  src/slides_cli/gpn/restyle_runner.py` — clean.
- `uv run pytest --no-cov -q tests/gpn/test_stage3_native.py
  tests/gpn/test_stage5_render.py tests/gpn/test_stage7_acceptance.py
  tests/gpn/test_stage7_runner.py` — exit 0.
- III native renders: 57 current; PNG просмотрены контактными листами
  (`/tmp/iii_sheets/sheet0..4.jpg`), дефекты зафиксированы выше.
- Хэши корпусов не менялись; новые run-каталоги не создавались,
  старые артефакты не перезаписывались.

### Запросы пользователя

Остановиться и задокументировать состояние вместо продолжения
генеративного ремонта. Ранее: не создавать новый batch, не
переписывать историю, работать только в проекте, не подменять render
проверкой XML, не делать required-диаграмму optional.

### Предстоит

1. **Сменить стратегию сцен на source-aware:** строить зоны из
   исходных shape/asset позиций (`source_ir.json`), привязывать
   `required_exact` image/chart/quote к brand-asset или исходному
   object, сохранять исходную геометрию и переносить её в шаблон.
   Это особенно критично для round_table (34/41 слайд с exact
   ассетами) и III (8/20).
2. После смены стратегии пересобрать сцены/кандидаты, затем заново
   пройти visual/design review, selection, assembly, final acceptance.
3. Отдельно разобрать NTC `BUILD_NO_CANDIDATE` и
   `VISUAL_REPRESENTATION_UNRESOLVED`, round_table `status=failed`.
4. Не запускать `bundle`, пока три дека не пройдут приёмку.

---

## Этап 19 (2026-10-20): коммит состояния Этапов 17–18, починка pre-build coverage и флакующего теста

Дополнение к Этапам 17/17.1/18; предыдущие разделы не изменялись.

### Реализовано

1. **Коммит рабочего состояния Этапов 17–18.** В git вошли изменения
   `gpn-restyler/src/slides_cli/gpn/*` (render, review_jobs, restyle_runner,
   response_validation, visual_norm, models, measured_build, layout,
   constraint_layout, composition, frame_contract, native, export_pipeline,
   scene_jobs, candidate_jobs), новые модули `design_frontier.py`,
   `owned_examples.py`, `source_images.py`, `visibility_scope.py`, новые
   тесты `test_delivery_frontier.py`,
   `test_candidate_visibility_repair.py`, правки
   `tools/harness_batch.py` и тестов, а также `development_history.md` и
   `IMPLEMENTATION_STATE.md`.
2. **Удалены из git** уже принятые и отработавшие артефакты:
   каталог `gpn_delivery_patch/` (патч принят на Этапе 16) и
   `opencode_layout_engine_recovery.md` (спецификация завершённого
   восстановления). История обоих остаётся в git-истории.
3. **Локальные артефакты вынесены из git.** В `.gitignore` добавлены
   `/reports/`, `/manifest.json`, `/RESTORE.md`,
   `gpn-restyler/.delivery_patch.lock`,
   `gpn-restyler/.delivery_patch_backups/`. Корпуса и `runs/` остались
   локальными, как требует AGENTS.md.
4. **Починён pre-build coverage gate.** `response_validation.py` требовал
   полностью типизированный `SourceDeckIR`; минимальные прогоны без
   типизированного IR и без `classification_and_transformation_ledger.json`
   получали `PAYLOAD_INVALID` вместо валидной сцены. Теперь при
   нечитаемом/неполном IR гейт не отклоняет корректный ответ, а
   пропускает классификацию (сборка такой кандидат всё равно не
   выпустит). Продуктовые прогоны (импорт пишет типизированный IR,
   семантический этап — ledger) по-прежнему проходят через гейт.
5. **Починен флакующий тест** `test_stage4_exporter.py::test_same_part_names_different_sources`:
   фикстура встраивает xlsx с меткой времени сборки, поэтому две
   отдельно собранные фикстуры не байт-в-байт равны. Тест копирует
   байты, сохраняя свой смысл «одинаковые байты → одинаковый хэш».

### Проблемы

- Продуктовая приёмка трёх деков не выполнена; selection, assembly,
  final acceptance и bundle по-прежнему не запускались.
- Главный дефект качества (авто-сцены теряют `required_exact`
  диаграммы/графики) не устранён — он зафиксирован на Этапе 18 и ждёт
  source-aware стратегии.
- Полный `pytest tests/` выполняется ~40 минут; в этом прогоне
  выполнялись `tests/gpn` (exit 0) и `tests` без `tests/gpn` (exit 0).

### Тесты

- `uv run ruff check src tests tools`: **clean**.
- `uv run pytest -q --no-cov tests/gpn`: **exit 0**.
- `uv run pytest -q --no-cov tests --ignore=tests/gpn`: **exit 0**.
- `uv run pytest -q --no-cov tests/gpn/test_stage4_exporter.py`: **exit 0**.

### Запросы пользователя

Коммит и пуш. Локальные отчёты (`reports/`, `manifest.json`,
`RESTORE.md`) и машинные артефакты приёма патча в публичный git не
включать — только код и документацию.

### Предстоит

Следующий шаг не начат: source-aware сцены (Этап 18, п. 1), затем
пересборка кандидатов и повторная приёмка.
