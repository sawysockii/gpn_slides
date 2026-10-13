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
