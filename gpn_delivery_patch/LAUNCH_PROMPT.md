# Выполнить кодовый патч и довести существующий batch до реальных результатов

Ты продолжаешь уже разрешённую работу. Не начинай новый архитектурный этап и не
предлагай пользователю ещё один план вместо исполнения. В пакете лежит код,
который нужно применить, проверить и использовать на реальных данных.

Рабочие пути:

```text
project_root = /Users/wysockii/Documents/gpn_slides
repo_root    = /Users/wysockii/Documents/gpn_slides/gpn-restyler
patch_root   = /Users/wysockii/Documents/gpn_slides/gpn_delivery_patch
batch_root   = /Users/wysockii/Documents/gpn_slides/runs/recovery-batch-20261016T173142Z
```

Продолжить ТРИ существующих кейса:

| Кейс | Исходный файл | Run |
|---|---|---|
| ntc | `NTC_business_AI_GPN.pptx` | `batch_root/ntc/run` |
| round_table | `Круглый_стол.pptx` | `batch_root/round_table/run` |
| iii | `ИИИ.pptx` | `batch_root/iii/run` |

Имя `Круглый_стол.pptx` может храниться в NFD. Сопоставляй имена через Unicode NFC,
затем проверь SHA-256 байтов против `input_spec`/snapshot существующего run.
Не создавай другой кейс из-за различия нормализации имени.

## 1. Границы, которые уже решены

1. Основной режим — `harness`: все смысловые/дизайнерские ответы создаёт ТЕКУЩАЯ
   онлайн-модель этого харнесса. Ты и есть исполнитель этих запросов. Локальный
   LLM-сервер сейчас не поднимаем; интерфейс `local_server` сохраняем для будущего.
   Не добавляй ключи, креденшелы и фиктивный endpoint для вызова самого себя.
2. `project_root/ontology` — обязательные правила. Размеры, роли, цвета, политика
   шрифтов, списки и ограничения берутся из актуального JSON и его проверенного
   compiled snapshot. Не создавай таблицы соответствий придуманных имён
   `metric_value`/`metric_caption` корпоративным стилям в Python.
3. Примеры оформления — только `project_root/slide_examples`. Исходный PPTX —
   источник содержания и доказательств, а не нормативный образец оформления.
   agent-slides может оставаться основой кода; его примеры, сторонние стили,
   темы, демонстрации, ссылки на внешние слайды и автоматически загружаемые
   референсы исключены. Пресеки такие переходы скиллов и fallback-механизмов.
4. Оригиналы PPTX, ontology, slide_examples и бинарники шрифтов не менять.
   Шрифты уже установлены. При ошибке discovery исследуй реальные пути и
   привязки, включая FontBase; не объявляй снова отсутствие шрифтов без проверки.
   Технические test-only fixtures не являются примерами дизайна и не могут
   попадать в retrieval, обучение, style policy или итоговые презентации.
5. Не делать reset/rebase/commit/push. Сохранить все чужие незакоммиченные правки.
   Работа по исправлению движка и продолжению этого batch уже разрешена.
   Не спрашивать «стартовать следующий stage?» или «продолжать?».
6. Инструмент универсальный. Нельзя добавлять условия по имени/хэшу NTC,
   количеству его слайдов, номеру слайда или конкретным словам. Эти три файла —
   проверочный корпус, а не ветви реализации. Режим создания из документа/
   промпта должен сохранять общий downstream; его smoke проверяется после
   запуска трёх основных кейсов и не вытесняет их из очереди.

## 2. Применить код с сохранением локальных изменений

Сначала прочитай `patch_manifest.json`, `IMPLEMENTATION_NOTES.md`, локальный
AGENTS.md и git diff/status. Не исполняй скрипты из run/harness в качестве модели.

Из `repo_root` выполнить preflight установщика:

```bash
python3 /Users/wysockii/Documents/gpn_slides/gpn_delivery_patch/APPLY_PATCH.py \
  --repo /Users/wysockii/Documents/gpn_slides/gpn-restyler \
  --report /Users/wysockii/Documents/gpn_slides/gpn_delivery_patch/install_preflight.json
```

Если `status=preflight_ok`, применить:

```bash
python3 /Users/wysockii/Documents/gpn_slides/gpn_delivery_patch/APPLY_PATCH.py \
  --repo /Users/wysockii/Documents/gpn_slides/gpn-restyler --apply \
  --report /Users/wysockii/Documents/gpn_slides/gpn_delivery_patch/install_result.json
```

Если есть конфликт, это задача агента по интеграции, а не вопрос пользователю.
Установщик не записывает ни одного проектного файла при конфликте. Сопоставь
`baseline/<path>`, `payload/<path>`, текущий файл и `changes.patch`. Перенеси
изменения по функциям с сохранением сторонних правок. Не заменяй весь файл
вслепую, не подгоняй baseline hash и не удаляй проверку конфликта. Сохрани
diff интеграции и причины решений в `patch_integration_report.json`.

Убедись, что реальный CLI импортирует код ИЗ ЭТОГО checkout: выведи
`restyle_runner.__file__`, `layout.__file__`, версии движка и OR-Tools. Исправь
editable install/PYTHONPATH, если запускается другой checkout. Не объявляй патч
использованным только на основании наличия файлов.

Проверь зависимость:

```bash
uv run python -c 'from ortools.sat.python import cp_model; import ortools; print(ortools.__version__)'
```

Если OR-Tools отсутствует, добавь `ortools==9.15.6755` в зависимости проекта через
`uv add`, сохрани изменения pyproject/lock. Если совместимая версия уже есть,
не переустанавливай весь environment. Для браузера используй существующие
`tools/layout/package.json` и lockfile: сначала проверь установленный Playwright
и Chromium, при необходимости `npm ci` и штатную установку Chromium из этой
папки. Не заменяй реальный измеритель заглушкой при ошибке установки/запуска.

Прогнать:

```bash
uv run pytest tests/gpn/test_delivery_patch.py tests/gpn/test_delivery_dispatch.py tests/gpn/test_delivery_native.py tests/gpn/test_runner_reissue_idempotence.py tests/gpn/test_stage7_runner.py --no-cov
uv run ruff check src/ tests/ tools/harness_batch.py harness/model_loop.py
```

Затем полный требуемый проектом suite на Mac. Тесты `test_delivery_native`
используют test-only шрифт; при различии доступных технических шрифтов адаптируй
fixture к реально установленному face, не корпоративную политику и не гейты.
Все реальные продукты строятся с GPN faces из production inventory.

Не объявляй старый тест зелёным, удалив причинную проверку. В этом патче
устаревшее ожидание «stale ответ создаёт новый ID» намеренно заменено на
«ошибочный ответ не меняет вопрос; тот же pending request можно исправить».
Проверки profiling/шрифтов остаются в своих интеграционных модулях; runner unit
tests изолируют только границу профиля, что явно написано в fixture.

## 3. Принять патч в существующие runs

Не начинай три новых run вместо продолжения уже созданных. Сохрани копии
run_state и хэши обязательных корпусов ДО активации. Убедись, что все три state
действительно относятся к трём разным исходным файлам, а не к одному NTC.

Из repo_root:

```bash
uv run python tools/harness_batch.py --batch-root /Users/wysockii/Documents/gpn_slides/runs/recovery-batch-20261016T173142Z register --expected-run ntc/run --expected-run round_table/run --expected-run iii/run
uv run python tools/harness_batch.py --batch-root /Users/wysockii/Documents/gpn_slides/runs/recovery-batch-20261016T173142Z activate
uv run python tools/harness_batch.py --batch-root /Users/wysockii/Documents/gpn_slides/runs/recovery-batch-20261016T173142Z activate --apply
uv run python tools/harness_batch.py --batch-root /Users/wysockii/Documents/gpn_slides/runs/recovery-batch-20261016T173142Z advance
```

Активация:

- сохраняет старые packets/responses и историю;
- повторно валидирует authored_plan, связывает роли и порядок новым lowering;
- архивирует старый plan перед изменением; невалидный plan убирается из
  исполняемой позиции в run, а не из истории;
- заменяет устаревшие pending scene tasks новыми immutable заданиями v3 с
  актуальным JSON-контекстом; старые получают `superseded`;
- не выдаёт старый ответ за новый и не меняет его request_id/fingerprint;
- не начинает повторно semantic analysis для уже разобранного файла;
- оставляет round_table и iii на их настоящих semantic задачах, которые ТЫ
  должен выполнить; повтор с той же версией патча не меняет state.

Если активация находит старый повреждённый state, восстанови его по manifest,
очереди и потреблённым ответам. Сохраняй отрицательную историю. Не присваивай
`accepted`, `materialized`, `render_verified` вручную для разблокировки.

## 4. Непрерывный цикл текущей модели

Начать:

```bash
uv run python tools/harness_batch.py --batch-root /Users/wysockii/Documents/gpn_slides/runs/recovery-batch-20261016T173142Z next
```

Результат содержит абсолютный `work_order`. Прочитай этот JSON, его packet,
динамическую schema, нормы и все `required_images`. Если картинки ещё не
существуют, выполни предусмотренный реальный source render и почини путь;
не утверждай, что посмотрел отсутствующие изображения.

**Критически:** `model_work_required`/`awaiting_harness` — это не блокер и не
разрешение закончить работу. Это следующее действие для тебя как активной модели.

Для каждого work order:

1. По packet понять семантику/дизайн именно этого кейса и слайда. Прочитать
   фактические JSON-правила, полный текст и изображения. Нельзя копировать
   архивный ответ NTC в другой слайд или запускать `semantic_author.py`,
   `concepts_author.py`, `scenes_author_v7.py`, `author_plans.py` как LLM.
2. САМОСТОЯТЕЛЬНО создать ответ текущей моделью. Можно использовать Python для
   сериализации уже принятых моделью решений, извлечения источника и проверки
   структуры; нельзя выдавать эвристический скрипт за модельного автора.
3. Сохранить полный envelope из `response_envelope` в `response_path`, заполнив
   `payload` и честный `actual_model`. Если точное имя модели не сообщается
   средой, так и написать; не придумывать имя провайдера. Копировать ID/schema/
   fingerprint только из ЭТОГО work order. Не переподписывать старые ответы.
4. Выполнить:

```text
uv run python tools/harness_batch.py --batch-root <batch_root> submit <response_path>
```

5. При `response_repair_required` исправить содержимое ответа по точной ошибке
   и отправить снова к тому же immutable вопросу. Не менять queue/state/ID.
6. При consumed перейти к следующему `next`, затем снова выполнить работу.
   Dispatcher вращает кейсы и первым обслуживает ещё не тронутые semantic
   задачи round_table/iii. Не выключай эту политику ради удобства NTC.
7. Если модельных заданий нет, вызвать `advance` для детерминированной части,
   затем `next`. Если состояние не меняется и нет работы, искать ПЕРВЫЙ
   неисполненный переход по runtime trace, исправить общий код и добавить
   причинный regression. Не крутить бесконечный resume без изменения входа.

Не завершать назначение после первого исправленного слайда, первого consumed
ответа, принятого plan или passing unit suite. Продолжение всей этой работы уже
разрешено. При реальном лимите среды сохраняется честный checkpoint и точная
следующая операция; завершение продукта при этом не заявляется.

## 5. Как делать сцены и обрабатывать layout failures

Все namespace в `semantic-zones/v3` имеют конкретный смысл:

| Поле | Значения |
|---|---|
| `zones[].content_refs` | ID семантических units текущего SemanticSlide |
| `zones[].ref_roles` keys | Те же semantic unit IDs |
| `ref_roles` values, `zone.role` | Точные style tokens загруженного JSON |
| `reading_order`, `dominant_ref` | zone IDs |
| `relations.from_ref/to_ref` | Semantic unit IDs |
| `relations.evidence` | Включает исходный relation ID |
| `paragraph_groups` | Группы semantic refs, которые образуют один абзац |
| `inline_separator` | Только пустая строка или один пробел |

Lowering один раз переводит semantic refs в source IDs и сохраняет все
назначения ролей. Не правь одновременно authored и lowered namespaces вручную.
Например, значение и подпись могут иметь разные `ref_roles` в одном native
блоке. Нельзя дать всей карточке large_metric только потому, что в ней есть
число. Семантический тип `metric_value` сам по себе не является style token.

`paragraph_groups` отделяет новую структуру абзацев от дробления исходного
PPTX. Три текстовые фигуры одной фразы могут стать одним абзацем, а значение
и пояснение — двумя абзацами одного блока. Для явного объединения нескольких
исходных объектов каждый должен иметь один однозначный не-списочный абзац;
иначе запрос выдаёт `TEXT_FLOW_AMBIGUOUS_PARAGRAPHS`, а не схлопывает списки
в строку. Сохрани исходные абзацы отдельными группами или расширь общий span
контракт с тестом, если реальный источник требует более тонкой адресации.

Измеритель и exporter используют ОДНУ проекцию `text_flow.py`. В output map
сохраняемые paragraph atoms адресуются как `para[i]/chars[start:end]`.
Проверка после reimport сравнивает точный диапазон, а не ищет подстроку
где-нибудь на слайде и не сравнивает каждый исходный абзац со всем блоком.
Run IDs сохраняются для измерений/ссылок, но не выдаются за несуществующие
paragraph atoms. Поля остаются `a:fld`, переносы — `a:br`, ссылки сохраняются.

Для каждого слайда создать несколько РАЗНЫХ визуальных объяснений: меняется
топология, доминанта, крупные зоны/маршрут чтения, а не только зазоры. Отбирать
по настоящим PNG в корпоративной типографике. Минимум для comparative gate:
два различных отрендеренных и просмотренных варианта и один пригодный к выбору.
Отклонённый вариант сохраняется с причиной. Простой текстовый дамп при чистой
геометрии должен получить repair/reject по design review.

На `INFEASIBLE` или overflow действовать в таком порядке:

1. Проверить, что исполняется новый вызов layout, а не старый source-stack
   путь. Runtime counters должны показать настоящий solver call. Проверить
   правильный frame, роли, exact font evidence, структуру paragraph_groups,
   реальные ширины и метрики. Не заключать «контента много» по числу shapes.
2. Сопоставить authored → lowered → components → measured requests → chosen
   variants → resolved → saved OOXML. Найти первый расходящийся переход.
   Особенно проверить ref_roles в ResolvedSlide и текстовом эмиттере.
3. OR-Tools CP-SAT решает размещение совместно: позиции и варианты ширина/
   высота, где КАЖДАЯ высота измерена реальным Chromium на этой ширине. Нельзя
   взять ширину одного варианта и высоту другого. Учитываются разрешённые
   вложенные поверхности, препятствия и направление чтения; не назначается
   новый отступ за каждый исходный объект. Таблицы/чарты со своим контрактом
   измеряются и проверяются отдельно.
4. Если измерение на финальной ширине отличается — повторить измеренную
   constraint-модель в ограниченном бюджете. Никаких скрытых gap=7.2 за shape,
   down-only стеков и эвристической упаковки всех объектов в одну колонку.
5. Если конечный набор вариантов/ограничений неразрешим, сохранить именно
   этот scope доказательства и передать сцену на осмысленную переработку:
   объединить фрагменты, поменять сетку, выделить тезис/сравнение/процесс,
   сделать подписи компактнее за счёт структуры, изменить распределение
   площадей. Не уменьшать нормативный кегль, не выкидывать текст и цифры,
   не сдвигать обязательный футер и не ставить autofit.
6. Нельзя передавать сбой c1 на все другие варианты слайда или сбой одного
   слайда на весь дек. Каждый candidate job содержит один source slide и
   один candidate ID, собственные evidence и input/code fingerprint.

Панель должна оставаться под своим текстом, включая объединённые блоки.
Семантическая связь НЕ разрешает overlap. Разрешение задаётся явно, с
типизированным underlay/content, scope и evidence. Нет оснований — нет pass.
Условия цветов, контраст и брендовые поверхности сверяются с ontology;
байтовая сохранность белого логотипа на белом не является хорошим результатом.

## 6. Review, repair, selection и финальная сборка

1. Рендер — из сохранённого candidate.pptx через настоящий LibreOffice.
   HTML/DOM служит измерителем, а не доказательством вида итогового PowerPoint.
   Hidden slides включены в экспорт PDF для проверки; show/hidden в PPTX
   сохраняются. Ожидаемое число страниц у single-slide job равно одному.
2. Cache учитывает candidate bytes, код и среду. Если байты/план/шрифты/
   движок изменились, старый pass не переносится. Повторный idle resume
   не должен снова рендерить неизменившиеся варианты и расходовать бюджет.
3. Открыть ВСЕ required PNG. Visual review возвращает точное покрытие страниц
   и SHA из выданного manifest. Design review отвечает по всем объявленным
   dimension families: смысл/связи, иерархия, группировка/ритм, пропорции/воздух,
   кодирование/данные, баланс доказательств и текста, корпоративный язык,
   согласованность и разнообразие дека. Замечание адресовать source_slide_id
   в `findings.scope`; не оставлять голое «сделать красивее».
4. На repair система выдаёт полную задачу: прежняя сцена, семантика, текущие
   нормы, реальные PNG и конкретные findings. Исправляй причину. Новая попытка
   получает новый ID, вычисленный после включения evidence в immutable packet.
   Ошибочный schema-ответ исправляется к прежнему вопросу без новой попытки.
5. Изменение плана/кода вызывает отдельную новую сборку этого кандидата.
   Старые candidate/render/reports сохраняются в `candidate_history`.
6. Выбор возможен только после покрытия ВСЕХ ожидаемых source slides. Для
   каждой SelectionEntry нужны правильные slide/candidate IDs, реальный
   SHA/path выбранного candidate, рассмотренные альтернативы и причина.
   Нельзя выбрать c1 первого слайда по совпадению плоского имени `c1`.
7. Финальный дек собирается из выбранных сцен, проверяется на сохранённых
   байтах, рендерится целиком и получает СВЕЖИЕ reviews именно своих байтов.
   Review отдельного слайда не заменяет просмотр итогового дека.
8. Замечания к final направляются обратно выбранным сценам по scope; после
   исправлений выполняется новая сборка и новые гейты. Не публиковать вариант
   со старым `accepted=true`, не подписывать acceptance от другого SHA.
9. `product_accepted` и публикацию выставляет только настоящий общий acceptance
   path. Missing evidence и unknown блокирующего scope не заменяются pass.
   Редактируемость проверяется native структурой и reopen/edit/save; PowerPoint
   UI roundtrip отмечается отдельно по реально выполненному scope. При наличии
   PowerPoint можно проверить отдельную копию, не исходник и не чужие документы.

## 7. Что проверить на трёх входах

Не ограничиваться NTC. На каждом кейсе показать runtime chain:

```text
input bytes → semantic/model response → brief → distinct concepts
→ scene responses → native candidate jobs → actual measurements/solver
→ saved bytes → PDF/PNG → visual/design findings → repairs/selection
→ complete corporate PPTX → final native render → acceptance
```

Сверить число/порядок/hidden слайдов, обязательные тексты/числа/условия,
изображения, таблицы, native charts/workbook, связи и поля в declared scope.
Конструкцию вне поддержки не превращать в пропуск; сначала применить
сохраняющую данные стратегию, иначе точный блокер с source object ID.

После основного корпуса выполнить причинные проверки универсальности:
другой текст/JSON без правки Python, дробление фразы на большее число исходных
объектов при неизменной target paragraph grouping, технический dense input,
один create-from-text smoke через тот же downstream. Эти проверки не являются
дизайнерскими референсами и не должны менять реальные источники.

## 8. Когда можно закончить и что прислать на следующий гейт

Проверить:

```bash
uv run python tools/harness_batch.py --batch-root /Users/wysockii/Documents/gpn_slides/runs/recovery-batch-20261016T173142Z finish
```

Exit 0 требует всех зарегистрированных кейсов, существующих опубликованных
файлов и совпадения их SHA с реальным accepted продуктом. Пустой state или
один завершённый NTC не является завершением batch.

Собрать проверочный пакет:

```bash
uv run python tools/harness_batch.py --batch-root /Users/wysockii/Documents/gpn_slides/runs/recovery-batch-20261016T173142Z bundle
```

В `delivery_review.zip` ОБЯЗАТЕЛЬНО нужны:

- Для каждого кейса: run_state, activation report, candidate_attempts,
  comparison coverage (если есть), selection_report, product_acceptance,
  publication_manifest, runner_transition_log.
- Три фактических corporate PPTX, их полные native PDF и все финальные PNG;
  для незавершённого кейса — не фиктивный финал, а текущий candidate и PNG
  конкретных блокирующих вариантов плюс candidate_evaluations/layout attempts.
- content_diff, part_preservation_diff, saved_authoring_rule_report,
  saved_geometry_report, saved_measurement_report, required_visibility_report,
  native_editability_report, ontology_rule_coverage, saved_frame_report и
  gate_reports по финальным байтам.
- Current-hash visual/design reviews вариантов и финала, render manifests;
  актуальные pending packets и rejected/preflight receipts, если остались.
- Реально исполняемые изменённые файлы кода, `patch_integration_report.json`
  при ручном переносе, install_result.json, список команд и полный вывод тестов/
  ruff, хэши корпусов до/после и manifest всего пакета.

Команда bundle собирает базовый набор автоматически. Проверь полноту по этому
списку; недостающие scope artifacts/тестовые логи добавь программно, затем
пересчитай manifest. Не присылай только рассказ о выполнении без этих файлов.
Не включай в пакет бинарники лицензируемых шрифтов и лишние копии корпусов.

Финальный ответ пользователю: три ссылки на реальные результаты, компактно
что проверено и что честно осталось непроверенным, ссылка на `delivery_review.zip`.
Если реально остаётся блокер — точная причина, выполненные попытки и пакет
доказательств. Не называй работу завершённой по числу passing unit tests.
