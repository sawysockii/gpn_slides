/* GPN Restyler Stage 5 measurement tool (spec §5.2–§5.4).
 *
 * Single local controlled Chromium process. Usage:
 *   node src/measure.js <request.json> <response.json>
 *
 * Protocol: request JSON in, exactly one response JSON out (atomic write).
 * Logs go to stderr only. Exit 0 = measured (items may still carry issues);
 * exit 2 = protocol error (response still written when possible);
 * exit 3 = browser/timeout/crash failure (no zero-size success).
 *
 * Security: no network (all http/https aborted), source text inserted via
 * textContent only, font bytes re-hashed and allowlisted, font-synthesis:none.
 */
import { createHash } from "node:crypto";
import fs from "node:fs";
import path from "node:path";
import { chromium } from "playwright";

const SCHEMA_VERSION = "1.0";
const ALIAS_RE = /^MEASURE_FACE_[0-9a-f]{12}$/;
const COLOR_RE = /^[0-9a-fA-F]{6}$/;

function fail(requestId, message, outPath) {
  const response = {
    schema_version: SCHEMA_VERSION,
    request_id: requestId || "",
    request_hash: "",
    engine: {},
    font_load: { faces: [], all_loaded: false, issues: [message] },
    items: [],
    issues: [message],
  };
  if (outPath) {
    fs.mkdirSync(path.dirname(outPath), { recursive: true });
    fs.writeFileSync(outPath, JSON.stringify(response));
  }
  console.error(`[measure] fatal: ${message}`);
  process.exit(3);
}

function isFiniteNum(v) {
  return typeof v === "number" && Number.isFinite(v);
}

/* ------------------------------------------------------------------ */
/* validateRequest                                                     */
/* ------------------------------------------------------------------ */

function validateRequest(raw) {
  const problems = [];
  if (!raw || typeof raw !== "object") return { ok: false, problems: ["request is not an object"], request: null };
  if (raw.schema_version !== SCHEMA_VERSION) problems.push(`unsupported schema_version: ${raw.schema_version}`);
  if (!raw.request_id || typeof raw.request_id !== "string") problems.push("missing request_id");
  const pxPerIn = raw.css_px_per_in;
  if (!isFiniteNum(pxPerIn) || pxPerIn <= 0) problems.push("bad css_px_per_in");
  if (!Array.isArray(raw.components) || raw.components.length === 0) problems.push("no components");
  if (!Array.isArray(raw.font_assets)) problems.push("font_assets must be an array");
  const aliases = new Set();
  for (const fa of raw.font_assets || []) {
    if (!ALIAS_RE.test(fa.alias || "")) problems.push(`bad font alias: ${fa.alias}`);
    else if (aliases.has(fa.alias)) problems.push(`duplicate font alias: ${fa.alias}`);
    else aliases.add(fa.alias);
    if (!fa.path || !path.isAbsolute(fa.path)) problems.push(`font path must be absolute: ${fa.alias}`);
    if (!/^[0-9a-f]{64}$/.test(fa.sha256 || "")) problems.push(`bad font sha256: ${fa.alias}`);
  }
  const ID_RE = /^[A-Za-z0-9_.:-]{1,128}$/;
  for (const c of raw.components || []) {
    if (!c.id || typeof c.id !== "string") problems.push("component without id");
    else if (!ID_RE.test(c.id)) problems.push(`component id charset rejected: ${c.id}`);
    const box = c.box_pt || {};
    for (const k of ["x", "y", "w", "h"]) {
      if (!isFiniteNum(box[k])) problems.push(`component ${c.id}: bad box_pt.${k}`);
    }
    // Zero extent is valid for lines/connectors (area is not required);
    // only negative extents are rejected. NaN/Infinity already rejected.
    if (box.w < 0 || box.h < 0) problems.push(`component ${c.id}: negative box extent`);
    for (const p of c.paragraphs || []) {
      for (const r of p.runs || []) {
        if (typeof r.text !== "string") problems.push(`component ${c.id}: run text must be a string`);
        const st = r.style || {};
        if (st.face_alias && !aliases.has(st.face_alias)) problems.push(`component ${c.id}: unknown face alias ${st.face_alias}`);
        if (st.face_alias && !ALIAS_RE.test(st.face_alias)) problems.push(`component ${c.id}: malformed face alias`);
        if (!isFiniteNum(st.size_pt) || st.size_pt <= 0 || st.size_pt > 1000) problems.push(`component ${c.id}: bad size_pt`);
        if (st.color_rgb && !COLOR_RE.test(st.color_rgb)) problems.push(`component ${c.id}: bad color_rgb`);
      }
    }
    if (c.rotation_deg && !isFiniteNum(c.rotation_deg)) problems.push(`component ${c.id}: bad rotation`);
  }
  const ids = (raw.components || []).map((c) => c.id);
  if (new Set(ids).size !== ids.length) problems.push("duplicate component ids");
  return { ok: problems.length === 0, problems, request: raw };
}

/* ------------------------------------------------------------------ */
/* buildDocument / renderTextComponent                                  */
/* ------------------------------------------------------------------ */

function ptToPx(pt, pxPerIn) {
  return (pt * pxPerIn) / 72;
}

function escAttr(s) {
  return String(s).replace(/&/g, "&amp;").replace(/"/g, "&quot;");
}

function runCss(style) {
  // Exact-face aliases only; weight/style stay normal + synthesis off.
  const parts = [
    `font-family:'${style.face_alias}'`,
    `font-size:${style.size_pt}pt`,
    "font-weight:normal",
    "font-style:normal",
    "font-synthesis:none",
  ];
  if (style.italic) parts.push("font-style:italic");
  if (style.color_rgb) parts.push(`color:#${style.color_rgb}`);
  if (style.baseline_shift === "superscript") parts.push("vertical-align:super");
  if (style.baseline_shift === "subscript") parts.push("vertical-align:sub");
  if (style.uppercase) parts.push("text-transform:uppercase");
  return parts.join(";");
}

function paragraphCss(p) {
  const parts = ["margin:0", `padding-top:${p.space_before_pt || 0}pt`, `padding-bottom:${p.space_after_pt || 0}pt`];
  if (p.line_spacing_mode === "points" && p.line_spacing_value > 0) {
    parts.push(`line-height:${p.line_spacing_value}pt`);
  } else if (p.line_spacing_mode === "multiple" && p.line_spacing_value > 0) {
    parts.push(`line-height:${p.line_spacing_value}`);
  } else {
    parts.push("line-height:normal");
  }
  const align = { left: "left", center: "center", right: "right", justify: "justify" }[p.alignment] || "left";
  parts.push(`text-align:${align}`);
  if (p.left_indent_pt) parts.push(`padding-left:${p.left_indent_pt}pt`);
  if (p.first_line_indent_pt) parts.push(`text-indent:${p.first_line_indent_pt}pt`);
  return parts.join(";");
}

function buildDocument(request) {
  const pxPerIn = request.css_px_per_in;
  const canvas = request.canvas_pt || { w: 960, h: 540 };
  const vw = Math.max(1, Math.round(ptToPx(canvas.w || 960, pxPerIn)));
  const vh = Math.max(1, Math.round(ptToPx(canvas.h || 540, pxPerIn)));
  let html = `<!DOCTYPE html><html><head><meta charset="utf-8"><style>
*{box-sizing:border-box}
html,body{margin:0;padding:0}
body{width:${vw}px;min-height:${vh}px;position:relative;font-synthesis:none}
.comp{position:absolute;overflow:visible;display:flow-root;font-synthesis:none}
.comp p{font-synthesis:none}
.comp p:empty::before{content:'\\200b'}
.probe{position:absolute;left:-10000px;top:0;visibility:hidden;font-synthesis:none}
.probe.min{width:min-content}
.probe.max{width:max-content}
.debug-outline .comp{outline:1px solid red}
.debug-outline .comp p{outline:1px dotted blue}
</style></head><body>`;
  for (const c of request.components) {
    const box = c.box_pt;
    const m = Object.assign({ l: 0, r: 0, t: 0, b: 0 }, c.margins_pt || {});
    const wrap = c.wrap_policy === "nowrap" ? "white-space:pre" : "white-space:pre-wrap;overflow-wrap:normal";
    html += `<div class="comp" data-cid="${escAttr(c.id)}" style="left:${ptToPx(box.x, pxPerIn)}px;top:${ptToPx(box.y, pxPerIn)}px;width:${ptToPx(box.w, pxPerIn)}px;min-height:${ptToPx(box.h, pxPerIn)}px;padding:${ptToPx(m.t, pxPerIn)}px ${ptToPx(m.r, pxPerIn)}px ${ptToPx(m.b, pxPerIn)}px ${ptToPx(m.l, pxPerIn)}px;${wrap}">`;
    for (const p of c.paragraphs || []) {
      html += `<p data-pid="${escAttr(p.id)}" style="${paragraphCss(p)}">`;
      for (const r of p.runs || []) {
        html += `<span data-rid="${escAttr(r.id)}" style="${runCss(r.style || {})}"></span>`;
      }
      html += "</p>";
    }
    html += "</div>";
  }
  html += "</body></html>";
  return { html, vw, vh };
}

/* ------------------------------------------------------------------ */
/* loadExactFonts                                                       */
/* ------------------------------------------------------------------ */

async function loadExactFonts(page, request) {
  const roots = (request.engine_options && request.engine_options.allowlist_roots) || [];
  const report = { faces: [], all_loaded: true, issues: [] };
  // Read + hash bytes in Node (never in page), then load via exposeBinding.
  for (const fa of request.font_assets) {
    const rec = {
      alias: fa.alias, requested_path: fa.path,
      expected_sha256: fa.sha256, actual_sha256: "",
      load_status: "error", glyph_probe: {},
      synthesis_detected: false, issues: [],
    };
    report.faces.push(rec);
    const allowed = roots.length === 0 || roots.some((r) => fa.path === r || fa.path.startsWith(r.endsWith("/") ? r : r + "/"));
    if (!allowed) {
      rec.issues.push(`path outside allowlist: ${fa.path}`);
      continue;
    }
    let bytes;
    try {
      bytes = fs.readFileSync(fa.path);
    } catch (e) {
      rec.issues.push(`cannot read: ${e.message}`);
      continue;
    }
    rec.actual_sha256 = createHash("sha256").update(bytes).digest("hex");
    if (rec.actual_sha256 !== fa.sha256) {
      rec.load_status = "hash_mismatch";
      rec.issues.push("bytes do not match declared sha256; refused");
      continue;
    }
    const loadTimeout = (request.engine_options && request.engine_options.font_timeout_ms) || 15000;
    try {
      // Base64: evaluate arguments must be JSON-serializable (no ArrayBuffer).
      const b64 = bytes.toString("base64");
      const status = await page.evaluate(async ({ alias, b64, timeoutMs, style, weight }) => {
        try {
          const bin = atob(b64);
          const arr = new Uint8Array(bin.length);
          for (let i = 0; i < bin.length; i++) arr[i] = bin.charCodeAt(i);
          const face = new FontFace(alias, arr.buffer, { style: style || "normal", weight: String(weight || 400) });
          document.fonts.add(face);
          const loaded = await Promise.race([
            face.load(),
            new Promise((_, rej) => setTimeout(() => rej(new Error("font load timeout")), timeoutMs)),
          ]);
          await document.fonts.ready;
          return { ok: true, status: loaded.status };
        } catch (e) {
          return { ok: false, error: String(e && e.message || e) };
        }
      }, { alias: fa.alias, b64, timeoutMs: loadTimeout, style: fa.style, weight: fa.weight });
      if (status.ok) {
        rec.load_status = "loaded";
      } else {
        rec.load_status = /timeout/i.test(status.error || "") ? "timeout" : "error";
        rec.issues.push(status.error || "load failed");
      }
    } catch (e) {
      rec.load_status = "error";
      rec.issues.push(`evaluate failed: ${e.message}`);
    }
  }
  report.all_loaded = report.faces.length > 0 && report.faces.every((f) => f.load_status === "loaded");
  if (!report.all_loaded) {
    const bad = report.faces.filter((f) => f.load_status !== "loaded").map((f) => `${f.alias}:${f.load_status}`);
    if (bad.length) report.issues.push(`fonts not verified: ${bad.join(", ")}`);
  }
  return report;
}

/* ------------------------------------------------------------------ */
/* collectGeometry                                                      */
/* ------------------------------------------------------------------ */

async function collectGeometry(page, request, pxPerIn) {
  return page.evaluate((pxPerIn) => {
    const pxToPt = (px) => (px * 72) / pxPerIn;
    const items = [];
    for (const comp of document.querySelectorAll(".comp")) {
      const cid = comp.getAttribute("data-cid");
      const compRect = comp.getBoundingClientRect();
      const paragraphs = [];
      for (const p of comp.querySelectorAll("p")) {
        const pid = p.getAttribute("data-pid") || "";
        const pRect = p.getBoundingClientRect();
        const runs = [];
        for (const span of p.querySelectorAll("span")) {
          const rid = span.getAttribute("data-rid") || "";
          const textNode = span.firstChild;
          const text = textNode ? textNode.nodeValue || "" : "";
          const rects = [];
          if (textNode && text.length > 0) {
            const range = document.createRange();
            range.selectNodeContents(textNode);
            for (const r of range.getClientRects()) {
              rects.push({ x: r.x, y: r.y, w: r.width, h: r.height });
            }
          }
          const cs = window.getComputedStyle(span);
          runs.push({ rid, text_len_utf16: text.length, rects_px: rects, font_family_used: cs.fontFamily });
        }
        paragraphs.push({
          pid,
          rect_px: { x: pRect.x, y: pRect.y, w: pRect.width, h: pRect.height },
          runs,
        });
      }
      items.push({
        cid,
        comp_rect_px: { x: compRect.x, y: compRect.y, w: compRect.width, h: compRect.height },
        scroll_w_px: comp.scrollWidth,
        scroll_h_px: comp.scrollHeight,
        paragraphs,
      });
    }
    return items;
  }, pxPerIn);
}

function clusterLines(rects) {
  // Overlap-based visual line clustering (no exact-y comparison).
  const sorted = rects.slice().sort((a, b) => (a.y - b.y) || (a.x - b.x));
  const lines = [];
  for (const r of sorted) {
    let placed = false;
    for (const line of lines) {
      const top = Math.max(line.top, r.y);
      const bottom = Math.min(line.bottom, r.y + r.h);
      const overlap = bottom - top;
      const minH = Math.min(line.h, r.h);
      if (minH > 0 && overlap > 0.5 * minH) {
        line.rects.push(r);
        line.top = Math.min(line.top, r.y);
        line.bottom = Math.max(line.bottom, r.y + r.h);
        line.h = line.bottom - line.top;
        placed = true;
        break;
      }
    }
    if (!placed) lines.push({ rects: [r], top: r.y, bottom: r.y + r.h, h: r.h });
  }
  lines.sort((a, b) => a.top - b.top);
  return lines;
}

async function measureIntrinsicWidths(page, request, pxPerIn) {
  // Hidden probes: preferred (max-content) and minimum (min-content).
  const out = await page.evaluate(() => {
    const res = {};
    document.body.insertAdjacentHTML("beforeend", '<div id="gpn-probe-max" class="probe max"></div><div id="gpn-probe-min" class="probe min"></div>');
    for (const comp of document.querySelectorAll(".comp")) {
      const cid = comp.getAttribute("data-cid");
      const pMax = document.getElementById("gpn-probe-max");
      const pMin = document.getElementById("gpn-probe-min");
      pMax.innerHTML = "";
      pMin.innerHTML = "";
      const cloneMax = comp.cloneNode(true);
      cloneMax.removeAttribute("class");
      cloneMax.style.cssText = "display:inline-block;width:max-content;white-space:pre;font-synthesis:none;";
      const cloneMin = comp.cloneNode(true);
      cloneMin.removeAttribute("class");
      cloneMin.style.cssText = "display:inline-block;width:min-content;white-space:pre-wrap;overflow-wrap:normal;font-synthesis:none;";
      pMax.appendChild(cloneMax);
      pMin.appendChild(cloneMin);
      const rMax = cloneMax.getBoundingClientRect();
      const rMin = cloneMin.getBoundingClientRect();
      res[cid] = { pref_px: rMax.width, min_px: rMin.width };
      pMax.innerHTML = "";
      pMin.innerHTML = "";
    }
    document.getElementById("gpn-probe-max").remove();
    document.getElementById("gpn-probe-min").remove();
    return res;
  });
  const conv = {};
  for (const [cid, v] of Object.entries(out)) {
    conv[cid] = {
      pref_pt: (v.pref_px * 72) / pxPerIn,
      min_pt: (v.min_px * 72) / pxPerIn,
    };
  }
  return conv;
}

function detectOverflow(item, boxPt, marginsPt, tolPt) {
  /* Coordinate contract (§5.1), versioned by TOOL_VERSION in measurement.py.
   *
   * `fragments[].logical_rect_pt` are relative to the component ORIGIN
   * (outer box), so they already include the top/left insets. The fit test
   * therefore compares them against the inner content area expressed in
   * that SAME origin:
   *
   *     left   = margins.l
   *     top    = margins.t
   *     right  = box.w - margins.r
   *     bottom = box.h - margins.b
   *
   * Comparing only against the outer border (the pre-0.3.0 behaviour) let a
   * fragment intrude into the right/bottom insets without ever being
   * reported. Subtracting top/left a second time here would double-charge
   * them instead. Each inset is therefore applied exactly once.
   */
  const l = marginsPt.l || 0;
  const r = marginsPt.r || 0;
  const t = marginsPt.t || 0;
  const b = marginsPt.b || 0;
  const inner = {
    x: l,
    y: t,
    w: boxPt.w - l - r,
    h: boxPt.h - t - b,
  };
  const limits = {
    left: l,
    top: t,
    right: boxPt.w - r,
    bottom: boxPt.h - b,
  };
  let maxRight = 0;
  let maxBottom = 0;
  let minLeft = Infinity;
  let minTop = Infinity;
  for (const f of item.fragments) {
    const q = f.logical_rect_pt;
    maxRight = Math.max(maxRight, q.x + q.w);
    maxBottom = Math.max(maxBottom, q.y + q.h);
    minLeft = Math.min(minLeft, q.x);
    minTop = Math.min(minTop, q.y);
  }
  if (!item.fragments.length) {
    minLeft = 0;
    minTop = 0;
  }
  // Only the FAR side decides fit. The near side (left/top) is guaranteed
  // by the layout origin; a glyph's range rect can legitimately bleed a
  // fraction of a point past its line box (ascender above line-height) or
  // start left of the content origin for a hanging/negative indent, and
  // neither is a fit failure. Intrusions are reported as diagnostics.
  const overflowX = maxRight > limits.right + tolPt;
  const overflowY = maxBottom > limits.bottom + tolPt;
  // Content extent measured from the INNER origin (insets excluded).
  const contentBottomInner = maxBottom - t;
  const contentRightInner = maxRight - l;
  return {
    overflowX,
    overflowY,
    limits,
    inner,
    // Outer box height that fits this content EXACTLY: both vertical insets
    // charged once, on top of the real paragraph/line extent.
    requiredOuterHeightPt: maxBottom + b,
    requiredOuterWidthPt: maxRight + r,
    contentBottomInner,
    contentRightInner,
    excessPt: {
      x: Math.max(0, maxRight - limits.right),
      y: Math.max(0, maxBottom - limits.bottom),
    },
    intrusionPt: {
      x: Math.max(0, limits.left - minLeft),
      y: Math.max(0, limits.top - minTop),
    },
    excessInnerPt: {
      x: Math.max(0, contentRightInner - inner.w),
      y: Math.max(0, contentBottomInner - inner.h),
    },
  };
}

/* ------------------------------------------------------------------ */
/* measureDocument                                                      */
/* ------------------------------------------------------------------ */

async function measureDocument(page, request, viewport) {
  const pxPerIn = request.css_px_per_in;
  const pxToPt = (px) => (px * 72) / pxPerIn;
  const geo = await collectGeometry(page, request, pxPerIn);
  const intrinsic = await measureIntrinsicWidths(page, request, pxPerIn);
  const tolPt = 0.5;
  const items = [];
  const loadedAliases = new Set(
    (request._fontReport.faces || []).filter((f) => f.load_status === "loaded").map((f) => f.alias)
  );
  for (const comp of request.components) {
    const g = geo.find((x) => x.cid === comp.id);
    const issues = [];
    const measured = {
      component_id: comp.id,
      source_ref: comp.payload_ref || "",
      available_inner_box_pt: { x: 0, y: 0, w: 0, h: 0 },
      inner_box_in_frame_pt: { x: 0, y: 0, w: 0, h: 0 },
      logical_bounds_pt: { x: 0, y: 0, w: 0, h: 0 },
      content_bounds_inner_pt: { x: 0, y: 0, w: 0, h: 0 },
      logical_line_boxes_inner_pt: [],
      occupied_height_pt: 0,
      required_outer_height_pt: 0,
      intrinsic_min_width_pt: 0,
      intrinsic_preferred_width_pt: 0,
      overflow_x: false,
      overflow_y: false,
      overflow_inner_x: false,
      overflow_inner_y: false,
      excess_pt: { x: 0, y: 0 },
      excess_inner_pt: { x: 0, y: 0 },
      content_absent: false,
      fragments: [],
      line_count: 0,
      paragraph_count: (comp.paragraphs || []).length,
      text_origin_x_pt: 0,
      applied_transforms: [],
      font_evidence: [],
      confidence: request.profile_kind === "test_only" ? "test_only" : "exact_browser",
      issues: [],
    };
    if (!g) {
      issues.push(`no geometry for component ${comp.id}`);
      measured.issues = issues;
      items.push(measured);
      continue;
    }
    const margins = Object.assign({ l: 0, r: 0, t: 0, b: 0 }, comp.margins_pt || {});
    const boxPt = comp.box_pt;
    measured.available_inner_box_pt = {
      x: boxPt.x + margins.l,
      y: boxPt.y + margins.t,
      w: Math.max(0, boxPt.w - margins.l - margins.r),
      h: Math.max(0, boxPt.h - margins.t - margins.b),
    };
    // The reported box is clamped to a real extent; the RAW (possibly
    // negative) value is kept inside detectOverflow and drives the
    // inner_box_non_positive finding. Reporting a negative width would
    // violate the response schema and hide the very defect it describes.
    measured.inner_box_in_frame_pt = {
      x: Math.max(0, margins.l),
      y: Math.max(0, margins.t),
      w: Math.max(0, boxPt.w - margins.l - margins.r),
      h: Math.max(0, boxPt.h - margins.t - margins.b),
    };
    // Component origin in px (viewport space) for relative coords.
    const ox = g.comp_rect_px.x;
    const oy = g.comp_rect_px.y;
    let firstTextX = null;
    // `lineCount` counts LOGICAL lines: two runs of one paragraph sharing a
    // visual line contribute one line, not two. Per-paragraph clustering
    // below is the single source of truth; run lines are mapped onto it.
    let lineCount = 0;
    for (const p of g.paragraphs) {
      const allRects = [];
      for (const run of p.runs) {
        for (const r of run.rects_px) {
          allRects.push({ ...r, rid: run.rid, pid: p.pid, text_len: run.text_len_utf16 });
        }
      }
      const paraBase = lineCount;
      if (allRects.length === 0) {
        // Empty / break-only paragraph: element rect still occupies style height.
        const er = p.rect_px;
        if (er.h > 0) {
          measured.fragments.push({
            paragraph_id: p.pid,
            run_id: "",
            utf16_start: 0,
            utf16_end: 0,
            line_index: paraBase,
            logical_rect_pt: {
              x: pxToPt(er.x - ox), y: pxToPt(er.y - oy),
              w: pxToPt(er.w), h: pxToPt(er.h),
            },
            ink_rect_pt: null,
            baseline_pt: null,
          });
          lineCount += 1;
        }
        continue;
      }
      const paraLines = clusterLines(allRects);
      const lineIndexFor = (top, bottom) => {
        let best = 0;
        let bestOv = -1;
        for (let i = 0; i < paraLines.length; i++) {
          const ov = Math.min(paraLines[i].bottom, bottom) - Math.max(paraLines[i].top, top);
          if (ov > bestOv) { bestOv = ov; best = i; }
        }
        return paraBase + best;
      };
      // Per-run line boxes: cluster this run's rects, then assign utf16
      // offsets progressively (rect order == text order for a single node).
      const byRun = {};
      for (const r of allRects) {
        (byRun[r.rid] = byRun[r.rid] || []).push(r);
      }
      for (const run of p.runs) {
        const rects = byRun[run.rid] || [];
        if (!rects.length) continue;
        const runLines = clusterLines(rects);
        const total = run.text_len_utf16;
        // Distribute utf16 offsets across this run's line rects by width share.
        const widths = runLines.map((ln) =>
          Math.max(...ln.rects.map((r) => r.w), 0.01)
        );
        const wSum = widths.reduce((a, b) => a + b, 0);
        let cursor = 0;
        runLines.forEach((ln, i) => {
          const share = i === runLines.length - 1
            ? total - cursor
            : Math.round((total * widths[i]) / wSum);
          const start = cursor;
          cursor += share;
          const box = {
            x: Math.min(...ln.rects.map((r) => r.x)),
            y: ln.top,
            w: Math.max(...ln.rects.map((r) => r.x + r.w)) - Math.min(...ln.rects.map((r) => r.x)),
            h: ln.h,
          };
          if (firstTextX === null) firstTextX = box.x;
          measured.fragments.push({
            paragraph_id: p.pid,
            run_id: run.rid,
            utf16_start: start,
            utf16_end: Math.min(total, cursor),
            line_index: lineIndexFor(ln.top, ln.top + ln.h),
            logical_rect_pt: {
              x: pxToPt(box.x - ox), y: pxToPt(box.y - oy),
              w: pxToPt(box.w), h: pxToPt(box.h),
            },
            ink_rect_pt: null,
            baseline_pt: null,
          });
        });
      }
      lineCount = paraBase + paraLines.length;
    }
    measured.line_count = lineCount;
    // Logical line boxes in INNER-origin coordinates (one entry per line,
    // runs sharing a line merged).
    const lineAcc = new Map();
    for (const f of measured.fragments) {
      const key = f.paragraph_id + "#" + f.line_index;
      const q = f.logical_rect_pt;
      let acc = lineAcc.get(key);
      if (!acc) {
        acc = {
          line_index: f.line_index,
          paragraph_id: f.paragraph_id,
          run_ids: [],
          x: q.x, y: q.y, r: q.x + q.w, b: q.y + q.h,
        };
        lineAcc.set(key, acc);
      }
      if (f.run_id && !acc.run_ids.includes(f.run_id)) acc.run_ids.push(f.run_id);
      acc.x = Math.min(acc.x, q.x);
      acc.y = Math.min(acc.y, q.y);
      acc.r = Math.max(acc.r, q.x + q.w);
      acc.b = Math.max(acc.b, q.y + q.h);
    }
    const lineBoxes = [...lineAcc.values()].sort((a, b) =>
      (a.line_index - b.line_index) || (a.x - b.x));
    const innerOriginX = margins.l || 0;
    const innerOriginY = margins.t || 0;
    measured.logical_line_boxes_inner_pt = lineBoxes.map((ln) => ({
      line_index: ln.line_index,
      paragraph_id: ln.paragraph_id,
      run_ids: ln.run_ids,
      rect_inner_pt: {
        x: ln.x - innerOriginX,
        y: ln.y - innerOriginY,
        w: ln.r - ln.x,
        h: ln.b - ln.y,
      },
    }));
    if (measured.fragments.length) {
      const xs = measured.fragments.map((f) => f.logical_rect_pt.x);
      const ys = measured.fragments.map((f) => f.logical_rect_pt.y);
      const rs = measured.fragments.map((f) => f.logical_rect_pt.x + f.logical_rect_pt.w);
      const bs = measured.fragments.map((f) => f.logical_rect_pt.y + f.logical_rect_pt.h);
      measured.logical_bounds_pt = {
        x: Math.min(...xs), y: Math.min(...ys),
        w: Math.max(...rs) - Math.min(...xs),
        h: Math.max(...bs) - Math.min(...ys),
      };
      // LEGACY field, meaning preserved verbatim across the 0.3.0 contract
      // bump: max content bottom relative to the OUTER origin (top inset
      // included, bottom inset NOT). New callers must use
      // `required_outer_height_pt`.
      measured.occupied_height_pt = Math.max(...bs);
      measured.content_bounds_inner_pt = {
        x: Math.min(...xs) - innerOriginX,
        y: Math.min(...ys) - innerOriginY,
        w: Math.max(0, Math.max(...rs) - Math.min(...xs)),
        h: Math.max(0, Math.max(...bs) - Math.min(...ys)),
      };
    }
    measured.text_origin_x_pt = firstTextX === null ? 0 : pxToPt(firstTextX - ox);
    const intr = intrinsic[comp.id] || { pref_pt: 0, min_pt: 0 };
    measured.intrinsic_min_width_pt = intr.min_pt;
    measured.intrinsic_preferred_width_pt = intr.pref_pt;
    const det = detectOverflow(measured, boxPt, margins, tolPt);
    measured.overflow_x = det.overflowX;
    measured.overflow_y = det.overflowY;
    measured.overflow_inner_x = det.excessInnerPt.x > tolPt;
    measured.overflow_inner_y = det.excessInnerPt.y > tolPt;
    measured.excess_pt = det.excessPt;
    measured.excess_inner_pt = det.excessInnerPt;
    measured.required_outer_height_pt = det.requiredOuterHeightPt;
    const contentPresent = (comp.paragraphs || []).some((p) =>
      (p.runs || []).some((r) => typeof r.text === "string" && r.text.length > 0));
    measured.content_absent = !contentPresent;
    if (!contentPresent) {
      // Nothing to place: a zero-extent connector/line with no text is a
      // legitimate empty geometry, not a fabricated fit and not an error.
      measured.required_outer_height_pt = 0;
    } else if (det.inner.w <= 0 || det.inner.h <= 0) {
      // Content inside a non-positive inner area is invalid geometry; it is
      // never reported as a clean fit.
      issues.push(
        `inner_box_non_positive: ${det.inner.w.toFixed(2)}x${det.inner.h.toFixed(2)}pt ` +
        `inside ${boxPt.w.toFixed(2)}x${boxPt.h.toFixed(2)}pt outer box`);
      measured.confidence = "unsupported";
      measured.overflow_x = true;
      measured.overflow_y = true;
    } else {
      if (det.overflowX) issues.push(`overflow_x +${det.excessPt.x.toFixed(2)}pt`);
      if (det.overflowY) issues.push(`overflow_y +${det.excessPt.y.toFixed(2)}pt`);
    }
    for (const p of comp.paragraphs || []) {
      for (const r of p.runs || []) {
        const alias = (r.style || {}).face_alias || "";
        if (alias && !loadedAliases.has(alias)) {
          issues.push(`face not verified: ${alias}`);
        }
        if ((r.style || {}).uppercase && !measured.applied_transforms.includes("uppercase")) {
          measured.applied_transforms.push("uppercase");
        }
      }
    }
    measured.font_evidence = [...loadedAliases].filter((a) =>
      (comp.paragraphs || []).some((p) => (p.runs || []).some((r) => (r.style || {}).face_alias === a)));
    measured.issues = issues;
    items.push(measured);
  }
  return items;
}


/* ------------------------------------------------------------------ */
/* closeMeasurementSession / main                                       */
/* ------------------------------------------------------------------ */

async function closeMeasurementSession(browser, context) {
  try {
    if (context) await context.close();
  } catch (e) {
    console.error(`[measure] context close: ${e.message}`);
  }
  try {
    if (browser) await browser.close();
  } catch (e) {
    console.error(`[measure] browser close: ${e.message}`);
  }
}

async function main() {
  const [reqPath, outPath] = process.argv.slice(2);
  const started = Date.now();
  if (!reqPath || !outPath) {
    console.error("usage: node src/measure.js <request.json> <response.json>");
    process.exit(2);
  }
  let raw;
  try {
    raw = JSON.parse(fs.readFileSync(reqPath, "utf-8"));
  } catch (e) {
    fail("", `cannot read request: ${e.message}`, outPath);
    return;
  }
  const { ok, problems, request } = validateRequest(raw);
  if (!ok) {
    const response = {
      schema_version: SCHEMA_VERSION,
      request_id: (raw && raw.request_id) || "",
      request_hash: "",
      engine: {},
      font_load: { faces: [], all_loaded: false, issues: problems },
      items: [],
      issues: problems,
    };
    fs.mkdirSync(path.dirname(outPath), { recursive: true });
    fs.writeFileSync(outPath, JSON.stringify(response));
    console.error(`[measure] protocol error: ${problems.join("; ")}`);
    process.exit(2);
  }
  const timeoutMs = (request.engine_options && request.engine_options.timeout_ms) || 120000;
  let browser = null;
  let context = null;
  try {
    browser = await chromium.launch();
    const { html, vw, vh } = buildDocument(request);
    context = await browser.newContext({
      viewport: { width: vw, height: vh },
      deviceScaleFactor: 1,
      offline: true,
      locale: "en-US",
    });
    await context.route(/^(https?|wss?):\/\//, (route) => route.abort());
    const page = await context.newPage();
    const debugDir = request.debug_output_dir || "";
    if (debugDir) fs.mkdirSync(debugDir, { recursive: true });
    // Shells carry no text: run texts are filled via textContent only
    // (no innerHTML anywhere on the input path).
    await page.setContent(html, { waitUntil: "load" });
    await page.evaluate((components) => {
      for (const c of components) {
        for (const p of c.paragraphs || []) {
          for (const r of p.runs || []) {
            const comp = [...document.querySelectorAll(".comp")].find(
              (el) => el.getAttribute("data-cid") === c.id);
            const para = comp && [...comp.querySelectorAll("p")].find(
              (el) => el.getAttribute("data-pid") === p.id);
            const el = para && [...para.querySelectorAll("span")].find(
              (el) => el.getAttribute("data-rid") === r.id);
            if (!el) throw new Error(`MEASUREMENT_RUN_NOT_FOUND:${r.id}`);
            el.textContent = r.text;
          }
        }
      }
    }, request.components);
    if (debugDir) {
      const dom = await page.evaluate(() => document.documentElement.outerHTML);
      fs.writeFileSync(path.join(debugDir, `${request.request_id}.html`), dom);
    }
    const fontReport = await loadExactFonts(page, request);
    request._fontReport = fontReport;
    // Re-measure after fonts are ready (layout may shift).
    const items = await measureDocument(page, request, { vw, vh });
    if (debugDir) {
      await page.evaluate(() => document.body.classList.add("debug-outline"));
      await page.screenshot({ path: path.join(debugDir, `${request.request_id}.png`) });
    }
    const response = {
      schema_version: SCHEMA_VERSION,
      request_id: request.request_id,
      request_hash: (request.input_hashes && request.input_hashes.canonical_request) || "",
      engine: {
        tool: "layout-measure 0.1.0",
        playwright: "1.64.0",
        chromium: await browser.version(),
        elapsed_ms: String(Date.now() - started),
      },
      font_load: fontReport,
      items,
      issues: fontReport.issues.slice(),
    };
    fs.mkdirSync(path.dirname(outPath), { recursive: true });
    const tmp = `${outPath}.tmp`;
    fs.writeFileSync(tmp, JSON.stringify(response));
    fs.renameSync(tmp, outPath);
    await closeMeasurementSession(browser, context);
    browser = null;
  } catch (e) {
    console.error(`[measure] failed: ${e.message}`);
    await closeMeasurementSession(browser, context);
    process.exit(3);
  }
}

main();