# GPN Restyler — Stage 4: native exporter и OPC transplant

Версия 1.2, 2026-10-07. Задание для OpenCode; не отчёт о проверке кода на Mac. Уточнены два режима LLM, текущий default и запрет чужих примеров оформления.

## 0. Задание, приоритет и границы

Выполни Stage 4 до сохранённого, повторно импортированного PPTX и проверяемого отчёта. Создай полноценный exporter для поддержанных типов, который переносит содержимое в новый deck на корпоративном шаблоне. Используй существующий fork agent-slides и Stage 1–3.5; не создавай второй независимый движок.

Проект:
- `project_root=/Users/wysockii/Documents/gpn_slides`.
- `code_root=/Users/wysockii/Documents/gpn_slides/gpn-restyler`.
- Онтология: `project_root/ontology`, JSON и нормативные Markdown — обязательный источник правил.
- Примеры: `project_root/slide_examples`; оформление примеров подчинено онтологии.
- Согласованная база agent-slides: `820782a5fb8cb8f251d7c0750e4299c321cd068c`.
- Ветка по предыдущим отчётам: `gpn-restyler`. Проверить фактически.
- Предыдущий run по отчёту: `runs/stage3_5/2026-10-07-audit/`; пути generation/native artifacts найти по manifest, не угадывать.

**У LLM должны быть два режима работы:**

| Режим | Значение provider | Исполнение LLM-заданий |
|---|---|---|
| **1 — локальный сервер** | `local_server` | Приложение обращается к настроенному локальному LLM-серверу через отдельный adapter |
| **2 — модель харнесса** | `harness` | Текущий харнесс выполняет задания своей текущей онлайн-моделью и передаёт результаты в приложение по JSON-контрактам |

**Сейчас по умолчанию работаем в режиме 2: `provider="harness"`.** Использовать существующее поле provider в конфигурации, не создавать дублирующие mode/provider настройки с независимыми значениями. Отсутствующее поле означает harness; неизвестное значение — configuration error.

Оба режима используют одинаковые PlanningContext/ответные JSON-контракты, проверки ссылок и онтологии, resolver и native exporter. Различается способ получения ответа модели; не создавать отдельный pipeline для каждого режима.

В Stage 4 выполняй необходимые семантические задания средствами текущего харнесса через его онлайн-модель. Локальный LLM-сервер, HTTP model client, endpoint discovery и credentials сейчас не реализовывать и не требовать. Режим 1 предусмотреть в конфигурационном контракте и архитектуре как отложенный adapter; его readiness не заявлять. Явная попытка генерации через ещё не реализованный режим 1 должна вернуть понятный deferred/not_implemented результат, без автоматического переключения на режим 2.

Переключение режима только явной настройкой; не менять модель/provider и не устраивать скрытый fallback. Продолжай Stage 4 в режиме 2, не откладывай его ради реализации режима 1.

Обязательное различие telemetry для текущего режима 2:
- Приложение не делает model HTTP requests.
- Harness использует свою онлайн-модель.
- Записывать выбранный provider и фактический origin принятого плана. В будущем режиме 1 generation owner будет local_server; существующий harness response нельзя объявить ответом локального сервера.
- Количество созданных/принятых response files не доказывает количество backend API calls. Сохранять фактически доступную provenance, не выдумывать usage/model metadata.
- Exporter работает детерминированно из принятого ResolvedSlide/плана; новый запрос модели для каждого emitter не нужен.

Исходные `ontology/`, `slide_examples/` и input decks неизменны. Новые run artifacts/cache отдельно. Сохраняй пользовательские и чужие working-tree changes. Не reset/rebase/force push.

Нормативных constants не добавлять: canvas, fonts, sizes, palette, list rules, role IDs и counts берутся из выбранного snapshot. Форматные constants OOXML/units допустимы. Таблицы прежнего задания с числами описывают конкретный snapshot.

Следующие этапы не выполнять автоматически: полноценный measurement/layout Stage 5, расширение planner Stage 6, full QA/render/repair Stage 7, document authoring/OCR Stage 8.

Однако exporter должен иметь общие emitters и для второй обязательной опции документа/текста/промпта. Создание новых charts принимать из VerifiedChartData, а не из неподтверждённых значений LLM.

Отсутствие GPN font binaries не мешает native XML/package tests и диагностическому candidate. Оно блокирует подтверждение точного вида/измерения/production. `strict_output_ready=false` сохраняется.

### 0.1. Примеры оформления — только пользовательский корпус

**Пользователь уже полностью удалил примеры agent-slides как противоречащие онтологии. Их удаление — осознанное требование, а не missing dependency. Восстанавливать, скачивать заново или заменять их другими чужими примерами запрещено.**

Единственный разрешённый корпус примеров оформления — `project_root/slide_examples` с пользовательскими файлами. Этот whitelist обязателен для pipeline, retrieval/indexing, prompts, skills и reference tools независимо от выбранного LLM-режима.

Запрещено использовать как reference:
- примеры, demo-decks, изображения/HTML/layout showcases и некомпоративные templates из agent-slides;
- built-in samples навыков и библиотек, чужие локальные reference folders;
- GitHub/web examples, remote URLs, автоматически скачанные sample packs;
- старые index/cache/packets/plans, чья example provenance относится к запрещённым источникам.

Skills не должны искать «похожие примеры» в других каталогах или интернете. Доступ к запрещённому reference должен завершаться явным `REFERENCE/FORBIDDEN_SOURCE`, а не fallback. Отсутствие/пустота пользовательского корпуса отражается в readiness; это не разрешение восстановить upstream examples.

Входная презентация/документ читается как пользовательский input для содержания, данных и структуры, но не становится нормативным образцом стиля. Пользовательские fonts/templates и производные assets допускаются по существующим provenance-контрактам; они не вводят новый корпус примеров.

Технические synthetic fixtures используются только для unit/integration checks OOXML/data/transactions. Они не попадают в reference index, model example packets или оформление, выбираемое skills. Native styles таких fixtures берутся из пользовательской онтологии. Для реального model smoke style examples берутся только из `slide_examples`.

Новые композиции по разрешённому extension contract по-прежнему допустимы: это proposed geometry в рамках правил, а не обращение к чужому примеру. Не сводить разнообразие к копированию фиксированного набора пользовательских слайдов.

## 1. Верификация Stage 3.5 и базы перед изменениями

Прочитать AGENTS.md, IMPLEMENTATION_STATE.md, SUPPORT_MATRIX, основную спецификацию §§7, 10–11, 15–17, 19–22 и Stage 3.5 с пользовательской поправкой harness.

Reported baseline: 303 passed, ruff clean; 19/19 metamorphic; 5 accepted harness responses; native diagnostic patch с 10 ops. Проверить фактически. Не считать число тестов доказательством свойств, для которых нет assertions.

### 1.1. Репозиторий

Из code_root:

```bash
git status --short
git rev-parse HEAD
git branch --show-current
git cat-file -t 820782a5fb8cb8f251d7c0750e4299c321cd068c
git merge-base --is-ancestor 820782a5fb8cb8f251d7c0750e4299c321cd068c HEAD
git diff --name-status 820782a5fb8cb8f251d7c0750e4299c321cd068c
rg --files src tests config docs scripts
rg -n 'class Presentation|def save|def to_bytes|CompiledOntology|ResolvedSlide|OutputObjectMap|Transplant|emit_|provider|source_ir' src tests config docs
```

Сохранить exit каждой команды. У ancestor check exit 1 не равен наличию ancestor. Прочитать remotes и source tree выбранного base; в отчёт вывести URLs без credentials.

Сохранить `upstream_provenance.json`: expected repository/base, actual remotes/HEAD/branch, ancestor proof, paths upstream api/model/cli, added GPN layer, differences, issues.

Если base отсутствует, сначала проверить существующие clones/metadata. При необходимости получить upstream code в отдельный cache/check-out и установить фактическое отношение деревьев, сохранив текущую работу. Не восстанавливать upstream examples/templates/showcases; checkout ограничить необходимым кодом и лицензией. Не клонировать поверх проекта и не переносить весь проект на другую базу без анализа diff.

### 1.2. Два адресных отрицательных теста

Не пересматривать весь Stage 3.5. Проверить:
1. Несогласованная копия онтологии: `marker.ooxml.buSzPct` изменён, но `relative_size_percent`/обязательный Markdown остались прежними. Должен выявляться нормативный конфликт, а не успешная смена стиля. Позитивная mutation должна менять согласованные связанные нормы в isolated fixture.
2. Candidate содержит то же число package parts, но изменён workbook payload или relationship target. Preservation должен отказать. `40/40` само по себе не proof.

Мутации — только fixtures/run copies. Corpora hashes оригиналов проверить до и после.

### 1.3. Новый run

Создать `project_root/runs/stage4/<run_id>`. Existing runs не перезаписывать.

Сохранить `audit_before.json` с реальным git/test/lint state, dependencies, corpus hashes, текущими functions, предыдущими artifacts и gaps. Исходные bugs исправлять адресно и отделять от Stage 4 changes.

### 1.4. Удалённые upstream examples: Git, skills и runtime

Выполнить эту очистку до exporter pilot, сохранив остальную текущую работу:

1. Найти по `git ls-files`, config, skill entrypoints, AGENTS.md, docs, scripts и manifests все legacy example/demo/style-reference paths. Проверять paths и callers; запрещённые примеры не читать для выбора дизайна.
2. Удалённые пользователем tracked upstream examples оформить как удаления в Git index. Сохранившиеся запрещённые tracked example assets убрать из текущего tracked tree. Использовать только точные выявленные paths, не массовый `git rm` всего assets/tests/project.
3. Добавить точные legacy paths в project `.gitignore` или эквивалентный проверяемый exclusion. Сам `.gitignore` не снимает уже tracked files — это отдельное действие. Не игнорировать пользовательский `slide_examples`, ontology, fonts/templates или входные документы.
4. Удалить ссылки/автозагрузку legacy examples из CLI defaults, skill instructions, README, scripts, templates discovery и pipeline. Project skills должны получать reference assets через GPN-команды с whitelist, не напрямую из upstream demo folders. Не изменять общие навыки других проектов.
5. Старые caches/indexes/packets/plans с чужой example provenance инвалидировать. Historic audit/debug artifacts можно сохранить как закрытые доказательства; runtime не должен загружать их как references.
6. Переиспользовать существующий corpus guard либо добавить `assert_allowed_reference(path, purpose, project_root, manifest) -> AllowedReference`. Проверять canonical resolved path внутри единственного разрешённого root, фактическую manifest entry/hash, symlinks и traversal. Производные thumbnails/descriptors допускаются только с доказанной lineage к разрешённому файлу. Сам по себе path в cache ничего не разрешает.
7. Все reference loaders, index builders, packet builders и project skill reference tools должны вызывать этот guard. Remote URLs и arbitrary alternate roots отклонять. Внешний source path в ответе модели не выполнять.
8. Обновить project AGENTS/skill instructions запретом любых иных reference sources. Если harness предоставляет read/tool hooks или ACL, добавить фактический deny для запрещённых reference paths и проверить его. Не называть общий доступ харнесса к filesystem технически заблокированным, если реализована только текстовая инструкция.
9. Выполнить отрицательный тест: навык/loader пытается получить пример из legacy upstream path, alternate folder, remote URL, symlink наружу или stale cache — request denied, fallback отсутствует. В штатном scenario используется только пользовательский corpus.
10. Сохранить `forbidden_reference_cleanup.json`: removed tracked paths, ignored paths, removed callers, invalidated caches, guarded entrypoints, deny-test results и оставшиеся gaps. Git history не переписывать; base provenance проверять по коду, а не наличию удалённых sample assets.

## 2. Файлы, модели и инварианты

Расположение определить по rg; вероятный package — `src/slides_cli/gpn/`. Сохранять public API и discovery contracts agent-slides.

| Файл | Изменение |
|---|---|
| `models.py` | NativeDeckContext contracts, typed binding/part preservation/export reports |
| `native.py` | Создание нового deck, emitters, z-order, фактические output bindings |
| `transplant.py` | Closure, names, source-qualified part map, owner-local relationships, styles/opaque parts |
| `opc_adapter.py` либо equivalent | Изолированные private python-pptx operations и final package assembly |
| `package.py` | Read/validate manifest, content types, targets, referenced parts |
| `typography.py`, `bullets.py` | Переиспользовать data-driven style/list resolution и noAutofit |
| `provenance.py`, `validation.py` | Saved-output ledger и mapped part/relationship comparison |
| `pipeline.py`, `cli.py` | build из принятого плана, diagnostic state/artifacts |
| `tests/gpn/test_stage4_*.py` | Meaningful native/transplant/negative/reopen tests |
| `docs/SUPPORT_MATRIX.md`, `IMPLEMENTATION_STATE.md` | Реальная поддержка и ограничения |
| Project `AGENTS.md`, skill instructions, `.gitignore` | Единственный reference root, запрет legacy samples и точные Git exclusions |

Не создавать duplicate implementations, если эквивалентные функции уже работают. Raw/private helpers размещать в одном проверяемом adapter, а не в каждом business emitter.

### 2.1. Основные types

Использовать существующие модели и дополнить без нарушения совместимости:

```python
from dataclasses import dataclass
from typing import Literal

@dataclass(frozen=True)
class SourcePartKey:
    package_sha256: str
    part_name: str

@dataclass(frozen=True)
class OutputShapeAddress:
    slide_part: str
    shape_id: int
    group_path: tuple[int, ...]
    kind: str

@dataclass(frozen=True)
class PartPreservationContract:
    source: SourcePartKey | None
    target_part: str | None
    mode: Literal[
        "binary_exact", "xml_allowed_rewrite",
        "replacement", "derived", "excluded"
    ]
    allowed_rewrite_paths: tuple[str, ...]
    reason: str
    evidence_refs: tuple[str, ...]
```

Схемы выше — контракты, не требование заменить текущий Pydantic на dataclass.

`TransplantContext`:
`source_graphs,target_package,part_map,relationship_map,slide_map,shape_map,protected_target_parts,edge_policy,pending_edges,part_contracts,allocated_names,active_stack,issues`.

Ключ part_map квалифицирован source package hash и part name. `ppt/charts/chart1.xml` из разных inputs — разные parts.

Для копий одного mutable chart с разным оформлением добавить variant key или copy-on-write. Immutable workbook/media допускают сохранение source sharing при одинаковых dependencies. Не смешивать distinct source objects только по одинаковым bytes.

`RelationshipKey`:
`source_package_hash,owner_part,rid`.

`RelationshipMapping`:
`source_key,target_owner,target_rid,target_part_or_uri,target_mode,relationship_type,handling,evidence`.

Hyperlink occurrence identity дополнительно содержит XML path/event click или hover; две occurrences одного rId не склеиваются.

`NativeDeckContext`:
`presentation,active_template_profile,rules_snapshot_id,source_to_output_slide_map,output_map,asset_store,transplant_context,pending_connectors,pending_hyperlinks,emission_records,part_contracts,issues`.

`OutputBinding`:
`source_ref,target_address,target_subaddress,origin,transform,verification_state`.

Target subaddress должен различать paragraph/run/field/table cell/plot/series/point. Не связывать два одинаковых текста по first matching string.

### 2.2. Общие инварианты

1. SourceDeckIR/SourceLedger immutable. Reimport кандидата пишет только `candidate_after_*`.
2. Emitter берёт payload из source по ID; не из пересказа LLM.
3. Role/list/font/color properties из того же ontology snapshot, который проверяет validator.
4. Source shape IDs и output shape IDs могут отличаться. Сохранность определяется фактической map и значениями.
5. Corporate template master/layout/theme не заменяются исходным некомпоративным мастером.
6. Internal relationships owner-scoped; external URIs сохраняются без fetch.
7. Source chart/workbook data неизменны для style-only restyle.
8. Native chart/table/text/connector не подменяются картинкой.
9. Unsupported объект не исчезает: verified opaque carry, explicit needs_review или отказ.
10. Output bindings формируются после реального создания shapes/parts, не по предполагаемым IDs.
11. Source count/order/hidden flags сохраняются при preserve_structure.
12. Fail не публикует requested output и не меняет source/corpora.
13. Ни один skill/reference loader не использует примеры за пределами пользовательского corpus или разрешённой производной lineage; нарушения дают отказ.

## 3. Preflight export и capabilities

### `preflight_native_export(source, ledger, resolved_slides, rules, template, options) -> ExportPreflightReport`

Входы: полный immutable source, ledger, принятые ResolvedSlides, loaded ontology, verified active template, typed options.

Переменные: `source_index,required_atoms,planned_refs,kind_matrix,closures,unresolved,asset_issues,contracts`.

1. Проверить hashes source, rules snapshot, template, plan schema. Stale plan не применять к новому corpus.
2. Expand content groups до required atoms; проверить multiplicity/coverage, source slide ownership и structure policy.
3. Проверить геометрию/role assignments на поддержанных scopes. Measurement отсутствует — unknown, а не pass.
4. Для каждого объекта определить emission strategy:
   `native_rebuild/native_transplant/opaque_transplant/unsupported`.
5. Вычислить dependency closure и edge handling до записи output.
6. Отдельно определить `data_preservation_support,style_normalization_support,native_structure_support,render_support`.
7. Unknown required relationship/data dependency — issue; не оставлять на «авось serializer сохранит».
8. Missing fonts не останавливают explicit diagnostic build; production policy остаётся blocked.
9. Подготовить PartPreservationContracts: source content closure, template replacements, approved exclusions, opaque obligations.
10. Вернуть report с обязательными issues/capabilities; не пустой success.

`ExportOptions`:
`workflow,preserve_structure,diagnostic,allow_opaque_carry,allow_split,allow_cross_slide_move,overwrite,output_policy`.

Тестовый диагностический exporter может выдать candidate с needs_assets/needs_review. Однако known data loss/dangling links/invalid package не получают successful structural-export status.

## 4. Новый output deck из активного шаблона

### `create_output_deck(template, profile, slide_plan) -> NativeDeckContext`

Signature основной спецификации сохранить; dependencies rules/store/options передавать через согласованный context/factory, а не global singleton.

Переменные: `active_template,template_hash,prs,layout_map,slide_slots,slide_mapping,protected_inventory`.

1. Открыть проверенный active template, сверить точные canvas EMU с loaded rules.
2. Использовать verified layout identities из TemplateProfile, не произвольный `slide_layouts[0]`.
3. Не переносить 99 демонстрационных слайдов из библиотеки как содержимое output. Если derived template ещё содержит примеры, создать чистую run/cache копию с сохранением masters/layouts/protected assets и явным derivation manifest.
4. Template sample slides удалять только из производной template copy; это не разрешение удалять source content.
5. Если active template требует нормализации exact font-face/bold/native lists, использовать только уже доказанные правила и diff. Geometry/logos/branding не угадывать.
6. Снять protected object fingerprints по активному template. Не сравнивать output с fingerprint другой, уже заменённой версии.
7. Заранее создать все output slides в сохранённом порядке. Сформировать source slide ID → actual output slide part/presentation ID map.
8. Preserve hidden flags. Для document workflow slides происходят из approved materialized plan, не из числа страниц PDF.
9. Зарегистрировать template part closure и защищённые target paths.
10. Вернуть context; пока не записывать content shapes.

Шаблон имеет собственные layout placeholders. Заполнять matched placeholder или создавать native content shape в разрешённой области. Не оставлять дубли заголовка и filler placeholders. Не удалять обязательный footer/master object как «мешающий».

## 5. OPC adapter и relationship-aware transplant

### 5.1. `plan_part_closure(root, context, policy) -> PartClosurePlan`

Переменные: `queue,visited,edges,part_keys,back_refs,unknown_edges,contracts`.

1. Обход source_graph от нужного root; visited по SourcePartKey.
2. Для каждой relationship применить typed edge policy:
   - `clone_dependency` — chart/workbook/image/style/color и другие поддержанные payload dependencies.
   - `map_slide_reference` — ссылка на уже созданный output slide.
   - `bind_target_template` — разрешённая layout/master/notes-master связь.
   - `preserve_external` — URI без запроса к сети.
   - `derive_or_normalize` — известное theme/style правило с provenance.
   - `unsupported` — blocking issue для значимых dependencies.
3. Source presentation/master/layout graph не клонировать целиком через back-reference.
4. Notes обратную связь на slide разрешать через slide map.
5. Unknown relationship type не считать image/chart dependency по похожему filename.
6. Циклы фиксировать как графовую структуру; не рекурсивно копировать бесконечно.
7. Описать source parts, которые сознательно заменяются template parts, и required parts, которые должны сохраниться.

### 5.2. `allocate_part_name(source_key, content_type, context, variant=None) -> str`

Входы: квалифицированный source key, content type, target package/name inventory, optional variant.

1. Если mapping уже существует в нужном variant, использовать его.
2. Проверить target name inventory и content types.
3. Выделить уникальное canonical OPC part name с подходящим extension.
4. Зарегистрировать mapping до обхода edges.
5. Не overwrite template/media/chart по совпадению имени.
6. Case-sensitive paths и URI normalization обрабатывать по package policy; конфликт нельзя скрывать lower-case.

### 5.3. `clone_part_graph(root_part, context) -> TransplantResult`

Переменные: `closure,reserved_map,part_objects,owner_rid_maps,pending_edges,rewrites,issues`.

Два прохода:

**A. Parts.**
1. Получить closure plan.
2. Для всех parts заранее выделить target names и зарегистрировать mapping.
3. Binary payloads копировать bytes exact; hash контракт.
4. XML копировать с сохранением namespaces/unknown content. Не собирать только известные children и не чистить extLst «ради совместимости».
5. Создавать target Parts через изолированный adapter, согласованный с закреплённой python-pptx version/content type factory.

**B. Relationships.**
6. Для каждого source owner создать target owner-local relationships.
7. Internal target разрешить через part/slide/template mapping; relative Target вычислять относительно target owner.
8. External Target/TargetMode/type сохранить; URI не скачивать.
9. Сохранить source owner+rId → target owner+rId map. Shared target допустим, duplicate occurrence остаётся.
10. Переписать известные relationship-valued attributes в XML owner: r:id/r:embed/r:link и поддержанные extension/VML attrs через handlers.
11. Не делать строковый replace `rId1` по всему XML: он может менять текст, другой owner или `rId10`.
12. Не выдумывать remap неизвестного attribute только потому, что значение похоже на rId. Неподдержанный extension dependency → issue.
13. Проверить сохранность namespace bindings, включая prefixes в QName-valued attrs/markup compatibility. Префикс, употреблённый в mc:Ignorable, нельзя потерять как «неиспользуемый».
14. Разрешить deferred links/backrefs после создания всех target nodes.
15. Возвратить actual root part, part/rel maps, contracts и issues.

`deepcopy(shape._element)` может быть частью работы с subtree, но не является полным переносом между пакетами.

### 5.4. Shared и copy-on-write

- Один source image, используемый несколько раз, может оставаться одним target media part при нескольких picture shapes.
- Один source workbook, разделяемый chart parts, можно сохранить shared при отсутствии data edits.
- Два разных source workbooks одинакового имени не объединять.
- Два references одного chart с одинаковой normalization могут использовать один cloned chart.
- Если их normalizations различаются, создать chart variants, не изменить общий chart «последним emitter».
- Distinct graph identity/multiplicity не выводить из hash равных bytes. Dedup допустим только с соответствующим contract.

### 5.5. Opaque и orphan parts

Значимые unknown objects сохранять только при проверенном opaque transplant. `opaque preserved` не означает `style compliant` или `fully editable by UI verified`.

Неизвестную значимость считать required до доказанного exclusion. Sidecar archive полезен для диагностики, но не доказывает присутствие editable content в итоговом PPTX.

Unreferenced ZIP members классифицировать отдельно. python-pptx serialization нельзя использовать как доказательство сохранности всех orphan/opaque members: проверять final bytes. Если approved opaque members можно сохранить через final package assembler без коллизий и нарушения package structure, сохранить byte-exact и content-type contract. Не создавать фиктивные semantic relationships, чтобы удержать orphan part.

Если корректная carry невозможна, вернуть needs_review/unsupported; не скрывать удаление через сравнение только reachable parts.

### 5.6. `validate_package_graph(bytes_or_path, expectations) -> PackageGraphReport`

Проверить:
- ZIP CRC/readability, отсутствие duplicate member names.
- Content type каждой нужной part и корректность declarations.
- Уникальность owner relationship IDs.
- Все internal targets существуют после URI resolution.
- XML relationship-valued refs разрешаются в соответствующем owner.
- Presentation slide list указывает на actual slides, layouts/masters существуют.
- Notes backrefs, internal links, chart/workbook/style/color targets корректны.
- Part name collisions отсутствуют; protected template parts не подменены.
- Required opaque contracts выполняются.

Не объявлять Office-repair-free только потому, что zipfile открыл архив. Если XSD validator уже установлен, использовать его по existing policy; отсутствие проверки честно указать.

## 6. Text, fields, списки и display transforms

### `emit_text(payload, resolved, slide, context) -> OutputShapeAddress`

Переменные: `shape,text_frame,paragraphs,runs,fields,role_style,list_profile,bindings`.

1. Создать actual native textbox или matched placeholder; точные resolved EMU, margins, wrap/anchor, noAutofit.
2. Для каждого исходного paragraph создать отдельный paragraph. Empty paragraphs сохранять, если это source content; не добавлять новые для вертикальных отступов.
3. Воссоздать ordered tokens: run, a:fld, soft/hard break, footnote marker. Не заменять поле его cached plain text.
4. Preserved field metadata: id/type/cached; date field не обновлять автоматически на сегодняшний день.
5. Run-level emphasis, superscript/subscript, hyperlinks и значимые boundary сохранить. Прямые font values заменить по loaded role/font policy, не теряя semantic marks.
6. Не использовать `shape.text=...` или `paragraph.text=...` для rich source: это уничтожает mixed runs/fields/links.
7. Resolve effective role font/size/color из текущего snapshot; b=false/true/inherit по exact policy, не по названию Bold.
8. Сохранить permitted inherited formulas, где они подтверждены. Не оставлять +mn-lt/+mj-lt ссылку на случайный новый template font.
9. Применить native list/numbering по semantic list status и loaded profile. Весь body не превращать в bullets автоматически.
10. Сформировать per paragraph/run/field output bindings и tokens order.
11. Reimport saved XML должен подтвердить типы/значения/порядок, а не только joined text.

### `apply_list_profile(paragraph, level, style, context) -> ListApplicationReport`

Переиспользовать Stage 2–3.5 build/resolve list profile.

1. Remove только competing bullet properties в target pPr.
2. Записать loaded buFont/buChar/buSzPct/native color rule в schema order.
3. Для current ontology значения Wingdings/§/80000 должны происходить из JSON/evidence, а не literals emitter.
4. Priority/conditions цвета исполняются по loaded list rules; nested follow text ≠ fixed primary blue.
5. Indent, hanging indent и spacing из verified/derived profile с provenance.
6. Effective nested size из loaded policy; infeasible не clamp до ложного pass.
7. Numbered semantic sequence сохранять native numbering. Chart labels/data rows/KPI/prose не считать перечислением по визуальному сходству.
8. Idempotency: повторное применение не создаёт competing bullet nodes.

### `apply_display_transform(payload, role, rules, context) -> DisplayTransformResult`

Если loaded applicable role требует uppercase:
1. Source payload не менять.
2. Преобразование разрешено только для подтверждённой роли и явно recorded transform.
3. Сохранить source refs/raw text, displayed text, transform kind, norm source ref и transform version.
4. Не менять числовые/единичные/case-sensitive atoms, где преобразование может менять смысл; конфликт решать как needs_review, не игнорировать.
5. Source/output comparison применяет transform только к конкретным binding, а не global lowercase/whitespace normalization.

Fixture: русский title с обычным текстом может upper-case; body и обозначения единиц остаются без такого преобразования. При отсутствии safe transform support отказать конкретному scope, не имитировать corporate pass.

## 7. Native tables и физические merged cells

### `emit_table(payload, layout, slide, context) -> OutputShapeAddress`

Переменные: `grid,rows,columns,merge_ranges,origin_cells,hidden_cells,table,raw_tc,cell_bindings`.

1. Проверить rectangular physical grid и координаты; каждый source cell имеет одну identity. Stage 3 duplicate-cell fix сохраняется.
2. Создать native graphicFrame/table. Не строить таблицу из rectangles/textboxes.
3. Col widths и row heights из ResolvedTable. Не «делить поровну», если resolved/source contract другой.
4. Применить merge ranges на пустой сетке; затем populate. Public merge над уже заполненными cells может переносить содержание к origin — это недопустимо без контроля.
5. Populate visible ordinary/merge-origin cells по source token structure.
6. Physical spanned cells и их hidden txBody сохранять через checked OOXML adapter, если source содержит значимое hidden content. Не append hidden strings к visible merged cell.
7. Если физическое hidden content не удалось сохранить внутри PPTX, status native_partial/needs_review; sidecar не full preservation.
8. Apply exact role/list/noAutofit/margins/fills/borders из loaded table profile.
9. Preserve true empty string, zero, NA и значимые number display formats.
10. Source tableStyleId/цветовые наследования либо корректно bind к target table style, либо materialize поддержанные свойства из approved profile. Не оставлять source style ID с отсутствующим definition.
11. Compare saved raw table grid/merges/cell payloads и effective native type.

Допустимо переносить source native table subtree с normalization, если это обеспечивает физическую сохранность и новые bindings. Это не позволяет переносить неподтверждённый source style.

## 8. Native charts: исходные и новые

### 8.1. `snapshot_chart_semantics(chart_part, graph) -> ChartSemanticSnapshot`

Snapshot включает:
`plot_order,plot_types,plot_scoped_series,series_order,point_indices,categories,value_lexemes,blank_states,formulas,external_data_binding,axis_ids,cross_axis_ids,axis_type,scale,reverse,log_base,number_formats,disp_blanks_as,error_bars,trendlines,custom_labels,semantic_color_mapping,workbook_hashes,uncertainties`.

Сохранять исходные lexical/display properties, не только floats. Plot-scoped series/point IDs предотвращают повтор Stage 3 cross-plot duplicates.

### 8.2. `emit_existing_chart(payload, resolved, slide, context) -> OutputShapeAddress`

Переменные: `before,cloned_graph,frame,style_scope,after,workbook_contracts,bindings`.

1. Chart cache/workbook blocking conflict из importer не игнорировать. Без разрешённого source truth нельзя подтвердить verified data export.
2. Снять semantic snapshot исходного graph.
3. Clone chart closure вместе с embedded workbook, chart style/color parts и поддержанными dependencies.
4. Создать actual native graphicFrame с chart relationship и resolved geometry.
5. Нормализовать только поддержанные visible style nodes: fonts/colors/lines/fills/text props. Source chart data не пересобирать из text summary.
6. Для style-only restyle **не вызывать replace_data**: он меняет chart data XML и workbook.
7. Series order, point idx, formulas, caches, axis semantics, gaps/zero, categories, custom labels и number formats не изменять.
8. Color encoding требует semantic_color_key mapping. Source red «риск» нельзя автоматически заменить первым синим, сохранив лишь число.
9. Chart theme-dependent properties нормализовать по реальному source context и target role policy. Не перенести source theme как новый общий corporate master.
10. Запрещённый 3D encoding не превращать в 2D автоматически без проверенного semantic transform. Unsupported normalization → native preserved + needs_review, не style pass.
11. Снять after snapshot; сравнить before/after на in-memory стадии и после saved reimport.
12. Workbook binary bytes/hash должны совпасть при style-only export.
13. Создать series/point/category/label bindings, preserving plot scope.

Не считать два overlay chart shapes настоящим единым combo. Исходный multi-plot chart переносить как один native chart с исходными primary/secondary axes.

### 8.3. `emit_new_chart(data, resolved, slide, context) -> OutputShapeAddress`

Вход `VerifiedChartData`:
`chart_type,series,categories_or_xy_points,axes,unit_refs,source_evidence_refs,verification_status`.

1. Требовать verified data; числа от layout LLM не принимать. Концептуальный текст сам по себе не разрешает выдуманные численные данные. Иллюстративный dataset требует отдельного approved типа, явной разрешающей policy и маркировки; этот дополнительный path не обязателен для Stage 4.
2. Применять native data builders по типу: CategoryChartData / XyChartData / BubbleChartData и supported add_chart.
3. Базовые families: column/bar, line, pie/doughnut, area, scatter/bubble. Поддержку оценивать по конкретным тестам, не только enum.
4. Non-shared X across series не превращать в common category list.
5. Blank≠zero; repeated categories сохранять с identity/order. Для backend limitation вернуть unsupported, не fill zero.
6. Preserve units, source display precision и meaningful numeric values. Если serializer теряет необходимую точность, зафиксировать failure; не broad epsilon.
7. Percentage chart требует понятного denominator/coverage.
8. Loaded native style применить отдельно от data generation.
9. Reimport chart cache и workbook; compare с approved data ledger.
10. Новый combo разрешён только при отдельной настоящей OOXML implementation/tests; upstream overlay helper не использовать как подмену.

Создание verified data fixtures не является document authoring. Stage 8 позже подаст approved data в этот emitter.

## 9. Shapes, groups, images и connectors

### 9.1. `emit_shape(payload, resolved, slide, context) -> OutputShapeAddress`

1. Native preset/freeform geometry согласно typed payload и capabilities.
2. Geometry и style — из resolved plan/loaded profile.
3. Semantic text frame — через emit_text tokens, не copy joined string.
4. Preserve rotation/flips/semantic outline. Удалять только доказанный old decoration/effect.
5. Unknown freeform/path features — explicit unsupported/opaque scope, не заменить на прямоугольник.
6. Actual shape ID получить после creation и зарегистрировать map.

### 9.2. `emit_group(payload, resolved, slide, context) -> OutputShapeAddress`

Переменные: `group,children,source_transform,target_transform,child_id_map`.

1. Preserve native p:grpSp для поддержанного group; не flatten children по приблизительным absolute rectangles.
2. Сохранять off/ext/chOff/chExt, rotation/flips и вложенный coordinate space.
3. Если group box меняется, выбрать явный метод transform composition и сравнить corners независимым вычислением.
4. Shape IDs unique в output slide, включая nested descendants; group path входит в map.
5. Nonuniform scale/rotation/flips не применять дважды.
6. Child text styles нормализовать, content/structure не удалять.
7. Unsupported group semantic transforms — refuse/needs_review, не silent flatten.

### 9.3. `emit_image(payload, resolved, slide, context) -> OutputShapeAddress`

1. Сохранить исходные media bytes для поддержанного формата. JPEG/PNG/поддержанный native SVG/EMF не recode автоматически.
2. Native picture, crop/aspect/rotation/flips из typed source+resolved.
3. Media graph/importer должен видеть фактический embedded target.
4. Linked media URI не скачать и не превращать в недостоверный local asset.
5. Photo natural colors не проверять попиксельно как palette violations.
6. Whole-slide/whole-chart rasterization запрещена как substitute native editability.

### 9.4. `emit_connectors(graph, slide, context) -> list[OutputShapeAddress]`

1. Создать все native node shapes сначала.
2. Для каждого source edge получить реальные source→target node shape IDs.
3. Записать native connector p:cxnSp и stCxn/endCxn IDs/connection sites.
4. Arrowheads/conditions/labels сохраняют source semantics, не рисуют неподтверждённую causality.
5. Order по z-order contract; creation order не должен случайно перекрыть nodes.
6. Горизонтальный/вертикальный connector с нулевым одним extent допустим; content box требует две положительные стороны.
7. Relink при nested group IDs — через actual shape map.
8. Нет разрешённого endpoint/site → explicit issue, не dangling ID.

## 10. Notes, links, flags и emission order

### `emit_slide(resolved, source, context) -> list[OutputBinding]`

1. Проверить supported objects/z-order и source ownership.
2. Emit native objects в resolved/source z-order с фактическими bindings.
3. Deferred connectors после создания endpoints.
4. Deferred hyperlinks после создания всех output slides/objects.
5. Copy notes и hidden flags.
6. Нормализовать только author style scope; protected template untouched.
7. Assert required atoms coverage; pending edges не оставить после finalize.

### `copy_notes(source_slide, output_slide, context) -> NotesEmissionReport`

1. Сохранить содержательные notes paragraphs/runs/fields/links и значимые media.
2. Notes slide link back to actual output slide, notes master — корректный active target notes master.
3. Не втаскивать source presentation graph через reverse relationship.
4. Corporate notes template structure и source content различать; не считать notes placeholder text source facts.
5. Source notes layout features, не поддержанные normalization, показывать отдельным scope.
6. Reimport notes сравнить с ledger, включая порядок и occurrences ссылок.

### `remap_hyperlinks(context) -> LinkResolutionReport`

1. Обойти run/field/shape/group-child click и hover occurrences.
2. Internal link target source slide ID → actual target slide part через complete map.
3. Shared rId occurrences сохранить; mapping relationships owner-scoped.
4. External hyperlink URI/type/action сохранить без fetch.
5. Navigation actions без explicit slide part различать с target-slide links; не переписывать next/previous как статический URI.
6. Unresolved source target remains explicit. Нельзя выбрать первый output slide.
7. Target omitted by selection/recompose — needs_review/refuse по policy, не silent rel removal.

Preserve_structure сохраняет count, порядок и hidden status. Любое split/cross-slide move требует уже согласованной policy и нового explicit mapping; не включать автоматически в Stage 4.

## 11. Сохранённый output: независимые проверки

### 11.1. `finalize_and_save_candidate(context, path, expectations) -> CandidateArtifact`

Переменные: `serialized,assembled,temp_path,package_report,reimported,mapping,content_diff,part_diff`.

1. Разрешить все pending edges, сверить contracts и in-memory coverage.
2. Serialize через agent-slides adapter и documented final assembly. Если python-pptx save переписал/выбросил unsupported parts, это должно выявиться и быть исправлено в adapter либо привести к refusal.
3. Final opaque assembly, если нужен, выполнять до конечных validators. После validators никакой ZIP patching.
4. Temporary candidate — только в run, same filesystem для последующей atomic replacement.
5. Validate final ZIP/package graph/content types.
6. Reopen через python-pptx и независимо importer на final bytes; не source_ir overwrite.
7. Сверить actual addresses в OutputObjectMap.
8. Выполнить compare preservation и mapped part contracts.
9. Native object/data checks на raw saved package, а не только API .has_chart.
10. Сохранить candidate и reports с actual artifact hash.
11. При data/package failure candidate может остаться debug artifact, но success=false и requested final output untouched.
12. Requested production publication не выполнять при strict readiness=false. Служебный `build` производит candidate, не готовый product output.

### 11.2. `compare_export_preservation(source_ledger, actual_ir, bindings, policy) -> ContentDiff`

1. Validate каждую target shape/subaddress реально существует.
2. Сравнить required atom multiset по source-qualified IDs/map.
3. Text/tokens/fields/footnotes/units/caveats: exact или explicit per-binding display transform.
4. Numeric state: Decimal comparison плюс сохранение meaningful display formatting; empty/NA/zero различаются.
5. Tables: physical grid/merges/visible и hidden payload, cell order/multiplicity.
6. Charts: plot-scoped series/categories/points, formulas/scales/caches и workbook contracts.
7. Nodes/edges/arrow conditions, image bytes/crop, notes, hyperlinks, hidden/count/order.
8. Corporate template fields/new branding имеют отдельную template provenance; не «unexpected facts».
9. Missing/duplicated/changed required atom — failure.
10. Unknown значимость не исключать из ledger, чтобы получить 0 missing.

### 11.3. `compare_export_parts(source_graphs, target_graph, contracts, mappings) -> PartDiff`

Новый corporate deck закономерно отличается от source по part names/counts. Не сравнивать literal `40/40` как критерий.

Для каждого contract:
- `binary_exact` — target bytes/hash совпадают.
- `xml_allowed_rewrite` — различия только в объявленных paths/relationship attrs/style scopes; XML content/semantics вне footprint сохраняются.
- `replacement` — например source master заменён active corporate master с provenance.
- `derived` — new slide/template field/native content имеет input refs и reproducible origin.
- `excluded` — доказанный noncontent decoration/unused template sample; причина/evidence обязательны.

Canonical XML hash не заменяет проверку allowed mutation paths. Stage 3 `c14n-exclusive-v1` для stale-shape guard сохранить; package equivalence может иметь другой versioned algorithm. Не объявлять namespace/QName-valued attributes безопасными только по C14N.

Check:
`unmapped_required_parts,missing_binary_payloads,unexpected_xml_mutations,changed_relationship_semantics,template_substitution_violations,dangling_targets,unverified_exclusions`.

Отчёт должен ловить случай, когда число частей совпало, а workbook/target изменён.

### 11.4. `verify_native_objects(actual_package, actual_ir, bindings) -> NativeEditabilityReport`

Для объектов проверить actual types:
- Text: native shapes/text frames и tokens.
- Table: native a:tbl/grid/merges/cells.
- Chart: native chart graphicFrame, chart part, workbook/approved data source.
- Diagram: native shapes/connectors/group IDs.
- Image: native picture/media.
- Unknown: opaque/native_partial с конкретным ограничением.

Fields:
`native_structure_verified,data_preservation_verified,graph_integrity_verified,style_checked_scopes,render_verified,powerpoint_ui_edit_verified`.

Native structure proof не равен выполненному тесту редактирования в UI PowerPoint. UI флаг false/unknown до actual check.

## 12. Pipeline и CLI

### `run_native_build(run_dir, plans_dir, config, options) -> NativeBuildResult`

1. Прочитать immutable run inputs и принятые планы с сохранённым generation origin; сейчас по умолчанию это harness plans.
2. Validate snapshot/source/template/schema hashes; stale cache/plan отказ.
3. Получить ResolvedSlides через существующий resolver. Не заменять plan статическим layout.
4. Если точный measurement ещё отсутствует, использовать declared diagnostic resolved geometry и отметить limitation. No fabricated overflow pass.
5. Preflight export.
6. Create output deck, все slide maps заранее.
7. Emit content, chart/part graph transplant, deferred connectors/notes/links.
8. Save final candidate, independent checks.
9. Записать result, выбранный LLM provider, происхождение планов и readiness. Native exporter сам не генерирует планы; в текущем режиме 2 нет model HTTP generation в приложении.

Служебный `build --run-dir PATH --plans-dir PATH` уже предусмотрен основной спецификацией. Переиспользовать и включить в help/discovery.

Если accepted artifact содержит LayoutIntent, а не ResolvedSlide, resolver должен явно создать resolved artifact; native emitter не интерпретирует свободный model JSON.

При необходимости semantic clarification в режиме 2 приложение создаёт harness request и возвращает typed `awaiting_harness`. Текущий OpenCode читает packet/schema, выдаёт JSON своей текущей моделью, затем продолжает команду. Будущий режим 1 подаст ответ настроенного локального сервера в те же validators и resolver; реализацию HTTP/server/SDK layer сейчас отложить. Cache планирования учитывает provider и доступную model identity; явно переданные сохранённые планы сохраняют исходный origin независимо от текущей настройки.

Не добавить третий product workflow. Будущие `from-ppt` и `from-document` используют этот native engine. На этом этапе `build` — service operation.

NativeBuildResult:
`operation_status,export_scope,llm_provider,plan_generation_origin,candidate_path,candidate_sha256,source_preservation_verified,package_graph_verified,native_structure_verified,style_coverage,unresolved_scopes,production_assets_ready,render_verified,powerpoint_ui_edit_verified,strict_output_ready,issues,artifacts`.

Exit codes сохранить по existing contract. Missing production assets и failure preservation — разные issue classes. Diagnostic operation может быть completed при needs_assets, если его structural/data contract прошёл; весь product status не COMPLETED.

## 13. Тесты и пилоты

Тесты добавлять на реальные ошибки/инварианты, не зеркалить implementation. Проверять actual serialized bytes независимо.

### 13.1. Обязательные positive/negative tests

| Тест | Проверяемое свойство |
|---|---|
| `test_new_output_deck_from_template` | Новый deck, loaded canvas, verified layouts/protected objects, без 99 demo slides |
| `test_text_tokens_fields_roundtrip` | Кириллица/₽/%/минус, mixed runs, a:fld, footnote, soft break, links |
| `test_equal_text_distinct_bindings` | Identical strings не теряют отдельные occurrences |
| `test_case_transform_scoped` | Title-only explicit transform; body/units неизменны либо safe refusal |
| `test_list_profile_saved_xml` | Native loaded buFont/buChar/buSzPct/priority/indents/noAutofit |
| `test_inconsistent_list_norm_blocks` | OOXML percent/declared percent/MD mismatch выявлен |
| `test_table_merge_visible_hidden_payload` | Physical grid, merges и hidden strings не migrate в origin |
| `test_table_empty_zero_na` | Различные states/display formats сохранены |
| `test_chart_style_only_workbook_exact` | Normalized style, workbook bytes equal, no replace_data |
| `test_combo_axes_and_plots_preserved` | Один multi-plot chart, plot identities, secondary axes |
| `test_scatter_bubble_nonshared_x` | X/Y/size и разный X grid across series |
| `test_chart_conflict_blocks_verified_build` | Cache/book source conflict не игнорируется |
| `test_new_verified_native_charts` | Approved data создаёт native basic supported charts, reimport matches |
| `test_chart_semantic_color_unknown` | Unsupported color meaning не становится pass |
| `test_shared_image_workbook_relations` | Sharing корректно, occurrence counts сохранены |
| `test_same_part_names_different_sources` | chart1/workbook names не коллидируют между source packages |
| `test_copy_on_write_chart_styles` | Different style contexts не overwrite shared mutable chart |
| `test_cycle_backrefs_no_deck_import` | Closure finite; notes/slide backrefs через map |
| `test_owner_scoped_rid_remap` | rId1 разных owners не склеены; rId10/text untouched |
| `test_unknown_namespace_preserved` | extLst и mc prefix bindings не исчезли |
| `test_external_links_no_fetch` | URI/click/hover сохранены без app network |
| `test_internal_notes_hidden_order` | Links/notes/backrefs, count/order/hidden flags |
| `test_group_transform_corners` | Независимый corner calculation при rotation/nonuniform scale/flips |
| `test_connector_output_ids` | stCxn/endCxn actual IDs/sites, zero extent допустим |
| `test_unknown_opaque_contract` | Byte preservation при declared support либо explicit refusal |
| `test_same_part_count_changed_workbook` | Equal counts не маскируют changed binary |
| `test_same_part_count_wrong_target` | Equal counts не маскируют changed relationship |
| `test_serializer_drops_opaque_member` | Final validator обнаруживает drop, success=false |
| `test_atomic_failure_preserves_inputs` | Source/corpora/requested output hashes unchanged |
| `test_source_ir_not_overwritten` | Candidate_after artifacts отдельные |
| `test_stale_snapshot_plan_rejected` | New corpus/schema/source hash не использует прежний plan |
| `test_harness_provider_no_local_endpoint_gate` | Build/diagnostic работают без model server/client |
| `test_default_llm_provider_harness` | Provider не задан → режим 2; локальный endpoint не проверяется |
| `test_explicit_local_provider_deferred` | Режим 1 распознаётся; генерация до реализации adapter честно deferred, без скрытого harness fallback |
| `test_reference_source_whitelist` | Upstream/alternate/web/symlink references отклонены; разрешены только user corpus/проверенные derivatives |
| `test_skill_legacy_reference_denied` | Project skill не обходит guard и не восстанавливает удалённые samples |
| `test_foreign_reference_cache_invalidated` | Старый индекс/packet/plan с запрещённой provenance не используется |
| `test_upstream_examples_untracked` | Выявленные legacy sample paths отсутствуют в current tracked tree; пользовательский corpus не удалён |

Не обещать support всем chart names по одному bar chart. Unsupported samples должны быть в matrix и negative tests.

### 13.2. Synthetic compound deck

Собрать isolated native fixture из 5–8 slides, достаточный для:
- text/fields/native list;
- merged table с hidden physical payload;
- category chart и embedded workbook;
- настоящий исходный multi-plot chart с axes;
- nested group/native connectors;
- internal/external click/hover links, notes и hidden slide;
- opaque dependency с defined preservation/refusal policy.

Fixture input и output разные packages; collision с существующими target media/chart names создать намеренно. Простое сохранение source .pptx в другой filename не считается exporter pilot.

Интеграционный positive sample должен реально использовать active target template; styles из loaded ontology. Без font binaries проверяются native/XML/data scopes, не внешний вид.

### 13.3. Реальный source pilot

1. Выбрать реально доступный input или подходящие slides из source library. Перечислить source path/hash/IDs/kinds в pilot manifest.
2. Library sample — технический pilot, не доказательство улучшения плохой презентации.
3. Partial selection не должна потерять internal target или notes. Проверить closure selection; unresolved target → diagnostic failure/needs_review.
4. Выполнить полный export выбранного supported source в новый template deck, не Stage 3 patch.
5. Reopen/import; сравнить ledger/chart data/workbook/notes/links/parts contracts.
6. Source sample import_complete не означает все его objects export_supported. Полный 99-slide preflight можно использовать для honest coverage; не обходить unsupported посредством удаления.
7. Сохранить output и difference reports.

Для технической выборки сохранить `pilot_selection.json` и `ledger_scope_manifest.json`: original deck hash, selected slide/object IDs, dependency closure, excluded-from-pilot IDs и основание. Полный original IR/ledger не изменять. Scoped comparison сверяет selected required atoms и связанные данные, а результат помечается `verification_scope=pilot_selection`. Он не доказывает экспорт всего исходного deck и не разрешает продукту `from-ppt` молча отбрасывать невыбранные слайды.

### 13.4. Связь с Stage 3.5 model intent

Использовать один реально принятый harness candidate из предыдущего run или новый harness JSON по тому же contract. При старом candidate убедиться в snapshot/source compatibility.

Trace:
`harness response → accepted LayoutIntent → ResolvedSlide → new deck emitters → actual saved output bindings/properties`.

Сохранить не менее одного meaningful примера grouping/reading-order geometry, где native export следует выбранному intent. Не требовать новый endpoint или generation counter для проверки emitter.

Model name/usage остаются reported/declared, если harness не предоставляет backend evidence. Не записывать application_API_calls=5 на основании пяти external response files.

## 14. Команды, артефакты и приёмка

### 14.1. Проверки

Формы команд; placeholders заменить фактическими paths/flags:

```bash
uv run ruff check .
uv run pytest
uv run slides gpn --project-root /Users/wysockii/Documents/gpn_slides --config CONFIG_PATH doctor
uv run slides gpn --project-root /Users/wysockii/Documents/gpn_slides --config CONFIG_PATH build --run-dir RUN_PATH --plans-dir PLANS_PATH
```

Если discovery/CLI syntax отличается, согласовать с existing API и документировать реально выполненный вызов. Не запускать команды с несуществующими placeholder paths.

Не `doctor --check-model` как gate Stage 4. Font readiness проверить отдельно и сохранить missing faces.

### 14.2. Артефакты

В `runs/stage4/<run_id>`:
- `audit_before.json`, `audit_after.json`, `upstream_provenance.json`.
- `forbidden_reference_cleanup.json`: Git/skills/pipeline/cache cleanup и фактические deny proofs.
- `input_manifest.json`, `ontology_manifest.json`, `active_template_manifest.json`.
- `source_ir.json`, `source_ledger.json`, `export_preflight.json`.
- `resolved_slides/`, `accepted_plan_manifest.json`, `intent_export_trace.json`.
- `emission_report.json`, `part_closure.json`, `part_map.json`, `relationship_map.json`.
- `part_preservation_contracts.json`, `output_object_map.json`.
- `candidate.pptx`, `candidate_after_ir.json`, `candidate_after_ledger.json`, `candidate_after_support.json`, `candidate_after_links.json`.
- `package_graph_report.json`, `part_preservation_diff.json`, `content_diff.json`, `chart_semantic_diff.json`, `workbook_hashes.json`.
- `native_editability_report.json`, `template_integrity_report.json`, `style_rule_coverage.json`.
- `pilot_manifest.json`, `commands_and_results.json`, `stage4_readiness.json`.

Не пустые success artifacts. Все reports относятся к SHA-256 конкретного final candidate. Debug candidates отделены от успешно checked candidate.

### 14.3. Done

Stage 4 implementation done на declared support scope только когда:
1. Происхождение agent-slides подтверждено; working tree preserved.
2. Создаётся новый deck на active corporate template, а не только изменяется исходник.
3. Text/fields/lists/tables/основные charts/groups/connectors/images/notes/links имеют tested native path либо явно ограниченный support.
4. Mandatory core path действительно реализован; unsupported essential feature не скрыта под «done».
5. Transplant closure, collisions, owner-local rIds, cycles, source-qualified keys и content types проверены.
6. Source workbook bytes неизменны при style-only chart restyle; plot/series/axes/formulas сохранены.
7. Saved candidate reimported, data/part/relationship contracts прошли; protected template сохранён.
8. Model intent реально влияет на exporter output через принятую schema.
9. Full pytest/ruff проходят; предыдущие regression checks сохраняются.
10. Corpora/input hashes unchanged; final output policy соблюдена.
11. Support matrix, IMPLEMENTATION_STATE и README обновлены с actual commands/paths/capabilities.
12. `render_verified=false` и `powerpoint_ui_edit_verified=false` остаются, если эти проверки не выполнялись; `strict_output_ready=false`.
13. Upstream/чужие examples исключены из current Git tree и runtime; project skills/reference tools используют только пользовательский whitelist с проверенными отказами.

Не считать «открывается python-pptx» доказательством отсутствия всех Office repair issues или визуальной читаемости. Не требовать fonts как предлог прекратить разработку native exporter.

Если data/relationship preservation failure не решена или exporter содержит core stubs, stage status partial с конкретными gaps.

### 14.4. Итоговый отчёт OpenCode

Дать:
1. Files/functions изменены и фактические upstream provenance.
2. Support matrix по объектам: native/data/style/render/UI отдельно.
3. Один source→target transplant example с names/rId mappings и workbook hashes.
4. Native candidate path/hash, actual output count/order/hidden flags.
5. Preservation results: missing/duplicates/changes/dangling/unexpected rewrite, а не только part counts.
6. Model intent→new output example, выбранный режим LLM и actual plan provenance; текущий default — режим 2/harness, реализация режима 1 отложена.
7. Test/lint numbers, actual commands/exits, corpus hashes.
8. Missing assets/unknown rules/unsupported features; next Stage 5, не запускать его автоматически.
9. Что исключено из Git/pipeline/skills/cache, какие reference guards реально enforced, результаты negative access tests.

## 15. Источники и версии

Изучать source/doc по фактически закреплённой версии зависимостей в проекте. Не менять python-pptx без необходимости и regression proof.

- [agent-slides, выбранная база](https://github.com/mpuig/agent-slides/tree/820782a5fb8cb8f251d7c0750e4299c321cd068c): facade/api/model/serialization reuse.
- [python-pptx chart API](https://python-pptx.readthedocs.io/en/latest/api/chart.html): chart находится в graphicFrame; replace_data заменяет XML data и workbook, поэтому не подходит для style-only переноса.
- [python-pptx table API](https://python-pptx.readthedocs.io/en/latest/api/table.html): table/grid/merge API; проверить поведение merge в исходниках закреплённой версии.
- [Microsoft: структура PresentationML](https://learn.microsoft.com/en-us/office/open-xml/presentation/structure-of-a-presentationml-document): parts и допустимые связи slide/notes/master.

Private operations проверить по установленному source. В исследовательском окружении python-pptx 1.0.2: _Cell.merge переносит content к origin, Package.save сериализует обнаруженные parts. Это подсказка для regression, не доказательство версии на пользовательском Mac.
