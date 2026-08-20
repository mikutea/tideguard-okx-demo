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
