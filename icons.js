/* SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
 * Copyright (c) 2026 FXseek. All rights reserved.
 * 本文件是 FXseek 的一部分：非商业用途免费，商业用途需取得授权（见仓库根目录 COMMERCIAL.md）。
 */
/* ==========================================================================
   图标库 —— 统一的线性 SVG 图标（Lucide / Feather 风格）
   用法： icon('search')  或  icon('video', 16)
   特点： 统一 24x24 视口、1.75 线宽、currentColor 描边，随文字颜色自适应
   ========================================================================== */
(function (global) {
  const P = {           // 路径数据（仅 path/circle/line 等基本图元）
    /* 导航类 */
    all:        '<path d="M3 6h18M3 12h18M3 18h18"/>',
    gallery:    '<rect x="3" y="3" width="18" height="18" rx="2"/><circle cx="9" cy="9" r="2"/><path d="m21 15-3.1-3.1a2 2 0 0 0-2.8 0L6 21"/>',
    video:      '<rect x="2" y="5" width="14" height="14" rx="2"/><path d="m22 8-6 4 6 4V8Z"/>',
    image:      '<rect x="3" y="3" width="18" height="18" rx="2"/><circle cx="9" cy="9" r="2"/><path d="m21 15-3.1-3.1a2 2 0 0 0-2.8 0L6 21"/>',
    audio:      '<path d="M9 18V5l12-2v13"/><circle cx="6" cy="18" r="3"/><circle cx="18" cy="16" r="3"/>',
    document:   '<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><path d="M14 2v6h6M9 13h6M9 17h6"/>',
    folder:     '<path d="M20 20a2 2 0 0 0 2-2V8a2 2 0 0 0-2-2h-7.9a2 2 0 0 1-1.69-.9L9.6 3.9A2 2 0 0 0 7.93 3H4a2 2 0 0 0-2 2v13a2 2 0 0 0 2 2Z"/>',
    folderPlus: '<path d="M20 20a2 2 0 0 0 2-2V8a2 2 0 0 0-2-2h-7.9a2 2 0 0 1-1.69-.9L9.6 3.9A2 2 0 0 0 7.93 3H4a2 2 0 0 0-2 2v13a2 2 0 0 0 2 2Z"/><path d="M12 10v6M9 13h6"/>',
    layers:     '<path d="m12 2 9 5-9 5-9-5 9-5Z"/><path d="m3 12 9 5 9-5"/><path d="m3 17 9 5 9-5"/>',

    /* 操作类 */
    search:     '<circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/>',
    refresh:    '<path d="M21 12a9 9 0 1 1-2.64-6.36"/><path d="M21 3v6h-6"/>',
    brain:      '<path d="M12 5a3 3 0 0 0-6 0v.5A3 3 0 0 0 4 8c0 1.1.6 2.1 1.5 2.6A3 3 0 0 0 6 17a3 3 0 0 0 6 0Z"/><path d="M12 5a3 3 0 0 1 6 0v.5A3 3 0 0 1 20 8c0 1.1-.6 2.1-1.5 2.6A3 3 0 0 1 18 17a3 3 0 0 1-6 0Z"/>',
    upload:     '<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><path d="m7 9 5-5 5 5M12 4v12"/>',
    imageSearch:'<rect x="3" y="3" width="18" height="18" rx="2"/><circle cx="9" cy="9" r="2"/><path d="m21 15-3.1-3.1a2 2 0 0 0-2.8 0L6 21"/><path d="m17 17 3 3"/>',
    sortAsc:    '<path d="m3 16 4 4 4-4M7 20V4"/><path d="M21 8h-6M21 12h-4M21 16h-2"/>',
    grid:       '<rect x="3" y="3" width="7" height="7" rx="1"/><rect x="14" y="3" width="7" height="7" rx="1"/><rect x="3" y="14" width="7" height="7" rx="1"/><rect x="14" y="14" width="7" height="7" rx="1"/>',
    list:       '<path d="M8 6h13M8 12h13M8 18h13"/><circle cx="3.5" cy="6" r="1"/><circle cx="3.5" cy="12" r="1"/><circle cx="3.5" cy="18" r="1"/>',
    plus:       '<path d="M12 5v14M5 12h14"/>',
    close:      '<path d="M18 6 6 18M6 6l12 12"/>',
    check:      '<path d="M20 6 9 17l-5-5"/>',
    play:       '<path d="M6 4v16l14-8Z"/>',
    pause:      '<path d="M9 4.5v15M15 4.5v15"/>',
    volume:     '<path d="M11 5 6.5 9H3v6h3.5L11 19V5Z"/><path d="M15.5 9.2a4 4 0 0 1 0 5.6"/><path d="M18.3 6.6a8 8 0 0 1 0 10.8"/>',
    volumeOff:  '<path d="M11 5 6.5 9H3v6h3.5L11 19V5Z"/><path d="m16 10 5 4M21 10l-5 4"/>',
    fullscreen: '<path d="M8 3H5a2 2 0 0 0-2 2v3M16 3h3a2 2 0 0 1 2 2v3M8 21H5a2 2 0 0 1-2-2v-3M16 21h3a2 2 0 0 0 2-2v-3"/>',
    compress:   '<path d="M9 3v4a2 2 0 0 1-2 2H3M15 3v4a2 2 0 0 0 2 2h4M9 21v-4a2 2 0 0 0-2-2H3M15 21v-4a2 2 0 0 1 2-2h4"/>',
    rotate:     '<path d="M21 12a9 9 0 1 1-2.64-6.36"/><path d="M21 3v6h-6"/>',
    zoomIn:     '<circle cx="11" cy="11" r="7"/><path d="m20 20-3.6-3.6M11 8.2v5.6M8.2 11h5.6"/>',
    zoomOut:    '<circle cx="11" cy="11" r="7"/><path d="m20 20-3.6-3.6M8.2 11h5.6"/>',
    fit:        '<path d="M3 8V5.5A2.5 2.5 0 0 1 5.5 3H8M16 3h2.5A2.5 2.5 0 0 1 21 5.5V8M21 16v2.5a2.5 2.5 0 0 1-2.5 2.5H16M8 21H5.5A2.5 2.5 0 0 1 3 18.5V16"/>',
    chevronLeft:'<path d="m15 6-6 6 6 6"/>',
    chevronRight:'<path d="m9 6 6 6-6 6"/>',
    gauge:      '<path d="M12 21a9 9 0 1 0-9-9"/><path d="M3 12h4M12 3v4M18.4 5.6l-2.8 2.8"/><path d="m12 12 4.5-3.5"/>',
    settings:   '<circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 1 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 1 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 1 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 1 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1Z"/>',

    /* 状态类 */
    spinner:    '<path d="M21 12a9 9 0 1 1-6.22-8.56"/>',
    alert:      '<path d="M12 9v4M12 17h.01"/><path d="M10.29 3.86 1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0Z"/>',
    info:       '<circle cx="12" cy="12" r="9"/><path d="M12 16v-4M12 8h.01"/>',
    shield:     '<path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10Z"/>',
    save:       '<path d="M19 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11l5 5v11a2 2 0 0 1-2 2Z"/><path d="M17 21v-8H7v8M7 3v5h8"/>',
    download:   '<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><path d="m7 10 5 5 5-5M12 15V3"/>',
    trash:      '<path d="M3 6h18M8 6V4a1 1 0 0 1 1-1h6a1 1 0 0 1 1 1v2"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6"/><path d="M10 11v6M14 11v6"/>',
    cpu:        '<rect x="4" y="4" width="16" height="16" rx="2"/><rect x="9" y="9" width="6" height="6"/><path d="M9 2v2M15 2v2M9 20v2M15 20v2M2 9h2M2 15h2M20 9h2M20 15h2"/>',
    sparkles:   '<path d="m12 3-1.9 5.1L5 10l5.1 1.9L12 17l1.9-5.1L19 10l-5.1-1.9Z"/><path d="M5 19h.01M19 5h.01"/>',
    database:   '<ellipse cx="12" cy="5" rx="9" ry="3"/><path d="M3 5v14c0 1.7 4 3 9 3s9-1.3 9-3V5"/><path d="M3 12c0 1.7 4 3 9 3s9-1.3 9-3"/>',
    clock:      '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
    link:       '<path d="M10 13a5 5 0 0 0 7.5.5l3-3a5 5 0 0 0-7-7l-1.5 1.5"/><path d="M14 11a5 5 0 0 0-7.5-.5l-3 3a5 5 0 0 0 7 7L12 19"/>',
    eye:        '<path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7-10-7-10-7Z"/><circle cx="12" cy="12" r="3"/>',
    eyeOff:     '<path d="M10.7 5.1A10.9 10.9 0 0 1 12 5c6.5 0 10 7 10 7a13.2 13.2 0 0 1-1.7 2.7"/>' +
                '<path d="M6.6 6.6A13.5 13.5 0 0 0 2 12s3.5 7 10 7a9.7 9.7 0 0 0 5.4-1.6"/>' +
                '<path d="M9.9 9.9a3 3 0 0 0 4.2 4.2"/><path d="m2 2 20 20"/>',
    wand:       '<path d="m15 4-11 11 2 2 11-11-2-2Z"/><path d="M18 13v4M16 15h4M6 4v2M5 5h2"/>',
    scale:      '<path d="M12 3v18M7 7l-4 8h8L7 7ZM17 7l-4 8h8l-4-8Z"/>',
    chevronDown:'<path d="m6 9 6 6 6-6"/>',
    arrowUp:    '<path d="M12 19V5M5 12l7-7 7 7"/>',
    arrowDown:  '<path d="M12 5v14M5 12l7 7 7-7"/>',
    arrowUpDown:'<path d="m7 15 5 5 5-5M7 9l5-5 5 5"/>',
    home:       '<path d="m3 9 9-7 9 7v11a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2Z"/><path d="M9 22V12h6v10"/>',
    x:          '<path d="M18 6 6 18M6 6l12 12"/>',
    mic:        '<path d="M12 2a3 3 0 0 0-3 3v6a3 3 0 0 0 6 0V5a3 3 0 0 0-3-3Z"/><path d="M19 10v1a7 7 0 0 1-14 0v-1M12 18v4"/>',
    star:       '<path d="M11.525 2.295a.53.53 0 0 1 .95 0l2.31 4.679a2.123 2.123 0 0 0 1.595 1.16l5.166.756a.53.53 0 0 1 .294.904l-3.736 3.638a2.123 2.123 0 0 0-.611 1.878l.882 5.14a.53.53 0 0 1-.771.56l-4.618-2.428a2.122 2.122 0 0 0-1.973 0L6.396 21.01a.53.53 0 0 1-.77-.56l.881-5.139a2.122 2.122 0 0 0-.611-1.879L2.16 9.795a.53.53 0 0 1 .294-.906l5.165-.755a2.122 2.122 0 0 0 1.597-1.16z"/>',
  };

  function icon(name, size) {
    const d = P[name];
    if (!d) return '';
    const s = size || 16;
    return `<svg class="ico" width="${s}" height="${s}" viewBox="0 0 24 24" fill="none"
      stroke="currentColor" stroke-width="1.75" stroke-linecap="round"
      stroke-linejoin="round" aria-hidden="true">${d}</svg>`;
  }

  /* 常用图标 + 文字的组合 */
  icon.label = function (name, text, size) {
    return icon(name, size) + '<span>' + text + '</span>';
  };

  global.icon = icon;
})(window);
