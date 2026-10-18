# Что входит в кодовый патч

Это изменения существующего `gpn-restyler`, а не новый проект и не готовые
конвертированные презентации. База восстановлена из предоставленных
`review_increment_universal_authoring.zip` и более новых файлов
`reissue_loop_gate.zip`. Последний архив проверен по всем 21 хэшам manifest.
Полного Mac checkout в среде автора нет; неизменённые upstream модули не все
присутствуют в диагностическом снимке.

## Исполняемые изменения

| Файл / граница | Реализация |
|---|---|
| `response_validation.py` + `consume_response` | Валидация содержимого до commit запроса. Ошибочный ответ оставляет тот же request pending. Проверяются роли, источник, semantic ledger, выбор и идентичность review. |
| `scene_contract.py` | Разделение semantic / zone / source namespaces; сохранение per-ref ролей и reading order; явный отказ на неоднозначную ownership; связи не становятся разрешениями overlap. |
| `text_flow.py` | Единая проекция source spans для измерителя и эмиттера. Явные paragraph_groups позволяют объединять фрагменты в строку без налога на количество исходных shapes. Строгий measurement wire получает только свои поля. |
| `composition.py`, `layout.py`, `export_pipeline.py` | Полные source IDs вместо обрезанных префиксов; устранение лишних bindings удалённых компонентов; роли и paragraph_groups сохраняются через ResolvedSlide до эмиттера. |
| `frame_contract.py` | Общий resolver frame вынесен без изменения алгоритма из legacy patch bridge; layout больше не зависит от импорта старого operation API ради вычисления frame. Bridge реэкспортирует тот же helper. |
| `constraint_layout.py`, `measured_placement.py` | Настоящий OR-Tools CP-SAT: измеренные варианты ширина/высота, совместное размещение, препятствия, разрешённые вложенные поверхности, направление чтения, численная постпроверка. Нет shrink/drop/autofit и подмены INFEASIBLE доказательством глобальной плотности. |
| `layout.py` | Основной allocation и refinement вызывают новый solver. Удалён активный down-stack/gap-per-object поиск. Учтены реальные solver counters и изменение кода в cache key. |
| `candidate_jobs.py` | Отдельный job на один slide/candidate, без ожидания целого дека. Fingerprint учитывает plan, snapshots и код; без fallback на чужой plan. Исторические варианты сохраняются. |
| `native.py` | Нативные rich paragraphs/runs с разными ролями, корректные единицы spacing, поля, soft breaks, ссылки и baseline markers. Ledger связывается с точными диапазонами абзаца, а не с выдуманными run atoms. |
| `export_pipeline.py` | Проверка точных character spans после reimport; адресация нескольких hyperlinks в объединённом абзаце; underlay привязывается к итоговому объединённому блоку. |
| `tools/layout/src/measure.js` | Идентификаторы параграфов/runs больше не разбираются через split(':'); используется точное сопоставление DOM attributes без подстановки source IDs в CSS selector. |
| `review_jobs.py`, `render.py` | Нативный render текущих байтов, проверяемый cache, source scope page count, actual required_images в immutable review packet; hidden slides входят в PDF без изменения hidden flags исходного PPTX. |
| `scene_jobs.py` | Полный repair packet с семантикой, ролями, прошлой сценой и evidence, включёнными ДО fingerprint; один активный запрос на candidate. Точные финальные findings возвращаются выбранным сценам. |
| `restyle_runner.py` | Исправлен typed result финальной сборки, покрытие selection, provenance кандидатов и свежие final reviews. Отсутствующие evidence не превращаются в pass. Ошибка typed Issue.details возвращает диагностический статус вместо AttributeError. |
| `patch_activation.py` | Явная, повторяемая активация нового контракта в старых runs: история остаётся, старые вопросы superseded, старые ответы не переподписываются. |
| `tools/harness_batch.py` | Fair dispatcher текущей модели, preflight, submit через публичный CLI, register/activate/advance/finish/bundle. Не вызывает run-local author scripts. Нет сервера, API-ключей или имитации вызова модели. |
| `harness/model_loop.py` | Совместимый вход в тот же dispatcher вместо прежнего AUTHORS runner. |

Основной runtime путь:

```text
public resume
  → envelope + payload validation
  → immutable accepted scene
  → semantic/source lowering
  → composition + typed text projection
  → real measurement callback + CP-SAT
  → resolved roles/flow/geometry
  → native exporter
  → reimport + exact source preservation
  → native PDF/PNG
  → current-model reviews / evidence-based repairs
  → complete selection
  → full assembly + fresh final render/reviews
  → actual acceptance + publication
```

## Доказательства и пределы проверки

Точные результаты, версии, число тестов и команды находятся в
`evidence/verification.json`, `pytest.log` и `pytest.xml`. Это выбранные
причинные/регрессионные suite на предоставленном снимке, не полный Mac suite.
Unit tests не заменяют визуальную приёмку трёх ваших деков.

Проверены реальный native emitter, сохранение/reimport OOXML, поля/ссылки/
переносы/сноски, диапазоны объединённого текста, обнаружение удаления слова,
per-run типографика, geometry сохранённых объектов, программное edit/save/reopen,
реальный LibreOffice PDF/PNG на test-only fixtures, включая hidden slide.
Проверены реальные вызовы CP-SAT и production call sites, а также безопасное
применение патча: dirty changes, конфликт, rollback и повторный запуск.

Есть отдельная интеграционная проверка source → composition → строгий
measurement request → реальный solver. В ней измеритель является явно
test-only protocol double; это не заявляется Chromium-fit доказательством.
В production test-only confidence не принимается новым placement helper.

Существующий semantic response NTC отдельно проверен на реальном source_ir:
327 объектов, 327 transformation decisions, payload preflight без ошибок.
Воспроизведение состояний очередей подтверждает, что два других кейса
находились на semantic_analysis без обслуженных запросов. Это не считается
выполнением их модели или конвертацией.

В этой среде НЕ выполнены:

- полный CLI на полном Mac checkout;
- настоящий Chromium с GPN faces: загрузка браузера в этой среде не удалась,
  поэтому нет подставленного browser success;
- реальные три презентации целиком с GPN fonts и всеми альтернативами;
- PowerPoint UI editing и окончательная корпоративная визуальная приёмка;
- полная проверка всех конструкций старого проекта, OCR/create и шрифтовых
  метрик target PowerPoint. Неподтверждённые возможности остаются по support matrix.

Первый более широкий пробный набор также упёрся в отсутствующие ontology
corpora; одна найденная при этом настоящая ошибка `Issue.message` исправлена
и покрыта отдельным regression. Корпуса ради тестов не были сфабрикованы.
`LAUNCH_PROMPT.md` требует полного Mac запуска, а не переноса локальных test-only
флагов в production readiness.

## Применение и проверочный архив

`APPLY_PATCH.py` проверяет payload/base hashes и все файлы до записи. Неизменённая
база заменяется атомарно; локальные правки объединяются только по однозначному
точному контексту. При конфликте весь preflight останавливается без записи
проектного кода. Есть резервные копии и rollback записи. Новые файлы с чужим
содержимым не перезаписываются. Контексты не подгоняются под желаемые хэши.

Если текущий Mac код уже ушёл дальше снимка, установщик может отказать в
автоматическом переносе. Это предусмотренный путь: агент переносит diff с
сохранением текущих изменений и запускает тесты. Без доступа к его диску нельзя
честно обещать автоматический clean merge любого более нового checkout.

`bundle` включает финальные файлы, state, IR/ledger/rules, основные saved-byte
reports, reviews, pending packets, код и логи из `batch_root/delivery_checks`.
Для незавершённых кандидатов большие диагностические байты берутся по одному
текущему примеру на различный набор failure codes; evaluations/manifests/reviews
сохраняются для всех. Если причина относится к другому конкретному варианту,
агент добавляет именно его candidate/PNG/trace, как указано в launch prompt.
Не включаются font binaries, чужие reference corpora и полные historical caches.
