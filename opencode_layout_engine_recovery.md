# OpenCode: исправить ложную «плотность», перейти к полноценной композиции и завершить дек

Продолжи текущий run и существующий проект. Прежний `opencode_general_pipeline_and_acceptance.md` с обязательным design cycle остаётся общей спецификацией; этот промпт задаёт приоритетные исправления после фактической проверки `review_increment_system.zip`. Не открывай новую стадию согласований. Сначала устрани ошибки представления и layout, затем исполни native build/render/review/repair/selection всей презентации. Остановиться на фразе «контент плотный, нужен новый этап» нельзя.

## 0. Цель и обязательные границы

```text
project_root = /Users/wysockii/Documents/gpn_slides
code_root = project_root/gpn-restyler
source_pptx = project_root/NTC_business_AI_GPN.pptx
run = project_root/runs/stage7/2026-10-13-135452
ontology_dir = project_root/ontology
examples_dir = project_root/slide_examples
```

Главный результат этого цикла: хорошо спроектированная полная восьмислайдовая корпоративная презентация с native editable text/diagrams/data, сохранёнными фактами/оговорками/отношениями и реальными разными вариантами. Входное количество shapes не ограничивает количество output blocks. Второй input document/prompt продолжает использовать тот же target authoring engine.

LLM provider default `harness`: current онлайн-модель OpenCode сама выполняет generation/repair/review/selection tasks. `local_server` предусмотрен, сейчас deferred; сервер/credentials/API calls не создавать. Используй имеющиеся GPN fonts на Mac.

Все нормы — из JSON `ontology/`; only owned `slide_examples` как design corpus. No normative literals в code/skills, no foreign agent-slides examples. Source/ontology/examples/fonts immutable. Existing correct import/OPC/assets/task idempotency fixes сохранить. Не reset/rebase/удалять dirty changes, commit/push не запрошены.

**Принцип:** сохраняются смысл, exact required spans, данные, изображения и связи; исходные textbox boundaries/blank decor/порядок source shapes не являются будущей композицией. Культура верстки означает работу с semantic groups, иерархией, relations, column widths, inline/paragraph roles и реальными footprints. Это не упаковка каждого исходного объекта в отдельный прямоугольник.

Не снижать нормативные кегли, не включать shrink-to-fit, не резать required content, не дробить исходные 8 slides ради ограничений solver. Настоящую невозможность можно заявлять только по доказанному ограничению с явным scope. Ошибки engine/schema/поиска — не доказательство высокой плотности материала.

## 1. Проверенные факты, которые нельзя игнорировать

### 1.1. Сколько материала в исходнике

Подсчитан текст `source_ir.json` во всех непустых text-bearing source objects, включая titles/source lines/footers. Words — диагностический `.split()` count, не универсальный критерий вместимости; image text и notes в него не входят.

| Слайд | Source objects | Text-bearing objects | Абзацы | Символы | Слова |
|---|---:|---:|---:|---:|---:|
| 1 | 18 | 9 | 12 | 361 | 52 |
| 2 | 66 | 26 | 29 | 738 | 120 |
| 3 | 58 | 27 | 29 | 793 | 119 |
| 4 | 37 | 27 | 30 | 674 | 101 |
| 5 | 75 | 27 | 28 | 637 | 101 |
| 6 | 21 | 15 | 23 | 567 | 81 |
| 7 | 23 | 15 | 17 | 770 | 96 |
| 8 | 29 | 18 | 34 | 1383 | 213 |

Общая «чрезмерная плотность» не установлена. Например, на слайде 2 текущий plan содержит 64 source refs/zones, в том числе 37 geometry-only figures. Число 64 не означает 64 содержательных блока. На №3 —55 refs, включая 27 geometry-only figures. На №5 —75 source objects, но лишь 27 text-bearing объектов и 101 слово.

Нельзя оценивать content density по количеству source shapes. Geometry fragments vector icons, borders, decorative cards, separators и paragraphs имеют разные функции и разную стоимость места.

### 1.2. Воспроизведена конкретная ложная «невместимость» №6

Текущий content box, использованный для диагностического воспроизведения, —960×496.677874 pt. В plan две разделительные **линии** получают `h_pct=6.4`, то есть bbox height31.787384 pt. Это не их stroke thickness.

Пять нижних nodes текущего c1:

| Node | Назначение | y/h, pt |
|---|---|---|
| `c-1403640b` | первая линия `z-rule` | 337.741 / 31.787 |
| `c-1b6f8cac` | «Для нас результат — решение, которое работает у бизнеса» | 360.091 / 54.635 |
| `c-92feee83` | вторая линия, тоже `z-rule` | 419.693 / 31.787 |
| `c-8af260bb` | подвал НТЦ | 461.910 / 23.841 |
| `c-9d487b7e` | номер/ГАЗПРОМ НЕФТЬ | 461.910 / 23.841 |

Вызван реальный `layout._resolve_hint_overlaps` с этими boxes. Результат:

```text
outcome y: 360.09 → 376.73 из-за первой «толстой» линии
second rule y: 419.69 → 438.56 из-за moved outcome
footer/page: no_room_below из-за moved second rule
```

Контрфактический geometry-only probe с line footprint height1pt вместо 31.8pt даёт **zero moves/zero reported conflicts** у этих пяти nodes при неизменных текстовых boxes. Это diagnostic counterexample, не требование установить всем корпоративным линиям толщину 1pt: production thickness берётся из resolved stroke/ontology/source policy.

Таким образом, footer failure №6 воспроизводится ошибочным представлением линий как занимающих место 32pt прямоугольников. Он **не доказывает** нехватку места для 81 слова. Probe не заменяет actual-font full slide check: после правильной geometry нужно исполнить его на Mac.

### 1.3. Solver умеет сдвигать только вниз

`_resolve_hint_overlaps`: `new_y = other.y + other.h + 7.2`, x/width/topology не меняются. Реальный independent helper test:

```text
region:400×100pt
A:(x0,y0,w100,h100)
B:(x80,y0,w100,h100)
current helper → no_room_below, overlap остаётся
feasible B:(x120,y0,w100,h100) → оба помещаются и не пересекаются
```

Это прямой counterexample выводу «no room below → слайд infeasible». Движок не проверил свободное место справа и не сменил arrangement. Listing `reflow/reorient/switch_candidate` в `ALLOWED_ATTEMPTS` не является реализацией этих действий: actual loop выполняет преимущественно expand вниз/вправо, после первой попытки может break.

### 1.4. «Бюджет исчерпан» при 0/6 и 1/6 — неверная диагностика

Current strings объявляют `budget 0/6 exhausted` или `1/6 exhausted` при early residual overlap/early break. Сохраняй actual consumed counters/termination reasons. Unimplemented search action, fixed geometry collision и действительно spent budget — разные результаты.

### 1.5. Все восемь c1 plans invalid по приложенной schema

В plan zones присутствует `min_h`. Actual `LayoutZone` его не содержит и имеет `extra='forbid'`. Выполнена validation реальных class bodies из предоставленного `planner.py` установленным Pydantic:

```text
slides1–8: extra_forbidden at zones[*].min_h
error counts:14 /64 /55 /37 /40 /21 /23 /29
```

При этом runner записал `scene_validated` без вызова этой schema. В measured path `LayoutIntent.model_validate` обязан отказать на тех же bytes. В ZIP нет raw `probe/candidate_evaluations.json`/measurement inputs/attempt files, поэтому источник расхождения diagnostic probe с archived plans не установлен. Не выдумывать историю «всё дошло до измерителя»: покажи exact plan hash/code hash, а schema error исправь первым.

Также на №6 два zones с одинаковым `zone_id='z-rule'`; uniqueness не проверяется. Не назначать новое default значение неизвестному `min_h` наугад: оно не имеет declared units/semantics. Если нужен minimum, объявить typed `minimum_height_pt`/geometry constraint и включить его в actual schema; иначе убрать legacy helper field из generation specification и исправить response through contract.

### 1.6. Scene tasks не получают свои concepts

Все scene_generation packets c1/c2/c3 каждого слайда после исключения `concept_id/concept_key` **побайтово эквивалентны по canonical JSON**. Поля actual visual concept нет; actual design brief также отсутствует во всех 24 packet documents. `_scene_generation_task` ищет brief file, но не включает `concept.json`.

Модель могла помнить authored concepts в текущем контексте, но pipeline этого не гарантирует. «41 tasks consumed» не доказывает, что scene выражает именно выбранную concept topology. Требуемая передача входов — explicit hashes/payload refs, а не надежда на память сессии.

### 1.7. Surface contract снова теряется downstream

`compile_composition` переносит refs/role/relative_box, но не zone surface/foreground/stroke. `_build_resolved_slide` пересоздаёт zones только с role/content_refs. `emit_slide` пытается прочитать tokens из этих resolved zones, где их уже нет.

Вызов реального `_build_resolved_slide` с node declaring blue/white/line вернул zones без всех трёх токенов. Function declarations нового surface contract не доказывают прохождение through measured path. Исправить полный callgraph, иначе после лечения geometry опять получится бесцветная раскладка или тёмный текст на поверхности.

### 1.8. Остальная evidence

Проверены 357 bundle manifest entries — size/hash совпадают. Runner имеет 41 unique consumed tasks: semantic1, briefs8, concepts8 ответов с 24 concepts, scene24. Но state остаётся build, `solves_used=0`, `renders_used=0`, no accepted product. Он не отражает meaningful failed-candidate repair, raw measurement evidence в bundle нет.

Большая часть SnapshotSet пустая: ontology/rules/examples/fonts/frame/code hashes. Заполни их actual values до новых tasks/cache/review. Frame в приложенном template profile всё ещё `is_verified=false`, `content_box=null`; слово «верифицированный» в комментарии не является доказательством.

Пакет содержит generic skills с конфликтующими corporate defaults: `slides-build/references/content-density.md` требует body14–16pt/min14pt, иных цветов и допускает autofit/split. Это противоречит loaded GPN profiles. **Не доказано**, что этот skill был активен в текущем run; audit actual reads/routing и исключи его нормативное влияние из GPN path. Не полагаться на устную формулу «онтология важнее», если agent уже получил другую instruction.

## 2. Немедленная ревизия диагностики и схем

Изменить `restyle_runner.py`, `planner.py`, `models.py`, `design.py` и actual task schemas:

```python
validate_scene_response(
    response: ModelTaskResponse,
    task: ModelTask,
    semantic: SemanticSlide,
    concept: VisualConcept,
    rules: CompiledOntology,
) -> SceneValidationReport
```

Checks: actual payload schema; unique IDs; correct candidate/slide/revision; required spans/relations coverage; allowed role/surface tokens from loaded JSON; valid units/finite geometry; parent/container relationships; concept SHA/topology linkage; no source ID used as semantic group by default.

`scene_validated` ставится **после** report.valid, не после сохранения dict. Invalid fields → `schema_repair` task/current harness response, не downstream build exception. Не включать `extra='ignore'` для молчаливого удаления model constraints. Source/semantic units и layout nodes имеют разные namespaces.

Materialize `design_briefs/<slide>.json` и concepts с hashes. Request packet path уникален на task/candidate/revision; нынешние общие `design_brief.json` и `visual_concepts.json` не должны overwrite inputs других slides. Store exact packet at creation and canonical fingerprint.

Scene task включает complete `design_brief`, `visual_concept`, semantic projection, actual JSON-schema, applicable rules/role/palette/spacing constraints, supported native primitives, verified frame and owned example refs. Merely required_fields/schema ID string insufficient.

Сначала revalidate archived24 scenes. Старые ответы/consumed count не переписывай; append invalidation/reconciliation event с actual plan/code snapshots и new revision tasks. Reuse genuine semantic/source data, если их независимая проверка passed, а не начинать project заново.

## 3. Основное исправление: authored blocks вместо source-object boxing

### 3.1. Canonical authored scene

`compile_composition` больше не создаёт leaf на каждый content_ref из source. Модель/semantic compiler создаёт target block/tree по visual concept; source refs — bindings/provenance.

```python
AuthoredScene:
    candidate_key: CandidateKey
    concept_sha256: str
    frame_id: str
    root_id: str
    nodes: dict[str, AuthoredNode]
    bindings: list[SpanBinding]
    relations: list[AuthoredRelation]
    snapshots: SnapshotSet

AuthoredNode:
    node_id: str
    kind: 'rich_text' | 'image' | 'table' | 'chart' | 'path' | 'surface' | 'container'
    parent_id: str | None
    children: list[str]
    layout_spec: NodeLayoutSpec
    text_spec: ResolvedTextSpec | None
    geometry_spec: GeometrySpec | None
    surface_spec: ResolvedSurfaceStyle | None
    semantic_unit_ids: list[str]
    source_bindings: list[SpanBinding]

SpanBinding:
    source_unit_id: str
    source_object_id: str | None
    source_subpath: str | None
    source_token_range: TokenRange | None
    target_node_id: str
    target_token_range: TokenRange | None
    transformation: 'exact' | 'normative_transform' | 'semantic_representation'
    authorization_ref: str | None
```

Reuse existing equivalent models, не плодить параллельный exporter. Containers/regions не are drawable obstacles без отдельной surface/path. Node ID — stable full identity, не `c-ref[:8]` для нового source-independent authoring.

### 3.2. Rich text с независимыми run/paragraph roles

Нужна настоящая сборка **нескольких source spans в один native text frame**, где это соответствует смыслу. Не concatenation всех слайдовых строк в один blob.

```python
TextBlockSpec:
    node_id: str
    paragraphs: list[ParagraphSpec]
    frame_insets: InsetsSpec
    wrap_policy: WrapPolicy
    anchor: VerticalAnchor
    bindings: list[SpanBinding]

ParagraphSpec:
    paragraph_id: str
    tokens: list[InlineToken]
    semantic_role: str
    spacing: ParagraphSpacingSpec
    alignment: str
    indentation: ResolvedListIndent | None
    list_profile_ref: str | None

InlineToken:
    kind: 'text' | 'field' | 'soft_break'
    text_or_cached_value: str
    role_ref: str
    source_span_ref: SourceSpanRef
    resolved_style: ResolvedRunStyle
    field_metadata: FieldMetadata | None
    hyperlink: HyperlinkIR | None
```

Алгоритм `compose_text_block(block, source_ledger, rules, context) -> ResolvedTextSpec`:

1. По binding order извлечь immutable source tokens/paragraphs, fields/breaks/hyperlinks. Использовать source IR/ledger token order, не `.join` text fields без абзацных границ.
2. Сгруппировать compatible body lines/list items/label+context по accepted concept. Grouping semantic/role-aware: number, label, caveat могут иметь разные styles внутри block. Раздельная геометрия остаётся там, где требует encoding/alignment.
3. Решить explicit paragraph/run boundaries и separators. Добавляемые structural separators имеют отдельную provenance; original required spans сохраняются exact. Uppercase/другие normative transforms — только с rule authorization.
4. Резолвить каждую role отдельно из JSON/font policy: style одного контейнера не размножается на всех потомков. Synthetic bold не включать. Text field stays field in saved OOXML.
5. Frame padding применяется **один раз** для authored block. Paragraph spacing/list indents/tabs управляют internal flow; не добавлять каждому исходному textbox ещё margins+7.2pt gap только из-за его source ID.
6. Сформировать exact spec/hash и источник каждого property; один spec используют Chromium/native. Пересборка не меняет числа/условия/links.
7. Coverage проверяется по source required tokens/spans/relations, а не по количеству shapes/параграфов.

Source-many→target-one и source-one→target-many должны реально работать через saved address/subpath. Три исходные body textboxes могут стать тремя paragraphs в одном frame. Heading/value остаются отдельными nodes или rich runs/paragraphs согласно design; размер/face не меняется ради fit.

### 3.3. Native writer и comparator

В `native.py` реализовать/расширить:

```python
emit_rich_text_block(spec: ResolvedTextSpec, box: RectEMU, slide, ctx) -> TextEmission
emit_scene(scene: ResolvedScene, semantic_slide, slide, ctx) -> SceneEmission
compare_span_preservation(expected_projection, saved, emission_map) -> ContentReport
```

Writer проходит target nodes, не обязательный source-object loop. `emit_rich_text_block` creates one p:sp/txBody, writes ordered p/r/fld/br с resolved per-run/per-paragraph properties, noAutofit и exact insets. Emit shape/address map на каждый source span, target paragraph/run/cell/field ID. Hyperlinks owner-scoped, notes/media/workbook contracts сохраняются.

Comparator many-to-one: читать **конкретный bound target span**, не полный merged textbox и сравнивать его с каждой исходной строкой. Проверить ordered exact coverage/duplicates/missing spans, field metadata/links/numbers/conditions. One native shape может покрывать много atoms, без ложного duplicated. Authorized repeat separately recorded.

Legacy per-source emit допустим как явно declared style_only/transplant support branch, но не default public recompose/create path. Не создавать dummy source PPTX для документов, чтобы удержать старый shape loop.

Для geometry-only source objects: confirmed optional decor can omit/replace по individual authorization; arrow/diagram/meaningful boundaries должны сохранять required relations через new path/sequence. Не возвращать все декоративные line fragments источника ради object-count equality, и не удалять required arrows blanket.

## 4. Разделить region, native frame и нарисованный footprint

### 4.1. Три геометрических сущности

```python
ResolvedNodeGeometry:
    layout_region_pt: RectPt
    native_frame_emu: RectEMU | None
    visual_footprint: VisualFootprint
    coordinate_space: str
    transform: AffineTransform
    geometry_origin: str

VisualFootprint:
    kind: 'text_fragments' | 'stroked_path' | 'opaque_area' | 'image_area' | 'none'
    shapes: list[FootprintShape]
    effective_bounds_pt: RectPt | None
    z_order: int
    opacity: float
    evidence_refs: list[str]
```

Layout region — область для размещения группы. Native text frame — editing/wrap geometry. Footprint — реально занимающий визуальное место текст/path/area. Не сравнивать всю region как opaque painted rectangle независимо от kind.

При отсутствии background у textbox его пустая область сама не закрывает соседнюю подпись. Но glyph/line fragments, wraps, clipping и semantic relationship контролируются. При strict rule запрещающем именно frame overlap применять его с declared scope; не выдавать такое требование за физическую плотность.

Containers не рисуются автоматически. Panel surface is отдельный node/underlay, children can be inside it; painter order и containment подтверждены, unrelated text overlap запрещён. Не применять blanket allowed-overlap clique ко всем children, чтобы скрыть плохие подписи.

### 4.2. Линии/стрелки

```python
StrokePathSpec:
    points_or_segments: list[PathSegment]
    stroke_token: str
    resolved_width_pt: float
    start_arrow: ArrowSpec | None
    end_arrow: ArrowSpec | None
    route_constraints: list[RouteConstraint]
    semantic_relation_ids: list[str]

compute_stroked_path_footprint(path, transform) -> VisualFootprint
emit_native_path(path, geometry, slide, ctx) -> PathEmission
```

Алгоритм:

1. Из source/target relation определить true path endpoints/route; не body text role для empty line.
2. Резолвить stroke width/color/arrowheads по applicable JSON/native policy; no default full-height card box.
3. Apply group/local rotation/flip transforms, calculate conservative stroke envelope и arrowhead footprint по actual properties. Horizontal line has zero centerline height и ненулевой stroke envelope; не `floor(height,1 inch)` и не universal h_pct6.4.
4. Allocation/obstacle processing uses этот footprint или declared routing reservation, не весь zone вокруг line.
5. Native writer emits actual line/connector/path со стрелкой и correct geometry. Reopened OOXML/PNG prove width/direction. Decorative separator doesn't become stroked rectangle автоматически.
6. C04 collision может потребовать не проводить line поверх unrelated caption; routes can be adjusted within free space. Arrow preservation проверяется semantic relation + visible direction, а не presence some p:sp.

У №6 убрать source-derived minimum-height text policy с обеих `z-rule`; исправить duplicate zone IDs. Ни одного текста/кегля не меняется ради этого исправления. Параметр 1pt из diagnostic witness не переносить в production как новая норма.

### 4.3. Surfaces и контекстный foreground

Пронести `surface_token/foreground_token/stroke_token` через `AuthoredNode → Component/ResolvedScene → native emitter → saved_style_check`. Не собирать resolved zones заново с тремя полями и потерей стиля.

Ввести `resolve_surface_style(tokens, rules, usage_context) -> ResolvedSurfaceStyle`; conditional token требует applicable context evidence. Одна версия разрешённого RGB/opacity/stroke находится в resolved style contract, provenance исходного JSON сохраняется. Parent surface может влиять на contrast, но не blanket менять все child roles.

Regression end-to-end: поменять token value в test ontology без Python edit → actual native fill/foreground/stroke меняются; content/geometry без непредусмотренных изменений. Reimport confirms written values; render shows readable contrast. `noFill` допускается по undeclared/explicit transparent surface, а не как универсальное снятие всех панелей.

## 5. Типографика измеряется как authored block, без накопления служебных расходов

### 5.1. Один exact contract для browser/native

Use `ResolvedTextSpec` §3 для measure и writer. Current `_object_paragraphs` подставляет 1.15/left/no indents; новые specs должны отражать resolved paragraph style/list/tabs/fields/baseline/margins из JSON/effective OOXML/context. Не изобретать другой formatter только для замера.

```python
TextMeasure:
    request_id: str
    request_sha256: str
    node_id: str
    offered_frame_pt: RectPt
    inner_box_pt: RectPt
    content_bounds_inner_pt: RectPt
    required_outer_height_pt: float
    intrinsic_min_width_pt: float
    paragraph_boxes: list[ParagraphMeasure]
    line_boxes: list[LineMeasure]
    glyph_fragments: list[GlyphFragment]
    face_evidence: list[FontEvidence]
    overflow: SizeDeficit
```

Height semantics declared explicitly. Сейчас JS `occupied_height_pt=max(fragment bottom)` вычисляется от **outer component origin**, поэтому включает верхний padding/offset; same-zone allocator затем добавляет top+bottom margins снова. Устрани mixed coordinate semantics, не просто вычти 3.6 константой.

Выбери единый контракт: measurement возвращает content extent relative inner origin; outer required height = topInset + contentExtent + bottomInset + validated calibration uncertainty. Если измеритель возвращает outer height, повторно insets не добавлять. Paragraph spacing считается ровно один раз, особенно при rich text assembly.

Actual extent должен учитывать line boxes/before/after/list tabs/baseline/fields, а не только extrema видимых glyphs. `line_count` — число logical paragraph lines, не сумма lines каждого run на одной строке. Native PDF/glyph line evidence связывается с тем же block/paragraph для проверки.

### 5.2. Пространство внутри semantic block

Между отдельными source strings нет обязательных margins/gap только потому, что раньше они были shapes. В одном native paragraph run adjacency не требует inter-block gap. Между paragraphs spacing зависит от semantic structure/style policy.

Между authored groups gap/padding — explicit layout design variables/constraints. Values from ontology если они нормативны; иначе accepted concept/search policy with origin. Не global `[7.2]*(N_source_objects-1)`.

`measure_rich_text(spec, width)` отвечает за line wrapping/actual needed height. `solve` выбирает width по group/composition constraints, после изменения width высоты измеряются заново. Number+unit unbreakable policy сохраняет смысл/переносы, а не удаляет source tokens. Табличные cells measured at actual spanned widths with actual tcPr, не whole table paragraph flow.

### 5.3. Сохранность и реальные шрифты

Loaded exact GPN binaries + glyph coverage, no synthesis/fallback. Wrong/missing response component IDs/fonts/hashes → unknown/fail, не fabricated12pt defaults. Все request/response автоматически persist, including saved remeasure, не только debug HTML.

Reader реконструирует actual saved spec через existing inheritance chain. Expected/spec mismatch блокирует, even if PNG readable. Fields/br/tabs/paragraph ordering и source spans проверяются на saved tokens. Native actual wrap/clipping finding имеет приоритет над browser-only pass.

Styles/kegli фиксированы применимыми нормами; solver меняет group arrangement/width/paragraph flow/role-appropriate structure, не font_scale. Footnote role нельзя назначить body для обхода вместимости; dense/caption variant допустим только с actual applicability.

## 6. Иерархический layout solver вместо одной стопки

### 6.1. Контракты и алгоритмы

```python
measure_scene(scene, frame, exact_specs, measurer) -> SceneMeasurements
solve_scene(scene, frame, measurements, policy, budget) -> LayoutSolveResult
solve_flow(container, children, measurements, constraints) -> ContainerSolution
solve_grid(container, children, measurements, constraints) -> ContainerSolution
solve_relations(container, graph, measurements, constraints) -> ContainerSolution
check_visual_collisions(resolved, actual_footprints, relation_policy) -> CollisionReport
```

`NodeLayoutSpec` имеет explicit organization row/column/grid/overlay/nest/graph/free-anchor, child IDs, width constraints, alignment/baseline, named region, padding/gap constraints, hard/soft origins. Percentage hints — предпочтения, не immutable source rectangles, если модель не объявила hard anchor согласно правилам.

Resolve tree top-down/bottom-up:

1. Получить проверенные header/content/source/footer/protected regions и styles. Не утрамбовать их вместе в одну root v-stack.
2. Для каждого leaf определить intrinsic min-width, needed height at candidate width, path/image footprint и attachment constraints.
3. Для row/grid распределить widths: суммы min widths+gaps/padding должны помещаться; available slack распределяется по intended emphasis/flex, не по числу исходных refs. Затем remeasure heights при выбранной width; align children by declared baseline/edge.
4. Для column нужны sum authored child heights + explicit inter-group gaps + one container padding. Source object count здесь отсутствует. Container height derived from children/allowed stretch, не source fixed panels.
5. Для nest: parent surface remains underlay, children layout внутри available region, move parent/children как unit. Overlap validation отдельно по footprint/z-order и allowed relation. Не требует all-pairs clique как единственного способа сохранить containment.
6. Для graph/process: связанный diagram subsolver, измеренные label nodes, edge routing и preserved directed relations. Arrow paths не забирают 32pt body slots.
7. На выходе exact assigned boxes, constraints evidence и conflicts. Violation на root group → выбрать rearrangement на group level, не двигать каждый run вниз.

Typed layout regions/body figures/mixed typography поддерживаются существующими native primitives; no second HTML-to-raster output branch.

### 6.2. Repair/search

При конфликте:

1. Определить smallest affected semantic/container subtree и class defect: bad footprint, unit/style measurement mismatch, excessive inherited padding, incompatible constraint, candidate topology failed.
2. Попробовать legal local actions: compact authored paragraph flow without font/content changes; revise explicit flexible gaps within allowed range; width redistribution/remeasure; side placement; row↔column/grid restructuring **если** сохраняет design/reading constraints; reroute edge; resize legitimate container; move group to free region.
3. Сохранять provenance/deltas и hard constraints. No hidden overlap, crop, clamp, source fallback.
4. Если legal geometry search для данного concept не нашёл решения, return structured `needs_recompose` task with measured affected units/regions/deficit + prior concept. Current harness делает новый structural plan; restart affected measured/native/render checks.
5. Evaluate other candidate concepts **самостоятельно**. Не объявлять все 8 infeasible, когда failed один составленный primary deck.

Используй existing подходящий constraint implementation, если имеется; иначе реализуй bounded hierarchical flow/grid with explicit search actions. Не обязательно внедрять большой внешний optimizer. Но названия `reflow/reorient` без actual behavior не считать implemented.

`_resolve_hint_overlaps` можно оставить только ограниченным helper для declared column flow. Его `no_room_below` становится local conflict with `directional_search_only=true`, не `CONTENT_INFEASIBLE` для всего слайда.

### 6.3. Не лечить конфликт opacity/empty area blanket allowances

Broadphase bbox сообщает possible intersection; narrowphase различает actual text fragments/strokes/opaque fills. Explicit surface-child containment permitted; unrelated text glyph overlap fails. Empty transparency не даёт право спрятать required content under another opaque node.

Container reservation и drawable obstacle различны. Parent+child footprint union не double-counted в capacity audit. Fixed footer защищённым region остаётся, но separator's reserved body-height — не неподвижная норма. Freed space от good grouping должно оставаться реальным, не увеличивать random fillers до 60% площади из чужого skill.

## 7. Feasibility — с ограниченным scope, никаких недоказанных диагнозов

```python
LayoutSolveResult:
    status: 'feasible' | 'schema_invalid' | 'unsupported_representation' | 'constraint_conflict' | 'needs_recompose' | 'search_exhausted'
    scene_scope: CandidateKey
    resolved_scene: ResolvedScene | None
    attempted_actions: list[LayoutActionEvidence]
    conflicts: list[ConstraintConflict]
    consumed_budget: BudgetDelta
    termination_reason: str
    feasibility_proof: FeasibilityWitness | None

CapacityAudit:
    source_text_characters: int
    required_semantic_units: int
    authored_text_blocks: int
    source_geometry_fragments: int
    painted_footprints: list[FootprintRecord]
    text_content_cost_pt: list[MeasuredTextCost]
    frame_inset_cost_pt: list[InsetCost]
    paragraph_spacing_cost_pt: list[SpacingCost]
    inter_group_gap_cost_pt: list[GapCost]
    rigid_reservations_pt: list[RegionReservation]
    unsupported_or_unchecked: list[str]
```

Audit отвечает, что реально занимает место, где оплачиваются margins/gaps и какие constraints мешают. Area/word ratios — diagnostics, не критерий «всегда влезает/не влезает». Source text density and engine representation overhead разделены.

Если применяется formal UNSAT checker, proof относится к exact stated constraint set/candidate, с implementation/version/input hash. Не распространяется на все возможные semantic compositions. Для обычного bounded heuristic search честный результат — search_exhausted/needs_recompose, не global physical impossibility.

Запретить нынешний `LAYOUT_DENSITY_INFEASIBLE` как summary этих восьми неразобранных случаев. Mapping existing failures:

- unknown `min_h` → schema_invalid;
- line/card footprint ошибка → representation/geometry defect;
- source1:1 block restriction → unsupported authoring representation;
- no room below → constraint_conflict/directional search limitation;
- failed one candidate → needs_recompose;
- реально использованы allowed attempts → search_exhausted с exact counters.

Не писать `budget0/6 exhausted`. В диагностике не советовать меньший шрифт/удаление/новые slides при наличии неисследованных legal layouts. Это устраняет неверные комментарии **через правильные причины и алгоритмы**, а не цензуру честного failure.

## 8. Исполнить исправления через runner, не остановиться на новом отчёте

### 8.1. Поиск по отдельным candidate scenes

Current `_build_all_deck_candidates` собирает c1 на всех 8, затем c2 только одного слайда с c1 на остальных 7; c3 оставлены «про запас». При failures c1 на других slides такой deck не позволяет проверить, пригоден ли новый c2. Это неправильная гранулярность candidate search.

Добавь/расширь:

```python
build_candidate_scene(
    key: CandidateKey,
    scene: AuthoredScene,
    context: CandidateBuildContext,
) -> CandidateAttemptResult

evaluate_slide_candidates(slide_id, attempts, responses, policy) -> SlideSearchResult
assemble_selected_scenes(selection, scene_refs, context) -> CandidateRef
```

Candidate attempt хранит schema/measurement/layout/native/render findings и scoped eligibility именно этого candidate. Solve per slide/concept отдельно; native preview может быть isolated slide или context-preserving package с явно declared single-slide review/gate scope. Internal links/transplants requiring final deck имеют declared final-assembly checks, не молчаливый pass. Неуспех unrelated c1 других slides не делает candidate c2 infeasible.

Реально рассмотреть c1/c2/c3 в исходном search budget; не ограничиваться c2 full-deck swaps. Если concept failed, сохранить structured findings, выполнить available repair/new-concept task, сравнить реальные render alternatives. Финальный пакет собирается **после** selection из resolved scenes победителей, затем complete gates/render/review всех 8 и publication.

### 8.2. Сам переход build → repair

`_advance_stage` при failed build сейчас может остаться `stage='build'` без pending tasks и ясного resume action. Исправь: каждый repairable failure переводится в actionable candidate state, scheduled task или deterministic next search action. `state.findings` и return AdvanceResult отражают same findings; logs include root cause/affected units/attempt evidence.

Failed schema → contract repair; incorrect path footprint/style loss → engine fix/invalidation; valid scene with local conflict → group solve/recompose; truly spent budget → candidate rejected or explicit search_exhausted. Не повторять тот же 9-deck build в каждом resume без нового scene/code hash.

Current harness обязан прочитать/ответить tasks и продолжить resume. `accepted_scene отсутствует`, `нужно объединять textboxes`, `следующая стадия` — не external blockers. Rich authoring implementation уже часть этого задания.

### 8.3. Budgets и snapshots

Счётчики model tasks, local solves, candidates, native renders increment по actual actions and persisted state. Поддержи reuse валидных measurements по exact payload/style/font/frame/engine hash; opaque counters не замещают proof.

При необходимости расширить search budget из-за revalidation24 invalid plans, сделай это explicit technical policy с recorded reason/old/new values. Не обнулять уже spent counters. Generation repairs могут быть пакетными, но каждый response/candidate/revision identity сохраняется.

Заполнить source/semantic/ontology/compiled/examples/fonts/frame/code snapshots реальными SHA. Brief/concept/task filenames unique. Code changes invalidate dependent old measurements/resolved outputs/reviews; genuine content projection может reuse после independent exact verification. Cache не resurrect invalid geometry.

### 8.4. Обязательный стандарт композиции

Устранение overflow — технический минимум, а не критерий хорошего слайда. Новый authoring path обязан создавать осмысленную композицию. Для каждого слайда модель явно отвечает:

1. Что зритель должен понять первым? Это reading objective/semantic focus, не разрешение переписать исходный заголовок или факты.
2. Какие смысловые группы существуют и каковы отношения: сравнение, последовательность, причина–следствие, компоненты системы, иерархия, значение+подпись, тезис+доказательства? Группы выводятся из содержания, а не из количества source textboxes.
3. Какая структура делает эти отношения видимыми: сопоставленные колонки, timeline/process, native diagram, aligned metric-label row, матрица/таблица, несколько параграфов в одном блоке? Использовать только подходящие поддержанные примитивы и разрешённые ontology profiles.
4. Как устроены фокус, вторичный уровень, caveats/source/footer; куда идут взгляд и чтение; какие края/оси/базовые линии выравниваются? Фактические роли и размеры — из JSON, emphasis не через произвольный крупный/мелкий шрифт.
5. Какие поверхности и линии выражают реальную группировку/отношения? Не разливать карточки по всем строкам и не украшать свободное место ради процента заполнения.

Эти решения фиксируются в `design_brief`/`visual_concept` и переходят в typed groups/relations сцены. Не достаточно заполнить текстовые поля rationale, если фактическая сцена снова стала source-ordered vertical stack. Проверять соответствие intent→group topology→render.

Три concepts должны различаться способом организации/визуального кодирования, а не только координатами, цветом, названием и ID. Например, для содержательного сопоставления могут быть согласованные колонки, матрица и акцентный вывод со структурированными доказательствами — только если каждый вариант честно сохраняет смысл. Не навязывать эти три всем слайдам. Если пригодна одна family, сравнить существенно разные способы работы с группировкой/акцентом внутри неё и объяснить ограничения; fabricated diversity запрещена.

Текстовый слайд может быть правильным. Но произвольное размещение строк, одинаковое оформление всех групп, потерянные связи, отсутствие выделенной структуры и сохранение фрагментации исходника не проходят design review только потому, что нет пересечений.

### 8.5. Выбор — по реальным изображениям и содержанию

После hard gates current harness как reviewer открывает native render каждого eligible варианта. Перед сравнением передай ему source meaning, ontology scope и owned reference evidence; при возможности скрыть ID/название предпочтённого candidate. Сохранить exact request/response и hashes просмотренных PNG. Не review HTML вместо сохранённого PPTX.

Записать оценки и конкретные наблюдения по шести осям: смысловая иерархия, группировка/видимость отношений, чтение/выравнивания, ясность при нормативной типографике, уместность визуального кодирования, цельность с остальным деком. Числовая rubric — configurable quality policy, не новая корпоративная норма и не автоматический сертификат качества. Каждая претензия/преимущество ссылается на область render/semantic units и приводит причину, а не «выглядит современно».

Hard violations не компенсируются красивым видом/суммарным score. Очевидно неудачные варианты отправить в исполненный repair; победителем не назначать первый eligible. Сохранить winning/losing native PPTX и render, причины отбора. Затем повторить просмотр всех восьми окончательных слайдов и общего contact sheet, чтобы сборка не внесла новые style/sequence/furniture дефекты.

Историческое `accepted=true` у отклонённого пользователем текстового результата не является visual baseline и не наследуется. Различать `automated_gates_pass`, `model_design_review_pass`, `release_ready` и `user_approved`: последнее никогда не ставится самой моделью. Новая техническая приёмка требует текущих images/reviews/repair evidence и не может обещать «идеальность» без фактического просмотра.

## 9. Устранить противоречащую культуру generic skills

GPN route использует project `gpn-restyle` и procedures, согласованные с ontology JSON. Generic build/polish/critique instruction не получает права устанавливать body14pt/min14pt, чужие colors, six-bullet cap, source split, fill60%, autofit и обязательные карточки.

Действия:

1. Audit project instructions/actual skill loader/read trace для GPN tasks; source paths/hash каждого реально прочитанного design reference.
2. Если generic skills доступны и автоматически применяются, route GPN только в own approved procedure или отредактировать GPN-scoped wrapper исключающим conflicting defaults. Open-source core API code сохраняется; чужие examples не возвращать.
3. Документация technical API может читаться, но не как style guide. Не удалять полезный engine code/все dependencies ради isolation.
4. `gpn-restyle` добавить правило: «source object boundaries не нормативны; проектируем semantic groups/rich blocks/paths; failure ограниченного solver не означает density; нормативные sizes не уменьшаем». Это procedure, а не дублирующая style table.
5. Контент более плотный не равен плохому слайду. Hierarchy/grouping/data encodings/paragraph flow делают его читаемым внутри правил. Не мешать content-slide с appendix; не выставлять arbitrary word-count cap как норму.
6. Artifact `design_reference_access_report.json` показывает разрешённые actual reads и blocked foreign design references. Не заявлять установленную причинную роль generic skill без trace: найдена конфликтующая доступная инструкция, её влияние нужно исключить доказательно.

Проверить actual discovery/invocation project skill. Его описание должно соответствовать runnable commands/functions; no claim implementation по наличию Markdown.

## 10. Проверки: маленький слайд и втрое больше содержания

### 10.1. Обязательные regressions

Добавь независимые tests/probes для **причин**, не mirrors реализации:

1. Все archived c1 with `min_h` дают schema_invalid, не scene_validated. После actual schema/response repair новые native plans проходят exact declared units/unique IDs.
2. Actual №6 five-node lower-band witness §1.2: path stroke footprint снимает ложные conflicts при unchanged text/style/font. Не blanket allow-overlap для всех nodes.
3. Region400×100 с двумя 100×100 objects §1.3: legal row placement найден, no global infeasible от отсутствия места снизу. Если current allowed layout явно запрещает row, result scoped conflict с этим constraint, не утверждение о физическом количестве текста.
4. Path `h=0`/rotated stroke/arrowheads имеет корректный visual envelope, group transforms правильные; no 6.4% body min-height.
5. Three body source objects → one native frame/three paragraphs с exact span/field/br/link coverage; reopened text/spec match и object count может отличаться.
6. Mixed metric/label/caveat roles в one rich block не превращают всю строку в large_metric; resolved measure/native per-run styles identical.
7. Source1→target-many disjoint spans и source-many→target1; comparator не сравнивает full target text с каждым source atom и не разрешает accidental duplicates.
8. Same padding считается один раз, spacing/gaps не зависят от количества source shapes; source chunking change при unchanged semantic text/grouping не меняет authored capacity.
9. Surface/foreground/stroke token end-to-end проходят composition→resolved→saved OOXML. JSON color mutation changes actual native style без Python edit.
10. Scene packet содержит actual selected concept/brief/schema/rules/frame snapshots; different concepts не отличаются лишь ID; overwrite общего packet filename rejected.
11. Repair of candidate c2 не blocked failures c1 other slides. c3 реально attempted/rendered when feasible; chosen candidate actually assembled.
12. Failed build creates next actionable repair/search state. Resume idempotent, old schema/engine inputs invalidated; no fake budget0/6 exhausted.
13. Opaque area/text glyph/path footprints collision correctly scoped; meaningful arrows preserved, broadpair allowance не скрывает unrelated labels.
14. Dense native table/list/paragraph with required size/LS01/noAutofit сохраняет content и остаётся editable; fields/hyperlinks/source references не теряются при merge.

Unit mocks допустимы для protocol/fault injection, но не выдавать их за GPN font/Chromium/native render acceptance.

### 10.2. Actual №6, затем все 8

Сначала инструментированный actual-font replay №6:

- corrected typed scene + separate line footprints;
- exact source spans/roles/font sizes unchanged unless legitimate role correction по JSON;
- CapacityAudit cost breakdown и constraint trace;
- native candidate.pptx, reopened span/style/geometry checks;
- LibreOffice/native render + current harness PNG inspection;
- concept c1/c2/c3 search без forced adoption карточной сетки; минимум две genuine viable topologies для comparative selection.

Затем полный NTC: source8 count/order/hidden/content/data/images/links/relations preserved, style/frame checks, solved scenes всех 8, native output/render, visual_design review, executed repair/selection. Для №3 тоже минимум два genuinely different eligible actual variants. Хорошее качество, а не просто «влезло».

### 10.3. Контроль 3× content

Создай explicit test-only dense fixture, не в `slide_examples` и не под видом реального бизнес-результата. Контролируй **содержательный body volume**, а не число decorative source shapes.

```python
DenseFixtureSpec:
    baseline_content_manifest: SourceContentManifest
    content_multiplier: int = 3
    include_tables_or_lists: bool
    required_style_profile: str
    expected_facts_manifest: SourceContentManifest
    manual_or_constructive_reference: FeasibilityWitness | None
```

Основной контроль — втрое больший содержательный body самого многословного слайда исходника (№8; полное текстовое содержимое сейчас 213 слов, body считать отдельно от header/footer). Создай три набора distinct test units с теми же типами фактов, абзацев, списков и оговорок. Измерь фактический body multiplier по словам/символам/required units, а не по source shapes. Header/footer не размножаются. Сохрани применимые roles/kegli/conditions и проверяемые relations/data identities. Новый fixture title явно маркирован тестовым; не публиковать фиктивные факты как настоящий NTC.

До проверки solver подготовь независимый legal native reference layout для этого контроля: те же content/fonts/canvas/style rules, полноценный render и читабельность. Это constructive feasibility witness. Если именно этот 3× dataset при всех применимых жёстких нормах доказанно не помещается, предъяви actual reference attempts и precise constraint, затем создай отдельный содержательно плотный 3× контроль с проверенным witness. Не заменять основной контроль утроением одной короткой подписи и не объявлять arbitrary dataset universally feasible. Малые rich-text группировки — дополнительные regressions, не замена dense slide.

Pipeline из этого source запускается **тем же public runner**, без bespoke engine patches под ID. Модель выбирает column/rich paragraph/table/structured layout по содержанию, measured solver строит, native renderer проверяет. В приложении actual exact fonts/specs; unknown/fallback fonts не production evidence.

Если выбранная topology не подходит, пытаться другую; no shrinking/no loss/no extra slides. Для fixture с заранее known valid reference layout обязателен feasible output. Это регрессия способности engine, а не бесконечный стресс-тест.

Не обещать, что любой 3× набор фактов всегда физически уместится при любых фиксированных нормативах: если genuinely oversized, вернуть scoped measured constraint evidence. Но сначала проверить typed authoring/footprints/margins и реально перебрать legal concepts. Density из source shape count или раннего directional failure не доказательство.

### 10.4. Fragmentation invariance

Один и тот же required content/grouping подготовь как:

```text
fixture A: один source textbox с несколькими runs/paragraphs
fixture B: те же tokens в нескольких source textboxes
fixture C: те же tokens + harmless fragmented vector decoration
```

При одинаковом accepted semantic/concept contract **измеренная потребность authored text и valid feasibility совпадают в пределах justified render tolerance**. Layout can vary только по explicit design choice, не из-за source box fragmentation. Required data/relations/image artifacts сохраняются; optional decoration ledgered. Это ключевой тест текущего root defect.

### 10.5. Общий writer и качество

Малый document/prompt input подтверждает, что rich target writer работает без source-object loop. Не делать второй simplified generator; previous dual-input/acceptance specification сохраняется.

Publish final только after current mandatory semantic/style/frame/fit/geometry/visual_design gates; old rejected162b9684 не accepted reference дизайна. Native editing copy: изменение run и geometry persists после reopen, media/workbook hashes/graph неизменны где требовалось. PowerPoint UI отдельно unverified до actual UI check.

Tests relevant changes, required pytest/ruff текущего дерева; exact command/log hashes/node IDs сохранить. Уже заявленные 352 passed не доказывают capacity/authoring behavior. No new test-count target ради отчёта.

## 11. Конкретный порядок и файлы изменения

| Место | Исправление |
|---|---|
| `models.py`, actual scene schemas | Authored tree/rich text/span bindings/layout region vs footprint/typed result/constraints |
| `planner.py`, `design.py`, `restyle_runner._scene_generation_task` | Actual schema validation; concept/brief/rules/frame payloads; unique immutable task packet paths |
| `composition.py` | Target nodes/groups вместо leaf per source ref; full style/surface propagation |
| `layout.py` | Rich-group measure/solve, 2D/hierarchical flow/grid/graph actions, footprint collision; truthful statuses/counters |
| `measurement.py`, `tools/layout/src/measure.js` | Exact text spec, declared metric coordinate semantics, padding once, logical line counting, complete requests/responses |
| `native.py` | Native rich p/r/fld/br emission, actual path/connector primitives, per-span addresses, no frame/line confusion |
| `export_pipeline.py`, provenance/comparator | Emission target-driven + many-to-one saved span preservation, assets/relationships/transplant preservation |
| `measured_build.py` | Candidate-scoped solve/check/report, raw evidence persistence, no final selection before image review |
| `restyle_runner.py` | Per-candidate attempts, build→repair continuation, actual budget/snapshot bookkeeping, c3 search и assembly chosen scenes |
| `gpn-restyle` routing/instructions | GPN-only normative authority, no generic style/density defaults, group authoring culture |
| `review_bundle.py` | Raw probe/attempt inputs, data/hash closure, no repeated9.8MB template copies без нужды |

Работать в этом порядке:

1. Reconcile текущую неверную density summary и missing raw evidence; record old claims without rewriting history.
2. Fix schema invalid scenes/duplicate IDs и concept/brief packet linkage; reset affected current candidate revisions with explicit invalidation.
3. Reproduce actual №6 path footprint issue; исправить geometry model и downstream style losses. Сохранить tiny causal regression.
4. Implement core rich target blocks/span-addressed writer и hierarchical layout; no source1:1 enforcement. Это обязательная реализация, не «следующий шаг».
5. Native №6 end-to-end with actual GPN fonts/render. Если он fails, root trace before speculating density.
6. Resume all24 concept candidates с actual per-slide search/repair, process current harness tasks. Full8 product не откладывать после пилота.
7. Actual3× dense fixture + fragmentation invariance tests + small shared create writer run.
8. Assemble winner scenes, final8 native render/review/repair/acceptance/publish. Save real alternatives.
9. Required tests/corpora hashes and complete review bundle §12. End concise output/path/status, не новая «завершённая стадия» без презентации.

Если equivalent functionality уже есть, использовать его в main callgraph и доказать прохождение; class/function name сама по себе не цель. Не добавлять очередной isolated module, который writer/solver не вызывает.

## 12. Файлы для следующего гейта

Один `review_increment_layout_recovery.zip` и при необходимости отдельный code ZIP. Общий manifest связывает оба. Не повторять base_template/profile/source IR в каждом variant directory: один content-addressed asset плюс resolved refs. ZIP этой итерации содержал многократные копии~9.8MB template при отсутствии actual render evidence.

### 12.1. Root cause и capacity

```text
audit/claims_reconciliation.json
audit/source_content_stats.json
audit/scene_schema_validation.json
audit/s6_rule_foot_collision_before_after.json
audit/directional_solver_counterexample.json
audit/scene_packet_contract_report.json
audit/resolved_surface_propagation_report.json
audit/design_reference_access_report.json
main/capacity_audit.json
main/layout_constraint_trace.json
main/termination_diagnostics.json
```

Для каждого infeasible/conflict scope: exact candidate/scene/code/frame/text hashes, actual measured need/region, involved node kinds/footprints, attempted actions/consumed counters и next repair. Без raw inputs нельзя подтверждать причинный диагноз.

### 12.2. Actual новые сцены и writer

```text
main/inputs/source_ir.json
main/inputs/source_ledger.json
main/inputs/semantic_deck.json
main/inputs/classification_and_transformation_ledger.json
main/inputs/ontology_manifest.json
main/inputs/ontology_snapshot.json
main/inputs/compiled_rules.json
main/inputs/verified_frame.json
main/inputs/font_inventory.json

main/candidates/<slide>/<candidate>/<revision>/design_brief.json
main/candidates/<slide>/<candidate>/<revision>/visual_concept.json
main/candidates/<slide>/<candidate>/<revision>/authored_scene.json
main/candidates/<slide>/<candidate>/<revision>/resolved_scene.json
main/candidates/<slide>/<candidate>/<revision>/resolved_text_specs.json
main/candidates/<slide>/<candidate>/<revision>/span_binding_map.json
main/candidates/<slide>/<candidate>/<revision>/footprint_report.json
main/candidates/<slide>/<candidate>/<revision>/measurement_requests/*.json
main/candidates/<slide>/<candidate>/<revision>/measurement_responses/*.json
main/candidates/<slide>/<candidate>/<revision>/layout_attempts.json
main/candidates/<slide>/<candidate>/<revision>/candidate.pptx
main/candidates/<slide>/<candidate>/<revision>/render/*.png
main/candidates/<slide>/<candidate>/<revision>/design_review.json
main/candidates/<slide>/<candidate>/<revision>/gate_reports/*.json
```

Selected scene/spec/binding всех 8; подробный before→after actual span/footprint/measure/native trace №6 и двух topology alternatives №3/№6. All requested paths exact; schema ID string не replace actual schema or validated response.

### 12.3. Полный продукт и исполнение

```text
main/final/NTC_business_AI_GPN_corporate.pptx
main/final/render/candidate.pdf
main/final/render/slide-01.png ... slide-08.png
main/final/render/render_manifest.json
main/final/render/render_font_report.json
main/final/render/native_render_probe.json
main/final/product_expectation.json
main/final/product_acceptance.json
main/final/gate_index.json
main/final/design_review.json
main/final/visual_review.json
main/final/selection_report.json
main/final/visual_design_gate.json
main/final/candidate_after_ir.json
main/final/content_diff.json
main/final/semantic_relation_report.json
main/final/part_preservation_diff.json
main/final/saved_span_style_report.json
main/final/saved_geometry_report.json
main/final/saved_text_fit_report.json
main/final/ontology_rule_coverage.json
main/final/native_edit_roundtrip_report.json

main/run_state.json
main/runner_transition_log.jsonl
main/model_requests/**
main/model_responses/**
main/model_task_manifest.json
main/repair_history.json
main/run_config.json
```

Scope/hash linkage identical through expectations/gates/reviews/selection/current bytes. Missing candidate remains explicit missing, не переносить success из прежнего rejected deck.

### 12.4. Более плотный контроль

```text
verification/dense-3x/source_fixture.pptx
verification/dense-3x/content_manifest.json
verification/dense-3x/reference_feasibility_witness.json
verification/dense-3x/authored_scene.json
verification/dense-3x/capacity_audit.json
verification/dense-3x/candidate.pptx
verification/dense-3x/render/candidate.pdf
verification/dense-3x/render/*.png
verification/dense-3x/content_diff.json
verification/dense-3x/saved_text_fit_report.json
verification/dense-3x/design_review.json
verification/dense-3x/product_acceptance.json
verification/fragmentation_invariance_report.json
verification/create_writer_smoke/**
```

Приложить исходный маленький test dataset и exact3× semantic/body count, preserved style/font hashes, actual field/paragraph/spans и measured costs. В reference witness непроверенный придуманный mock не полноценный native feasibility proof.

### 12.5. Код и проверка

```text
code/full/src/slides_cli/**
code/full/tools/layout/src/measure.js
code/full/tests/gpn/**
code/full/<actual active project skill and references>/**
code/repo_head_and_status.txt
code/git_diff.patch
code/code_manifest.json
code/commands_and_results.json
code/test_evidence.json
code/test_logs/pytest_focus.log
code/test_logs/pytest_full.log
code/test_logs/ruff.log
code/<dependency config/locks>
```

Need full current models/composition/layout/native/export/measured/runner/design/schema functions и relevant tests. Commands include exact input/code hash, flags/frame/font versions/exit. Прислать **сами** test logs: в current ZIP есть ссылки+sha, но log files отсутствуют. Tests summary не доказывает functional authoring.

Bundle builder проверяет required evidence and all referenced paths/hashes, proper CandidateKey/revision, no current trace containing old plan. Atomic source corpus before/after hashes unchanged. Existing source PPTX hash98e2357c… можно не дублировать при unchanged accessible file; ontology/font binary licenses не требуют пересылать fonts — manifests suffice.

## 13. Конец задачи

Исправление доказано, когда №6 с 81 словом и остальные source slides проходят полноценный authoring/render/design цикл;3× контроль и fragmentation invariance показывают, что вместимость зависит от смысловой композиции и реальной типографики, а не от source object fragmentation и ошибочных прямоугольников линий.

Final full deck должен быть хорошей корпоративной презентацией, а не аккуратнее разложенным текстом. Current model designs, engine faithfully renders native scene, actual images reviewed and repaired. Quality/error reports основаны на observed evidence. Нет smaller-font recommendations ради обхода engine, нет fake `exhausted0/6`, no unsupported source1:1 boxing as user content limitation.

Работу продолжать автоматически по already authorized tasks. Если настоящий terminal blocker остаётся, предъявить конкретный scope/attempt evidence/resume state; не заявлять generic content density. Не делать commit/push без отдельного указания.
