/* Fathom 图标库 —— 内联 SVG 线条图标（lucide 同款风格）
 * 规范（DESIGN.md 视觉层）：24 viewBox、stroke=currentColor、fill=none、
 * stroke-width 2、round linecap/linejoin；禁止 emoji（DEC-010）。
 * 所有用户可见 SVG 图标只在本文件集中维护（ES module）。
 *
 * 双形态体系（DEC-023 / ISS-045 / ISS-086）：
 *   - brandRing  = 线条「深度环」，用于界面小尺寸（菜单栏 tray 22pt、
 *                  加载环、详情标题装饰、favicon）。四处几何由
 *                  scripts/ci_brand_geometry.sh 门禁保证同步。
 *   - brandBasin = 实色「层叠深潭」，用于主窗口品牌站位（侧栏顶部 +
 *                  页头）。几何反向自 apps/desktop/src-tauri/icons/icon.png
 *                  （1024×1024 位图）；ISS-088 起各层为自该位图 PIL 采样的
 *                  固定 fill 真彩色板（象牙白底 + 蓝青阶地），不依赖 CSS
 *                  currentColor/opacity 染色。
 * 任何新品牌位复用：先在「主窗口/页头/关于区」用 brandBasin；其余场景
 * 才用 brandRing。混用由 ISS-087 之后的 review 阶段逐项核查。
 */

export const ICON_PATHS = {
  /* 深度环：品牌标记（R3 视觉签名，ISS-072）——开放圆环（海沟蓝，
   * currentColor）+ 中心探针与跨刻度缺口的短刻度（矿物青，CSS 类染色）。
   * Fathom = 测深单位；替代 R2 时期的船锚（R3 合同禁写实海洋元素）。
   * 仅用于界面小尺寸场景（菜单栏 tray 22pt、侧栏字标外的加载环与
   * favicon），由 scripts/ci_brand_geometry.sh 门禁保证四处几何同步；
   * 主窗口品牌站位请用 brandBasin（应用图标本身）。 */
  brandRing:
    '<path class="dr-ring" d="M20 9.1A8.5 8.5 0 1 1 14.9 4"/>' +
    '<line class="dr-probe" x1="12" y1="7.5" x2="12" y2="16.5"/>' +
    '<line class="dr-tick" x1="17.2" y1="6.8" x2="19.3" y2="4.7"/>',
  /* 层叠深潭：DEC-023 双形态体系的「主形态」（ISS-045 / ISS-086 / ISS-088）。
   * 反向自 apps/desktop/src-tauri/icons/icon.png（1024×1024）的几何——
   * 实色 4 层嵌套等深卵形（顶部偏窄、底部略宽的水滴/坑口）+ 顶部象牙白
   * 测深刻痕。ISS-088 真彩化：各层改为自 icon.png PIL 采样的固定 fill
   * （象牙白底板 + 浅青白/青蓝/深蓝/深潭墨四层阶地），不再依赖 CSS
   * currentColor/opacity 叠加——旧方案在 24px 下呈模糊单色色团，用户
   * 实机反馈「应用图标没显示出来」。色板采样记录（1024 图坐标，环形带
   * 中心多方向均值）：
   *   bv-base #FCFAF4 象牙白底板（(154,489)/(872,489)/(514,120)）
   *   bv-l1   #C6DCE4 浅青白阶地（(514,763)/(715,690)/(279,489)/(345,657)）
   *   bv-l2   #286B88 青蓝阶地  （(514,703)/(680,655)/(407,595)/(399,489) 等 7 点）
   *   bv-l3   #062743 深蓝阶地  （(514,603)/(613,588)/(427,489) 等 7 点）
   *   bv-core #00142A 深潭墨核心（(538,513)/(514,504)/(530,505)）
   *   bv-notch #FCFAF4 象牙白测深刻痕（与底板同色，(514,180)）
   * 绘制顺序：底板→四层椭圆→notch 最后叠加（icon.png 的刻痕坐在盆
   * 顶缘之上；旧顺序 notch 垫底会被底层椭圆盖掉大半）。
   * 与 brandRing 互斥：brandRing = 线条环（小尺寸）；brandBasin =
   * 层叠面（主窗口）。两套几何由 icons.js 集中维护，便于 ISS-087 复用
   * 与 DEC-023 双形态合同审计。 */
  brandBasin:
    /* bv-base 底板 ry=10.5（ISS-095 几何修正）：原 ry=11.3 时底缘
     * cy+ry=24.7 超 24 viewBox 底边 0.7 单位被裁平；收至 23.9（留 0.1
     * 抗锯齿余量）后底缘完整呈现，cy 与内部四层保持同心，视觉几乎不变。 */
    '<ellipse class="bv-base" fill="#FCFAF4" cx="12" cy="13.4" rx="11" ry="10.5"/>' +
    '<ellipse class="bv-l1" fill="#C6DCE4" cx="12" cy="13.4" rx="9.2" ry="9.5"/>' +
    '<ellipse class="bv-l2" fill="#286B88" cx="12" cy="13.4" rx="6.6" ry="7"/>' +
    '<ellipse class="bv-l3" fill="#062743" cx="12" cy="13.4" rx="4" ry="4.4"/>' +
    '<ellipse class="bv-core" fill="#00142A" cx="12" cy="13.4" rx="2.1" ry="2.5"/>' +
    '<path class="bv-notch" fill="#FCFAF4" d="M10.4 1.6 L12 4.4 L13.6 1.6 Z"/>',
  /* 总览：仪表盘 */
  gauge:
    '<path d="m12 14 4-4"/><path d="M3.34 19a10 10 0 1 1 17.32 0"/>',
  /* 变化：活动折线 */
  activity:
    '<polyline points="22 12 18 12 15 21 9 3 6 12 2 12"/>',
  /* 分布：饼图 */
  pie:
    '<path d="M21.21 15.89A10 10 0 1 1 8 2.83"/><path d="M22 12A10 10 0 0 0 12 2v10z"/>',
  /* 大文件 / 报告：文档 */
  fileText:
    '<path d="M15 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V7Z"/><path d="M14 2v4a2 2 0 0 0 2 2h4"/><path d="M10 9H8"/><path d="M16 13H8"/><path d="M16 17H8"/>',
  /* 设置：齿轮 */
  settings:
    '<path d="M12.22 2h-.44a2 2 0 0 0-2 2v.18a2 2 0 0 1-1 1.73l-.43.25a2 2 0 0 1-2 0l-.15-.08a2 2 0 0 0-2.73.73l-.22.38a2 2 0 0 0 .73 2.73l.15.1a2 2 0 0 1 1 1.72v.51a2 2 0 0 1-1 1.74l-.15.09a2 2 0 0 0-.73 2.73l.22.38a2 2 0 0 0 2.73.73l.15-.08a2 2 0 0 1 2 0l.43.25a2 2 0 0 1 1 1.73V20a2 2 0 0 0 2 2h.44a2 2 0 0 0 2-2v-.18a2 2 0 0 1 1-1.73l.43-.25a2 2 0 0 1 2 0l.15.08a2 2 0 0 0 2.73-.73l.22-.39a2 2 0 0 0-.73-2.73l-.15-.08a2 2 0 0 1-1-1.74v-.5a2 2 0 0 1 1-1.74l.15-.09a2 2 0 0 0 .73-2.73l-.22-.38a2 2 0 0 0-2.73-.73l-.15.08a2 2 0 0 1-2 0l-.43-.25a2 2 0 0 1-1-1.73V4a2 2 0 0 0-2-2z"/><circle cx="12" cy="12" r="3"/>',
  /* 在 Finder 中打开 */
  folderOpen:
    '<path d="m6 14 1.5-2.9A2 2 0 0 1 9.24 10H20a2 2 0 0 1 1.94 2.5l-1.54 6a2 2 0 0 1-1.95 1.5H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h3.9a2 2 0 0 1 1.69.9l.81 1.2a2 2 0 0 0 1.67.9H18a2 2 0 0 1 2 2v2"/>',
  /* 增长 / 缩减 / 新增 / 消失 */
  arrowUpRight: '<path d="M7 7h10v10"/><path d="M7 17 17 7"/>',
  arrowDownRight: '<path d="m7 7 10 10"/><path d="M17 7v10H7"/>',
  plus: '<path d="M5 12h14"/><path d="M12 5v14"/>',
  trash:
    '<path d="M3 6h18"/><path d="M19 6v14c0 1-1 2-2 2H7c-1 0-2-1-2-2V6"/><path d="M8 6V4c0-1 1-2 2-2h4c1 0 2 1 2 2v2"/>',
  /* 权限受限提示 */
  alert:
    '<path d="m21.73 18-8-14a2 2 0 0 0-3.48 0l-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.73-3Z"/><path d="M12 9v4"/><path d="M12 17h.01"/>',
  /* 授权状态盾牌：用于覆盖说明与权限深链 */
  shield:
    '<path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/>',
  /* 扫描期间消失：路径岔口（外部链接）
   * 表达"跳到另一处"，与 settingsLink 一致对外系统设置跳转。 */
  externalLink:
    '<path d="M15 3h6v6"/><path d="M10 14 21 3"/><path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/>',
  /* 排除掩码：圆角斜线，呼应名字匹配跳过的语义 */
  filter:
    '<path d="M22 3H2l8 9.46V19l4 2v-8.54L22 3z"/>',
  /* 复制路径（DESIGN：长路径可一键复制） */
  copy:
    '<rect width="14" height="14" x="8" y="8" rx="2" ry="2"/><path d="M4 16c-1.1 0-2-.9-2-2V4c0-1.1.9-2 2-2h10c1.1 0 2 .9 2 2"/>',
  /* 关闭详情侧栏 */
  x:
    '<path d="M18 6 6 18"/><path d="m6 6 12 12"/>',
  /* 折叠区展开指示（ISS-085 设置页「高级与诊断」等 details/summary）：
   * 右向 chevron，details[open] 时由 CSS 旋转 90°（200ms） */
  chevron:
    '<path d="m9 18 6-6-6-6"/>',
  /* ISS-087 设置页左导航：scope（监控范围）——十字准星/同心圆靶，
   * 表达「选定一块扫描范围」；与 gauge / activity / pie 同 24 viewBox 语言 */
  scope:
    '<circle cx="12" cy="12" r="9"/>' +
    '<circle cx="12" cy="12" r="5"/>' +
    '<line x1="12" y1="2" x2="12" y2="5"/>' +
    '<line x1="12" y1="19" x2="12" y2="22"/>' +
    '<line x1="2" y1="12" x2="5" y2="12"/>' +
    '<line x1="19" y1="12" x2="22" y2="12"/>',
  /* ISS-087 设置页左导航：clock（计划与通知）——钟表轮廓 + 时分针，
   * 表达「定时扫描 / 计划」语义，与 scan_coordinator / launchd 概念吻合 */
  clock:
    '<circle cx="12" cy="12" r="9"/>' +
    '<polyline points="12 7 12 12 15.5 14"/>',
  /* ISS-087 设置页左导航：stethoscope（高级与诊断）——探诊听筒轮廓，
   * 表达「排障 / 高级与诊断」语义；与 icons.js 既有 info / settings 区分 */
  stethoscope:
    '<path d="M5 3v6a5 5 0 0 0 10 0V3"/>' +
    '<path d="M5 3h2"/>' +
    '<path d="M13 3h2"/>' +
    '<path d="M15 13a3 3 0 1 0 3 3v-1"/>' +
    '<path d="M18 15v2a4 4 0 0 0 4 4h0"/>',
  /* ISS-087 设置页左导航：info（关于）——圆环 + 中央 i 字符的几何；
   * 关于区本身用 brandBasin 作大尺寸应用图标，info 仅作左导航 glyph */
  info:
    '<circle cx="12" cy="12" r="9"/>' +
    '<line x1="12" y1="11" x2="12" y2="17"/>' +
    '<circle cx="12" cy="7.6" r="0.6" fill="currentColor" stroke="none"/>',
};

/**
 * 生成内联 SVG。
 * @param {string} name 图标名（ICON_PATHS 键）
 * @param {number} size 像素尺寸，默认 18
 * @param {string} cls 额外 class
 *
 * 通用图标走 lucide 风格 stroke 模板；brandBasin 是实色 fill 几何（层叠
 * 嵌套 + notch，ISS-088 起各子元素带固定采样 fill 属性），SVG 根须清掉
 * stroke 属性，否则 stroke-width=2 会污染椭圆边缘、与品牌语义不符。
 */
export function icon(name, size = 18, cls = "") {
  const paths = ICON_PATHS[name];
  if (!paths) return "";
  const wrap = `<span class="icon${cls ? " " + cls : ""}" style="width:${size}px;height:${size}px" aria-hidden="true">`;
  if (name === "brandBasin") {
    return (
      wrap +
      `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" stroke="none">${paths}</svg></span>`
    );
  }
  return (
    wrap +
    `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" ` +
    `stroke-width="2" stroke-linecap="round" stroke-linejoin="round">${paths}</svg></span>`
  );
}
