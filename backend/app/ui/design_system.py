"""Ant Design Pro 风格的公共样式与本地线性 SVG 图标。"""

from urllib.parse import quote

# 图标仅作装饰；操作名称保留在原生按钮中，不增加网络或字体依赖。
_ICON_SHAPES = {
    "home": '<path d="m3 10 9-7 9 7v10H3z"/><path d="M9 20v-7h6v7"/>',
    "book": '<path d="M12 5v16M12 5C9 3 5 3 2 4v15c3-1 7-1 10 2 3-3 7-3 10-2V4c-3-1-7-1-10 1Z"/>',
    "library": '<rect x="3" y="4" width="4" height="16" rx="1"/><rect x="9" y="4" width="4" height="16" rx="1"/><path d="m16 4 5 15-4 1-5-15Z"/>',
    "questions": '<rect x="3" y="3" width="18" height="18" rx="2"/><path d="M7 8h1m3 0h6M7 12h1m3 0h6M7 16h1m3 0h6"/>',
    "exam": '<rect x="5" y="5" width="14" height="16" rx="2"/><rect x="9" y="3" width="6" height="4" rx="1"/><path d="m9 13 2 2 4-4"/>',
    "sparkles": '<path d="m12 3 2.5 6.5L21 12l-6.5 2.5L12 21l-2.5-6.5L3 12l6.5-2.5ZM20 2v4m-2-2h4"/>',
    "review": '<path d="M14 3H5v18h14V8Z"/><path d="M14 3v5h5m-10 8 2 2 4-4M8 10h3"/>',
    "chart": '<path d="M4 3v17h17M8 16v-4m5 4V8m5 8V5"/>',
    "users": '<circle cx="9" cy="8" r="3"/><path d="M3 21v-3a6 6 0 0 1 12 0v3m1-16a3 3 0 0 1 0 6m2 4a6 6 0 0 1 3 5"/>',
    "shield": '<path d="m12 3 8 3v6c0 5-8 9-8 9s-8-4-8-9V6Z"/><path d="m8 12 3 3 5-6"/>',
    "activity": '<path d="M2 12h5l3-8 4 16 3-8h5"/>',
    "bell": '<path d="M18 8a6 6 0 0 0-12 0c0 7-3 7-3 9h18c0-2-3-2-3-9M10 21h4"/>',
    "plus": '<path d="M12 5v14M5 12h14"/>',
    "refresh": '<path d="M20 7v5h-5M4 17v-5h5"/><path d="M5 8a8 8 0 0 1 13-3l2 3M4 16l2 3a8 8 0 0 0 13-3"/>',
    "back": '<path d="m10 5-7 7 7 7M3 12h18"/>',
    "edit": '<path d="m16 3 5 5-12 12-6 1 1-6Z"/><path d="m13 6 5 5"/>',
    "save": '<path d="M20 21H4V3h13l3 3ZM7 3v6h9V3M7 21v-7h10v7"/>',
    "reset": '<path d="m5 5 14 14M19 5 5 19"/>',
}
_NAVIGATION_ICONS = {
    "teacher-home": "home",
    "teacher-courses": "book",
    "teacher-knowledge": "library",
    "teacher-questions": "questions",
    "teacher-paper_import": "review",
    "teacher-exams": "exam",
    "teacher-generate": "sparkles",
    "teacher-review": "review",
    "teacher-analytics": "chart",
    "student-home": "home",
    "student-exams": "exam",
    "student-results": "chart",
    "admin-home": "home",
    "admin-users": "users",
    "admin-roles": "shield",
    "admin-status": "activity",
}
_ACTION_ICONS = {
    "edu-message-button": "bell",
    "edu-page-back": "back",
    "edu-question-new": "plus",
    "edu-question-refresh": "refresh",
    "edu-question-reset": "reset",
    "edu-question-back": "back",
    "edu-question-edit": "edit",
    "edu-question-save": "save",
    "edu-paper-edit": "edit",
    "edu-paper-save": "save",
    "edu-paper-cancel": "back",
    "edu-paper-confirm": "exam",
    "edu-paper-refresh": "refresh",
    "edu-paper-courses": "refresh",
    "edu-paper-upload": "plus",
    "edu-knowledge-refresh": "refresh",
    "edu-course-new": "plus",
    "edu-course-create": "save",
    "edu-course-save": "save",
    "edu-course-cancel": "back",
    "edu-knowledge-new": "plus",
    "edu-knowledge-save": "save",
    "edu-knowledge-cancel": "back",
    "edu-generation-refresh": "refresh",
    "edu-generation-generate": "sparkles",
    "edu-generation-list": "refresh",
    "edu-exams-refresh": "refresh",
    "edu-exams-new": "plus",
    "edu-exams-save": "save",
    "edu-exams-cancel": "back",
    "edu-review-refresh": "refresh",
    "edu-review-confirm": "save",
    "edu-review-save": "save",
    "edu-review-cancel": "back",
    "edu-review-next": "exam",
}


def _icon_rules() -> str:
    selectors: dict[str, list[str]] = {}
    for key, name in _NAVIGATION_ICONS.items():
        selectors.setdefault(name, []).append(f"#edu-nav-{key}::before")
    for key, name in _ACTION_ICONS.items():
        selectors.setdefault(name, []).append(f"#{key}::before")
    selectors.setdefault("book", []).append(".edu-logo-mark::before")
    result = []
    for name, targets in selectors.items():
        svg = (
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" '
            'fill="none" stroke="black" stroke-width="1.8" '
            'stroke-linecap="round" stroke-linejoin="round">'
            + _ICON_SHAPES[name]
            + "</svg>"
        )
        url = f'url("data:image/svg+xml,{quote(svg, safe="")}")'
        result.append(
            f"{', '.join(targets)} {{ mask-image: {url}; -webkit-mask-image: {url}; }}"
        )
    return "\n".join(result)


DESIGN_SYSTEM_CSS = _icon_rules() + """
#edu-root { --edu-blue: #1677ff; --edu-ink: #1f2937; --edu-line: #e8ebef;
    --edu-muted: #7b8493; font-family: Inter, 'Segoe UI', 'Microsoft YaHei', sans-serif;
    background: #f5f7fa; }
#edu-root .edu-nav::before, #edu-root .edu-icon::before, .edu-logo-mark::before {
    content: ''; display: inline-block; flex: 0 0 18px; width: 18px; height: 18px;
    background: currentColor; mask-repeat: no-repeat; mask-size: contain;
    -webkit-mask-repeat: no-repeat; -webkit-mask-size: contain; }
#edu-topbar { height: 60px; min-height: 60px; padding: 0 24px; gap: 20px;
    box-shadow: 0 1px 4px rgba(16, 24, 40, .04); border-color: #edf0f3; }
#edu-brand { flex-basis: 198px; }
#edu-root .edu-brand-lockup { display: flex; align-items: center; gap: 10px; }
#edu-root .edu-logo-mark { display: flex; align-items: center; justify-content: center;
    color: #fff; background: #1677ff; width: 32px; height: 32px; border-radius: 8px; }
#edu-brand strong { color: #17233d; font-size: 20px; letter-spacing: -.4px; }
#edu-context { color: #7b8493; font-size: 13px; }
#edu-search { flex: 0 1 280px; }
#edu-search input { background: #f5f7fa; border-radius: 6px; }
#edu-message-button { border: 0; background: transparent; color: #677285; gap: 8px; }
#edu-user-menu > .label-wrap { border: 0; background: #f7f9fc; border-radius: 6px; }
#edu-shell-row { min-height: calc(100vh - 60px); }
#edu-sidebar { flex-basis: 232px; width: 232px; padding: 20px 12px;
    border-right: 1px solid #edf0f3; }
#edu-sidebar .edu-nav { border: 0; border-radius: 6px; gap: 12px; padding: 0 14px;
    color: #596579; font-weight: 400; }
#edu-sidebar .edu-nav-active { color: #1677ff; background: #eaf3ff; font-weight: 600; }
#edu-sidebar .edu-nav-active:hover { background: #eaf3ff; }
#edu-sidebar .edu-group-title.block { padding: 22px 14px 8px; color: #929bac; }
#edu-content { padding: 24px 28px; gap: 16px; }
#edu-root button { border-radius: 6px; font-size: 14px; font-weight: 500; }
#edu-root button.primary { background: #1677ff; border-color: #1677ff; box-shadow: none; }
#edu-root button.primary:hover { background: #4096ff; border-color: #4096ff; }
#edu-root .edu-icon { gap: 8px; }
#edu-page-header { min-height: 32px; }
#edu-page-breadcrumb { color: #8993a3; font-size: 13px; }
#edu-message-panel { border-radius: 8px; }
#edu-message-panel > .label-wrap { color: #677285; }
#edu-question-bank { gap: 20px; }
#edu-question-bank .edu-question-heading { align-items: center; gap: 16px; }
#edu-root .edu-question-title h2 { margin: 0 0 8px; color: #17233d; font-size: 24px; font-weight: 600; }
#edu-root .edu-question-title p { margin: 0; color: #7b8493; font-size: 13px; line-height: 1.6; }
#edu-question-new { flex: 0 0 136px; min-width: 136px; }
#edu-question-list { gap: 18px; }
#edu-question-bank .edu-surface { padding: 20px 24px; border: 1px solid #edf0f3;
    background: #fff; border-radius: 8px; gap: 16px; box-shadow: 0 1px 3px rgba(16,24,40,.025); }
#edu-question-bank .edu-filter-row { gap: 16px; align-items: flex-end; }
#edu-question-bank .edu-filter-row > .block { min-width: 120px !important; }
#edu-question-bank .edu-filter-actions { flex: 0 0 auto; min-width: 0; gap: 8px; }
#edu-question-bank .edu-filter-actions button { min-width: 84px; }
#edu-question-bank .edu-surface .block { border: 0; box-shadow: none; }
#edu-question-bank .edu-filter-row .block { background: transparent; padding: 0; }
#edu-question-bank .edu-filter-row label { font-size: 13px; color: #596579; }
#edu-question-bank .edu-filter-row input { background: #fff; }
#edu-root .edu-question-summary { display: flex; gap: 24px; align-items: center; flex-wrap: wrap;
    color: #8993a3; font-size: 13px; }
#edu-root .edu-question-summary strong { color: #17233d; font-size: 16px; font-weight: 600; margin-left: 6px; }
#edu-root .edu-question-summary .edu-summary-pending strong { color: #d89614; }
#edu-root .edu-question-summary .edu-summary-approved strong { color: #389e0d; }
#edu-root .edu-question-list-title { display: flex; align-items: center; gap: 14px; }
#edu-root .edu-question-list-title h3 { margin: 0; color: #17233d; font-size: 16px; font-weight: 600; }
#edu-root .edu-question-list-title span { color: #8993a3; font-size: 12px; }
#edu-question-bank .edu-question-table { padding: 0; background: #fff; }
#edu-question-bank .edu-question-table > label { display: none; }
#edu-question-bank .edu-question-table .table-wrap { border: 0; border-radius: 0; }
#edu-question-bank .edu-question-table th { background: #fafbfc; font-size: 13px; color: #596579; }
#edu-question-bank .edu-question-table td { font-size: 13px; border-color: #f0f2f5; padding: 14px 12px; }
#edu-question-bank .edu-question-table td:first-child { color: #1677ff; }
#edu-question-bank .edu-question-table tbody tr { cursor: pointer; }
#edu-question-bank .edu-question-table tbody tr:hover td { background: #f5f9ff; }
#edu-question-message { min-height: 0; }
#edu-question-message:empty { display: none; }
#edu-question-detail { gap: 20px; }
#edu-question-detail .edu-detail-toolbar { align-items: center; flex-wrap: wrap; gap: 10px; }
#edu-question-detail .edu-detail-toolbar > button { flex: 0 0 auto; width: auto; }
#edu-question-detail-status { flex: 1 1 auto; min-width: 160px; }
#edu-question-preview { padding: 0; }
#edu-root .edu-question-preview-grid { display: grid; grid-template-columns: minmax(0, 1.5fr) minmax(0, 1fr); gap: 20px; }
#edu-root .edu-question-preview-section { background: #fff; border: 1px solid #edf0f3; border-radius: 8px; padding: 24px; }
#edu-root .edu-question-preview-section h3 { margin: 0 0 20px; font-size: 16px; color: #17233d; }
#edu-root .edu-question-preview-section h4 { margin: 24px 0 10px; font-size: 13px; color: #677285; font-weight: 500; }
#edu-root .edu-question-body { white-space: pre-wrap; overflow-wrap: anywhere; color: #364152; font-size: 14px; line-height: 1.85; }
#edu-root .edu-question-option { display: flex; gap: 12px; padding: 12px 0; border-bottom: 1px solid #f0f2f5; }
#edu-root .edu-question-option b { color: #8993a3; font-weight: 500; }
#edu-root .edu-question-meta { display: flex; gap: 8px; flex-wrap: wrap; margin-bottom: 20px; }
#edu-root .edu-question-tag { display: inline-block; font-size: 12px; color: #677285; background: #f5f7fa;
    border: 1px solid #edf0f3; border-radius: 4px; padding: 3px 8px; }
#edu-question-editor { max-width: 960px; width: 100%; margin: 0 auto; }
#edu-question-editor .block { background: transparent; }
#edu-question-editor .edu-editor-actions { border-top: 1px solid #edf0f3; padding-top: 18px; }
#edu-question-editor .edu-editor-actions > button { flex: 0 0 auto; width: auto; }
#edu-question-delete { border: 1px solid #fee2e2; background: #fff; border-radius: 8px; }
#edu-root .html-container, #edu-root .prose.gradio-style { padding: 0; }
#edu-root #edu-feedback:has(.prose:empty), #edu-root #edu-search-feedback:has(.prose:empty) { display: none; }
#edu-root #edu-question-message:has(.prose:empty), #edu-root #edu-message-empty:has(.prose:empty) { display: none; }
#edu-root .edu-logo-mark { color: #fff; }
#edu-root button.edu-icon { display: inline-flex; align-items: center; justify-content: center; white-space: nowrap; flex-wrap: nowrap;
    padding: 0 14px; height: 44px; }
#edu-root #edu-message-panel { padding: 0; }
#edu-root #edu-message-panel:not(:has(> .label-wrap.open)) { display: none; }
#edu-root #edu-question-delete { overflow-x: hidden !important; padding: 0 !important; }
#edu-root #edu-question-bank .edu-filter-row > .form { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr));
    gap: 16px; min-width: 0 !important; flex: 1 1 0; border: 0; background: transparent; }
#edu-root #edu-question-bank .edu-filter-row > .form > .block { min-width: 0 !important; }
#edu-root #edu-question-bank .edu-filter-actions { flex: 0 0 176px; width: 176px; }
#edu-root #edu-question-bank .edu-filter-actions button { flex: 1 1 0; width: auto; }
#edu-root #edu-question-bank .edu-filter-row [data-testid="block-info"] { font-size: 13px; color: #596579; margin-bottom: 8px; }
#edu-root #edu-question-bank .edu-question-table button { min-height: 0; min-width: 0; font-weight: 400; }
.gradio-container:has(#edu-root) footer { display: none; }
#edu-root #edu-question-detail-status { flex: 1 1 0; width: auto; min-width: 120px !important; }
#edu-root #edu-question-bank .edu-question-table .header-row { min-height: 32px; margin: 0; justify-content: flex-end; }
#edu-root #edu-question-bank .edu-question-table .header-row .label { display: none; }
#edu-root #edu-question-bank .edu-question-table [role="button"] { font-family: inherit; letter-spacing: 0; }
#edu-root #edu-question-bank .edu-question-table .toolbar button { height: 32px; width: 32px; }
@media (max-width: 1199px) and (min-width: 768px) {
    #edu-sidebar { width: 200px; flex-basis: 200px; }
    #edu-brand { flex-basis: 166px; }
    #edu-content { padding: 20px; }
    #edu-question-bank .edu-filter-row { flex-wrap: wrap; }
    #edu-root #edu-question-bank .edu-filter-actions { flex-basis: 176px; }
}
@media (max-width: 767px) {
    #edu-topbar { min-height: 60px; height: auto; padding: 8px 12px; gap: 8px; }
    #edu-brand { flex-basis: 132px; }
    #edu-search { flex: 1 1 100%; }
    #edu-root #edu-brand strong { font-size: 17px; }
    #edu-root #edu-message-button { padding: 0 8px; flex: 0 0 82px; min-width: 82px; }
    #edu-root #edu-user-menu { flex: 0 0 104px; width: 104px; }
    #edu-sidebar { flex-basis: auto; width: 100%; padding: 4px 12px; }
    #edu-sidebar .edu-group-title.block { padding-top: 12px; }
    #edu-content { padding: 16px 12px; }
    #edu-question-bank .edu-surface { padding: 16px; }
    #edu-question-bank .edu-question-heading > button { flex-basis: auto; width: 100%; }
    #edu-root #edu-question-bank .edu-filter-row > .form { grid-template-columns: minmax(0, 1fr); width: 100%; flex: 0 0 auto; }
    #edu-root #edu-question-bank .edu-filter-actions { flex: 0 0 auto; flex-direction: row; width: 100%; }
    #edu-question-bank .edu-filter-actions button { flex: 1 1 0; }
    #edu-root .edu-question-summary { gap: 12px; }
    #edu-root .edu-question-preview-grid { grid-template-columns: minmax(0, 1fr); }
    #edu-root .edu-question-preview-section { padding: 20px; }
    #edu-question-detail .edu-detail-toolbar { flex-direction: row; }
    #edu-question-detail-status { flex-basis: 100%; }
    #edu-question-editor .edu-editor-actions { flex-direction: row; flex-wrap: wrap; }
}
"""


# Shared page scales reuse the established question-bank theme and local icons.
DESIGN_SYSTEM_CSS += """
#edu-root { --edu-font-body: 14px; --edu-font-title: 24px; --edu-font-section: 16px;
    --edu-space: 16px; --edu-surface-padding: 20px; }
#edu-root .edu-business-page { gap: var(--edu-space); min-width: 0 !important; }
#edu-root .edu-business-page h2 { font-size: var(--edu-font-title); line-height: 1.4; margin: 0 0 8px; }
#edu-root .edu-business-page h3 { font-size: var(--edu-font-section); line-height: 1.5; margin: 0 0 12px; }
#edu-root .edu-business-page label, #edu-root .edu-business-page input,
#edu-root .edu-business-page textarea, #edu-root .edu-business-page p { font-size: var(--edu-font-body); line-height: 1.6; }
#edu-root .edu-business-page .edu-surface,
#edu-root .edu-business-page .result-tabs { padding: var(--edu-surface-padding); border: 1px solid var(--edu-line);
    border-radius: 8px; background: white; min-width: 0 !important; }
#edu-root .edu-business-page .row { gap: var(--edu-space); }
#edu-root .edu-business-page button { border-radius: 6px; font-size: var(--edu-font-body); min-height: 44px; }
#edu-root .edu-business-page .edu-actions { align-items: center; flex-wrap: wrap; }
#edu-root .edu-business-page .edu-actions > button { flex: 0 1 auto; width: auto; }
#edu-root .edu-business-page .edu-status-table { border: 1px solid var(--edu-line); border-radius: 8px; }
#edu-root .edu-business-page .edu-status-table th { background: #fafbfc; color: #596579; font-size: 13px; }
#edu-root .edu-business-page .edu-status-table td { font-size: 13px; line-height: 1.6; }
#edu-root .edu-business-page .edu-status-table button { min-height: 0; font-size: 13px; }
#edu-knowledge .block { min-width: 0 !important; }
#edu-knowledge-refresh, #edu-course-new { flex: 0 0 auto; width: auto; min-width: 100px !important; }
#edu-root .edu-business-page .gr-accordion > .label-wrap { box-sizing: border-box; }
#edu-root .edu-business-page .gr-accordion > .label-wrap > .icon { width: 12px; height: 12px; min-width: 0; min-height: 0; }
#edu-course-editor .row, #edu-knowledge-editor .row { flex-wrap: wrap; }
#edu-course-editor .row > button, #edu-knowledge-editor .row > button {
    flex: 0 0 auto; width: auto; min-width: 0 !important; }
#edu-paper-import .edu-paper-columns { align-items: flex-start; }
#edu-paper-import .edu-paper-columns > .column { min-width: 0 !important; flex-basis: 360px; }
#edu-paper-import .edu-paper-preview { white-space: pre-wrap; overflow-wrap: anywhere; }
#edu-paper-import .edu-paper-preview h3 { margin-top: 16px; }
#edu-paper-import .edu-paper-preview ul { padding-left: 20px; }
#edu-root .edu-state-banner { border-radius: 6px; font-size: var(--edu-font-body); }
#edu-root .edu-business-page .result-summary { background: white; border: 1px solid var(--edu-line); border-radius: 8px; }
@media (max-width: 767px) {
    #edu-root { --edu-surface-padding: 16px; --edu-font-title: 22px; }
    #edu-paper-import .edu-paper-columns { flex-direction: column; }
    #edu-paper-import .edu-paper-columns > .column { flex-basis: auto; width: 100%; }
    #edu-root .edu-business-page .edu-actions > button { flex: 1 1 auto; }
}
"""
