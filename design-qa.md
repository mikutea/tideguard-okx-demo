# 墨衡 MOHENG v0.4 Design QA

## Evidence

- Source visual truth: `docs/moheng-concept.png`
- Browser-rendered desktop implementation: `docs/moheng-desktop.png`
- Browser-rendered mobile implementation: `docs/moheng-mobile.png`
- Local route/state: Demo, no credential, no champion, no model-owned position; all execution controls remain locked.
- Source pixels: 1487 × 1058.
- Desktop implementation: 1265 × 712 pixels from a 1280 × 720 CSS viewport at deviceScaleFactor 1.5; the 15 px difference is the visible scrollbar gutter.
- Mobile implementation: 585 × 1266 pixels from a 390 × 844 CSS viewport at deviceScaleFactor 1.5.
- Density normalization: mobile evidence is a 1.5x capture of the exact 390 × 844 CSS viewport. For the full-view comparison, source and desktop captures were placed in one comparison canvas and scaled to the same 1400 px width while preserving their original aspect ratios. The concept is a denser populated-research state; the implementation is the truthful empty/pre-training state, so metric values were not compared pixel-for-pixel.

## Full-view comparison

- Information architecture: the implementation preserves the concept's ink-black, jade, bronze, blue and vermilion control language, persistent left navigation, top safety/status strip, emergency stop and dense quantitative panels. It intentionally distributes the concept's single-screen research wall across six task pages so new users can follow runtime → data → training → models → execution → audit without hiding technical evidence.
- Typography: Chinese display text uses Microsoft YaHei UI fallbacks with clear 24–32 px page hierarchy; body and table text are at least 13 px. Hashes and identifiers use the mono stack. No clipped or truncated primary labels were observed.
- Spacing/layout: desktop content uses the full available width with stable card rhythm and explicit alignment. At 390 px, every page has zero horizontal overflow and visible button height is at least 44 px.
- Colors/tokens: operational jade, research bronze, market blue and high-risk vermilion remain semantically separate. Demo and Live states do not reuse the same risk treatment.
- Image/asset fidelity: the product uses the generated PNG/ICO 墨衡 brand asset and Lucide icons; there are no emoji, handcrafted SVG logos, text-symbol substitutes or placeholder brand boxes.
- Copy/content: all empty states remain truthful. More history is explicitly not described as guaranteed profit; no-champion and no-credential states explain NOW / WHY / NEXT.

## Focused region comparison

- Header/navigation: checked logo sharpness, selected navigation, environment badge, evidence toggle and emergency stop against the concept. The implementation keeps the same safety hierarchy with larger, more readable controls.
- Runtime chart: checked legend, chart labels, empty state and model-owned position separation. The empty state no longer visually resembles a full-screen loading spinner.
- Data page: checked coverage bar, lineage diagram, integrity panel, walk-forward time protocol and ledger. Zero-row storage is labeled “等待首次全量回填”, never “已确认”.
- Mobile: checked the 390 × 844 viewport, fixed six-item navigation, NOW / WHY / NEXT stacking, chart/card wrapping and 44 px controls.

## Findings

No actionable P0, P1 or P2 findings remain.

Residual P3 differences are intentional:

- The concept is a single ultra-dense populated dashboard, while the implementation uses task pages and a truthful empty state. This improves learnability without removing evidence.
- The implementation logo is the production raster/ICO mark rather than the thinner concept sketch.

## Comparison history

### Pass 1 — blocked

- P1: `.performance-canvas svg` applied a 300 px minimum height to the small empty-state icon, producing a giant dashed ring over the chart.
  - Fix: narrowed the selector to `.performance-canvas > svg`.
  - Post-fix evidence: `docs/moheng-desktop.png` shows a compact inline empty-state icon and unobstructed chart canvas.
- P2: an empty warehouse with zero gaps/conflicts was shown as “公共数据已确认 / 已确认”.
  - Fix: confirmation now requires rows > 0, completed backfill and a snapshot hash; otherwise it shows “等待首次全量回填”.
  - Post-fix evidence: browser DOM and data page show “等待首次全量回填”, with integrity “待全量遥测”.

### Pass 2 — passed

- Desktop: six primary routes opened successfully; no horizontal overflow; console warnings/errors: 0.
- Mobile: six primary routes opened successfully at 390 × 844; horizontal overflow: 0; minimum visible button height: 44 px; console warnings/errors: 0.
- Primary interactions tested: desktop and mobile navigation, explanation-level controls visibility, empty states, Demo execution lock, persistent emergency control and responsive bottom navigation.

## Follow-up polish

- After the first historical backfill and training run, recapture populated data, training and model pages for release notes. This is not a current fidelity blocker because the software correctly exposes the empty/pre-training state.

final result: passed

## V3 historical replay addendum — 2026-08-22

### Accepted baseline and latest evidence

- Accepted training-page baseline: `.research-data/qa/training-baseline.png`.
- Latest desktop replay console: `.research-data/qa/training-replay-desktop.png`.
- Latest mobile replay console: `.research-data/qa/training-replay-mobile.png` at the exact 390 × 844 CSS viewport.
- The V3 work intentionally keeps the accepted 墨衡 design language instead of introducing a second concept style: ink-black workspace, jade operation state, bronze research markers, blue calibration state, vermilion losses, square technical panels and the existing Microsoft YaHei / mono hierarchy.

### Fidelity ledger

| Comparison point | Baseline | V3 implementation | Result |
| --- | --- | --- | --- |
| Navigation and safety hierarchy | Persistent left rail, Demo badge and emergency stop | Unchanged; replay appears inside the existing Training route | Match |
| Typography | 24–32 px page heading, 13 px dense evidence text, mono identifiers | Same hierarchy; replay ID, English kicker and numeric evidence use mono/tabular treatment | Match |
| Panel geometry | Tight 8 px radii, 1 px technical borders, dense grid rhythm | Replay title, KPI strip, chart/inspector split and generation rail follow the same geometry | Match |
| Semantic color | Jade operational, bronze research, blue model evidence, vermilion loss | Replay uses jade playback, bronze cursor, blue calibration and vermilion negative return | Match |
| Data visualization | Graphs and matrices disclose real state without decorative placeholder values | 841 real daily checkpoints, full muted path, played jade path and current cursor; no synthetic values | Improved |
| Safety copy | Demo/Live boundary and no-profit guarantee remain visible | Fixed `0 Shadow 天 · 0 下单能力`; even a passed development gate remains non-promotable | Match |
| Responsive behavior | Single-column mobile cards and fixed six-route bottom navigation | Replay KPI pairs, stacked causal stages, full-width controls and horizontal generation rail | Match |

### Above-the-fold copy diff

- Before: the first major panel was “任务阶段与证据产物”, which described the latest training run but did not expose historical model evolution.
- After: “历史高速回放训练场” appears first, with the explicit contract “365 天滚动拟合，30 天重新训练；播放器只读取已冻结证据，不触发训练、私有 API 或订单。” The original task-stage panel remains immediately below it.

### Interaction and responsive proof

- Play at 8× advanced the readout from `05/03 01:20` to `05/08 01:20`, then exposed a visible Pause control.
- Selecting generation 28 moved the slider to checkpoint 810 and changed the inspector to “第 28 代”.
- Desktop and 390 × 844 mobile both reported `scrollWidth === clientWidth`; no horizontal overflow.
- Replay controls have a minimum visible height of 44 px after the focused QA pass.
- Browser console warnings/errors: 0 on desktop and mobile.

Final V3 replay UI result: passed with no actionable P0, P1 or P2 finding.
