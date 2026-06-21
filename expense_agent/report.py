"""
expense_agent/report.py — 月底核銷稽核報告產生器

CLI 用法:
    uv run python -m expense_agent.report
    uv run python -m expense_agent.report --log tests/demo_audit_log.jsonl --month 2026-06 --out reports/

5 頁 PPTX:
  1. 封面
  2. 總覽統計
  3. 紅旗分類統計
  4. Top 可疑案件表（遮蔽姓名 + case_id）
  5. 風險摘要與建議（Gemini 生成）

配色:
  - 封面: 深藍 #1F3864 底、白字
  - 內頁: 白底、深灰 #2C3E50 文字
  - 高風險紅旗: 紅色 #C0392B
  - 強調色: 金黃 #F0B429
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# ── python-pptx ──────────────────────────────────────────
from pptx import Presentation
from pptx.dml.color import RGBColor
from pptx.enum.text import PP_ALIGN
from pptx.util import Inches, Pt

# ── Gemini (google-genai 同步 SDK) ───────────────────────
from google import genai

from .config import MODEL_NAME

# ── 路徑常數 ──────────────────────────────────────────────
_DATA_DIR = Path(__file__).parent / "data"
_DEFAULT_LOG = _DATA_DIR / "audit_log.jsonl"
_DEFAULT_OUT = Path(__file__).parent.parent / "reports"

# ── 配色 ──────────────────────────────────────────────────
C_NAVY   = RGBColor(0x1F, 0x38, 0x64)   # 深藍（封面底色）
C_GOLD   = RGBColor(0xF0, 0xB4, 0x29)   # 金黃（強調）
C_DARK   = RGBColor(0x2C, 0x3E, 0x50)   # 深灰（內頁文字）
C_RED    = RGBColor(0xC0, 0x39, 0x2B)   # 紅色（高風險）
C_WHITE  = RGBColor(0xFF, 0xFF, 0xFF)
C_LIGHT  = RGBColor(0xF5, 0xF6, 0xFA)   # 淡灰（表頭底色）
C_BORDER = RGBColor(0xCC, 0xCC, 0xCC)

# 投影片尺寸 (widescreen 16:9)
SLIDE_W = Inches(13.33)
SLIDE_H = Inches(7.5)


# ─────────────────────────────────────────────────────────
# 工具函式
# ─────────────────────────────────────────────────────────

def mask_name(name: str) -> str:
    """遮蔽姓名：王三豐 → 王○豐；英文：John Smith → J*** S***"""
    if not name or name in ("Unknown", "UNKNOWN"):
        return "○○○"
    # 英文（含空格，通常「名 姓」格式）
    if all(c.isascii() for c in name.replace(" ", "")):
        parts = name.split()
        return " ".join(p[0] + "***" if len(p) > 1 else p for p in parts)
    # 中文
    chars = list(name)
    if len(chars) <= 1:
        return name
    if len(chars) == 2:
        return chars[0] + "○"
    # 3字以上：頭尾保留，中間全○
    return chars[0] + "○" * (len(chars) - 2) + chars[-1]


def load_log(log_path: Path, month: str | None = None) -> list[dict]:
    """讀 audit_log.jsonl，可依 YYYY-MM 過濾。"""
    if not log_path.exists():
        return []
    entries = []
    with open(log_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
                if month:
                    # 比對 date 欄位（YYYY-MM-DD）或 timestamp
                    date_str = entry.get("date", "") or entry.get("timestamp", "")
                    if not date_str.startswith(month):
                        continue
                entries.append(entry)
            except Exception:
                continue
    return entries


def classify_flags(entries: list[dict]) -> dict[str, int]:
    """把 fraud_flags 分類計數。"""
    counts = {
        "歇業廠商": 0,
        "拆單規避": 0,
        "業務費超標": 0,
        "出差浮報": 0,
        "Injection/PII": 0,
        "其他": 0,
    }
    for e in entries:
        for flag in e.get("fraud_flags", []):
            f = str(flag)
            if "歇業" in f:
                counts["歇業廠商"] += 1
            elif "拆單" in f:
                counts["拆單規避"] += 1
            elif "業務費" in f or "超上限" in f:
                counts["業務費超標"] += 1
            elif "出差" in f or "住宿" in f or "雜費" in f or "浮報" in f:
                counts["出差浮報"] += 1
            elif "Injection" in f or "注入" in f or "PII" in f or "REDACTED" in f:
                counts["Injection/PII"] += 1
            else:
                counts["其他"] += 1
    return counts


def build_stats(entries: list[dict]) -> dict[str, Any]:
    """彙整總覽統計數字。"""
    total = len(entries)
    approved = sum(1 for e in entries if e.get("status") == "APPROVED")
    rejected = sum(1 for e in entries if e.get("status") == "REJECTED")
    pending  = total - approved - rejected
    total_amt    = sum(e.get("amount", 0) for e in entries)
    flagged_amt  = sum(
        e.get("amount", 0) for e in entries if e.get("fraud_flags")
    )
    high_risk = sum(
        1 for e in entries if e.get("risk_level") in ("HIGH", "CRITICAL")
    )
    return {
        "total": total,
        "approved": approved,
        "rejected": rejected,
        "pending": pending,
        "total_amt": total_amt,
        "flagged_amt": flagged_amt,
        "high_risk": high_risk,
        "flag_counts": classify_flags(entries),
    }


def top_suspicious(entries: list[dict], n: int = 10) -> list[dict]:
    """回傳 fraud_flags 非空的案件，依金額降序，最多 n 筆。"""
    flagged = [e for e in entries if e.get("fraud_flags")]
    return sorted(flagged, key=lambda e: e.get("amount", 0), reverse=True)[:n]


# ─────────────────────────────────────────────────────────
# PPTX 輔助
# ─────────────────────────────────────────────────────────

def _fill_bg(slide, color: RGBColor) -> None:
    """設定投影片背景色。"""
    from pptx.oxml.ns import qn
    from lxml import etree
    bg = slide.background
    fill = bg.fill
    fill.solid()
    fill.fore_color.rgb = color


def _add_textbox(
    slide, left, top, width, height,
    text: str,
    font_size: int = 18,
    bold: bool = False,
    color: RGBColor = C_DARK,
    align=PP_ALIGN.LEFT,
    wrap: bool = True,
) -> Any:
    txBox = slide.shapes.add_textbox(left, top, width, height)
    tf = txBox.text_frame
    tf.word_wrap = wrap
    p = tf.paragraphs[0]
    p.alignment = align
    run = p.add_run()
    run.text = text
    run.font.size = Pt(font_size)
    run.font.bold = bold
    run.font.color.rgb = color
    return txBox


def _add_table(slide, left, top, width, rows: int, cols: int):
    """建立表格，回傳 table 物件。"""
    col_width = width // cols
    tbl = slide.shapes.add_table(rows, cols, left, top, width, Inches(0.4) * rows)
    return tbl.table


def _style_cell(cell, text: str, font_size: int = 11, bold: bool = False,
                bg: RGBColor | None = None, color: RGBColor = C_DARK,
                align=PP_ALIGN.LEFT) -> None:
    cell.text = text
    tf = cell.text_frame
    tf.paragraphs[0].alignment = align
    run = tf.paragraphs[0].runs
    if run:
        run[0].font.size = Pt(font_size)
        run[0].font.bold = bold
        run[0].font.color.rgb = color
    if bg:
        cell.fill.solid()
        cell.fill.fore_color.rgb = bg


# ─────────────────────────────────────────────────────────
# 5 頁投影片
# ─────────────────────────────────────────────────────────

def slide_cover(prs: Presentation, month: str, gen_time: str) -> None:
    """第 1 頁：封面"""
    slide = prs.slides.add_slide(prs.slide_layouts[6])  # blank
    _fill_bg(slide, C_NAVY)

    # 金色裝飾橫條
    bar = slide.shapes.add_shape(
        1,  # MSO_SHAPE_TYPE.RECTANGLE
        Inches(0), Inches(2.8), SLIDE_W, Inches(0.06)
    )
    bar.fill.solid(); bar.fill.fore_color.rgb = C_GOLD
    bar.line.fill.background()

    _add_textbox(slide, Inches(1), Inches(1.0), Inches(11), Inches(1),
                 "核銷稽核報告", font_size=44, bold=True, color=C_WHITE, align=PP_ALIGN.CENTER)
    _add_textbox(slide, Inches(1), Inches(2.0), Inches(11), Inches(0.7),
                 f"Expense Audit Report  ·  {month}", font_size=22, color=C_GOLD, align=PP_ALIGN.CENTER)
    _add_textbox(slide, Inches(1), Inches(3.1), Inches(11), Inches(0.5),
                 "由 Expense Audit Agent 自動生成  |  AI-Generated Report",
                 font_size=14, color=RGBColor(0xBD, 0xC3, 0xC7), align=PP_ALIGN.CENTER)
    _add_textbox(slide, Inches(1), Inches(6.5), Inches(11), Inches(0.4),
                 f"生成時間 Generated: {gen_time}",
                 font_size=11, color=RGBColor(0x95, 0xA5, 0xA6), align=PP_ALIGN.CENTER)


def slide_overview(prs: Presentation, stats: dict, month: str) -> None:
    """第 2 頁：總覽統計"""
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    _fill_bg(slide, C_WHITE)

    # 頂部深藍標題欄
    hdr = slide.shapes.add_shape(1, Inches(0), Inches(0), SLIDE_W, Inches(0.9))
    hdr.fill.solid(); hdr.fill.fore_color.rgb = C_NAVY; hdr.line.fill.background()
    _add_textbox(slide, Inches(0.3), Inches(0.1), Inches(12), Inches(0.7),
                 f"總覽  |  {month}", font_size=24, bold=True, color=C_WHITE)

    # 6 個數字卡片（2 列 × 3 欄）
    cards = [
        ("處理筆數",  str(stats["total"]),        C_DARK),
        ("自動核准",  str(stats["approved"]),      RGBColor(0x27, 0xAE, 0x60)),
        ("駁回",     str(stats["rejected"]),      C_RED),
        ("高風險案件", str(stats["high_risk"]),    C_RED),
        ("涉及總金額", f"NT$ {stats['total_amt']:,.0f}", C_DARK),
        ("可疑金額",  f"NT$ {stats['flagged_amt']:,.0f}", C_RED),
    ]
    card_w, card_h = Inches(3.8), Inches(2.2)
    for i, (label, value, val_color) in enumerate(cards):
        col, row = i % 3, i // 3
        left = Inches(0.5) + col * (card_w + Inches(0.3))
        top  = Inches(1.1) + row * (card_h + Inches(0.2))
        # 卡片背景
        rect = slide.shapes.add_shape(1, left, top, card_w, card_h)
        rect.fill.solid(); rect.fill.fore_color.rgb = C_LIGHT
        rect.line.color.rgb = C_BORDER; rect.line.width = Pt(0.5)
        _add_textbox(slide, left + Inches(0.1), top + Inches(0.15),
                     card_w - Inches(0.2), Inches(0.45),
                     label, font_size=16, color=RGBColor(0x7F, 0x8C, 0x8D))
        _add_textbox(slide, left + Inches(0.1), top + Inches(0.6),
                     card_w - Inches(0.2), Inches(1.0),
                     value, font_size=36, bold=True, color=val_color)


def slide_flag_stats(prs: Presentation, stats: dict, month: str) -> None:
    """第 3 頁：紅旗分類統計（橫條文字圖）"""
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    _fill_bg(slide, C_WHITE)

    hdr = slide.shapes.add_shape(1, Inches(0), Inches(0), SLIDE_W, Inches(0.9))
    hdr.fill.solid(); hdr.fill.fore_color.rgb = C_NAVY; hdr.line.fill.background()
    _add_textbox(slide, Inches(0.3), Inches(0.1), Inches(12), Inches(0.7),
                 f"紅旗分類統計  |  {month}", font_size=24, bold=True, color=C_WHITE)

    flag_counts = stats["flag_counts"]
    max_val = max(flag_counts.values()) if any(flag_counts.values()) else 1
    bar_max_w = Inches(7.5)
    row_h = Inches(0.85)
    start_top = Inches(1.1)

    for i, (label, count) in enumerate(flag_counts.items()):
        top = start_top + i * row_h
        # 標籤
        _add_textbox(slide, Inches(0.5), top + Inches(0.15),
                     Inches(3.2), Inches(0.55),
                     label, font_size=16, color=C_DARK, bold=(count > 0))
        # 橫條
        if count > 0:
            bar_w = bar_max_w * (count / max_val)
            bar = slide.shapes.add_shape(1, Inches(3.8), top + Inches(0.18),
                                         max(bar_w, Inches(0.3)), Inches(0.45))
            bar_color = C_RED if label in ("歇業廠商", "拆單規避", "Injection/PII") else C_GOLD
            bar.fill.solid(); bar.fill.fore_color.rgb = bar_color
            bar.line.fill.background()
            _add_textbox(slide, Inches(3.8) + max(bar_w, Inches(0.3)) + Inches(0.1),
                         top + Inches(0.15), Inches(1.2), Inches(0.5),
                         str(count), font_size=20, bold=True, color=bar_color)
        else:
            _add_textbox(slide, Inches(3.8), top + Inches(0.15),
                         Inches(2), Inches(0.5),
                         "0", font_size=16, color=RGBColor(0xBD, 0xC3, 0xC7))


def slide_top_cases(prs: Presentation, cases: list[dict], month: str) -> None:
    """第 4 頁：Top 可疑案件表"""
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    _fill_bg(slide, C_WHITE)

    hdr = slide.shapes.add_shape(1, Inches(0), Inches(0), SLIDE_W, Inches(0.9))
    hdr.fill.solid(); hdr.fill.fore_color.rgb = C_NAVY; hdr.line.fill.background()
    _add_textbox(slide, Inches(0.3), Inches(0.1), Inches(12), Inches(0.7),
                 f"Top 可疑案件  |  {month}（姓名已遮蔽）", font_size=24, bold=True, color=C_WHITE)

    if not cases:
        _add_textbox(slide, Inches(1), Inches(3), Inches(11), Inches(1),
                     "本月無可疑案件 🎉", font_size=28, color=RGBColor(0x27, 0xAE, 0x60),
                     align=PP_ALIGN.CENTER)
        return

    # 欄位定義：(header, 寬度 inch, 欄位取值 lambda)
    COL_DEFS = [
        ("案件編號",  1.5,  lambda e: e.get("case_id", "")),
        ("關聯案件",  1.6,  lambda e: ",\n".join(e.get("related_case_ids", [])) if e.get("related_case_ids") else ""),
        ("遮蔽姓名",  1.0,  lambda e: mask_name(e.get("submitter", ""))),
        ("類別",      1.2,  lambda e: e.get("category", "")),
        ("金額",      1.2,  lambda e: f"NT$ {e.get('amount', 0):,.0f}"),
        ("日期",      1.1,  lambda e: e.get("date", "")),
        ("紅旗摘要",  4.2,  lambda e: "；".join(e.get("fraud_flags", []))[:60] + ("…" if sum(len(f) for f in e.get("fraud_flags",[])) > 60 else "")),
        ("狀態",      1.0,  lambda e: e.get("status", "")),
    ]
    n_cols = len(COL_DEFS)
    n_rows = len(cases) + 1  # +1 表頭
    total_w = Inches(12.8)
    tbl = slide.shapes.add_table(
        n_rows, n_cols,
        Inches(0.27), Inches(1.0), total_w, Inches(0.38) * n_rows
    ).table

    # 欄寬
    col_widths = [Inches(w) for _, w, _ in COL_DEFS]
    for ci, cw in enumerate(col_widths):
        tbl.columns[ci].width = cw

    # 表頭
    for ci, (header, _, _) in enumerate(COL_DEFS):
        _style_cell(tbl.cell(0, ci), header,
                    font_size=14, bold=True, bg=C_NAVY, color=C_WHITE, align=PP_ALIGN.CENTER)

    # 資料列
    for ri, entry in enumerate(cases, start=1):
        is_critical = entry.get("risk_level") in ("HIGH", "CRITICAL")
        row_bg = RGBColor(0xFF, 0xF0, 0xF0) if is_critical else C_WHITE
        status = entry.get("status", "")
        for ci, (_, _, getter) in enumerate(COL_DEFS):
            val = getter(entry)
            cell_color = C_RED if (ci == n_cols - 1 and status == "REJECTED") else C_DARK
            _style_cell(tbl.cell(ri, ci), val,
                        font_size=12, bg=row_bg if ci == 0 else (row_bg if is_critical else C_WHITE),
                        color=cell_color)


def slide_risk_summary(prs: Presentation, stats: dict, month: str, gemini_text: str) -> None:
    """第 5 頁：風險摘要與建議（Gemini 生成）"""
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    _fill_bg(slide, C_WHITE)

    hdr = slide.shapes.add_shape(1, Inches(0), Inches(0), SLIDE_W, Inches(0.9))
    hdr.fill.solid(); hdr.fill.fore_color.rgb = C_NAVY; hdr.line.fill.background()
    _add_textbox(slide, Inches(0.3), Inches(0.1), Inches(12), Inches(0.7),
                 f"風險摘要與建議  |  {month}  ·  AI 生成", font_size=24, bold=True, color=C_WHITE)

    # Gemini 生成的文字（可能較長，用較小字體+自動換行）
    txt_box = slide.shapes.add_textbox(Inches(0.5), Inches(1.0), Inches(12.3), Inches(6.0))
    tf = txt_box.text_frame
    tf.word_wrap = True

    for line in gemini_text.split("\n"):
        line = line.strip()
        if not line:
            p = tf.add_paragraph()
            p.space_after = Pt(4)
            continue
        p = tf.add_paragraph()
        run = p.add_run()
        run.text = line
        # 建議條目（以數字或 • 開頭）加粗、紅色
        is_rec = line[:2] in ("1.", "2.", "3.", "4.", "5.") or line.startswith("•") or line.startswith("建議")
        run.font.size  = Pt(16 if is_rec else 18)
        run.font.bold  = is_rec
        run.font.color.rgb = C_RED if is_rec else C_DARK
        p.space_after  = Pt(6)


# ─────────────────────────────────────────────────────────
# Gemini 風險摘要
# ─────────────────────────────────────────────────────────

def _load_env() -> None:
    """從 .env 讀取環境變數（非必要，若已有環境變數則跳過）。"""
    env_file = Path(__file__).parent.parent / ".env"
    if not env_file.exists():
        return
    with open(env_file, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k = k.strip()
            v = v.strip().strip('"').strip("'")
            if k and not os.environ.get(k):
                os.environ[k] = v
    # google-genai SDK 讀 GOOGLE_API_KEY，但 .env 存的是 GEMINI_API_KEY
    if not os.environ.get("GOOGLE_API_KEY") and os.environ.get("GEMINI_API_KEY"):
        os.environ["GOOGLE_API_KEY"] = os.environ["GEMINI_API_KEY"]


def call_gemini_summary(stats: dict, month: str) -> str:
    """呼叫 Gemini 生成 1 段摘要 + 3~5 條建議。"""
    _load_env()
    try:
        client = genai.Client()
        prompt = f"""你是一位企業稽核顧問。根據以下 {month} 月核銷稽核統計，
用繁體中文寫一份簡短報告：首先是 2~3 句整體摘要，然後列出 3~5 條具體改善建議（每條建議一行，以「1.」「2.」等編號開頭）。
請直接輸出內容，不要加標題或 Markdown。

統計資料（JSON）：
{json.dumps(stats, ensure_ascii=False, indent=2)}
"""
        resp = client.models.generate_content(
            model=MODEL_NAME,
            contents=prompt,
        )
        return resp.text.strip()
    except Exception as e:
        print(f"[!] Gemini 呼叫失敗: {e}")
        return (
            f"本月共處理 {stats['total']} 筆核銷，其中 {stats['rejected']} 筆被駁回，"
            f"高風險案件 {stats['high_risk']} 件。\n\n"
            "1. 建議加強廠商資格審查，定期更新歇業廠商名單。\n"
            "2. 建議對高頻次小額採購設定警示機制，防止拆單規避。\n"
            "3. 建議出差申請需附上行程證明文件，與實際天數比對。\n"
            "（Gemini 呼叫失敗，以上為預設摘要）"
        )


# ─────────────────────────────────────────────────────────
# 主流程
# ─────────────────────────────────────────────────────────

def generate_report(
    log_path: Path,
    month: str | None = None,
    out_dir: Path = _DEFAULT_OUT,
) -> Path:
    """產生 PPTX 報告，回傳輸出路徑。"""
    if month is None:
        month = datetime.now(timezone.utc).strftime("%Y-%m")

    print(f"[REPORT] 讀取 {log_path}（月份過濾: {month}）...")
    entries = load_log(log_path, month)
    if not entries:
        print(f"[REPORT] WARN: {month} 無資料，仍產出空白報告。")

    stats   = build_stats(entries)
    top_cas = top_suspicious(entries)
    gen_time = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    print(f"[REPORT] 共 {stats['total']} 筆，高風險 {stats['high_risk']} 筆。")
    print("[REPORT] 呼叫 Gemini 生成風險摘要...")
    gemini_text = call_gemini_summary(stats, month)

    print("[REPORT] 組裝 PPTX...")
    prs = Presentation()
    prs.slide_width  = SLIDE_W
    prs.slide_height = SLIDE_H

    slide_cover(prs, month, gen_time)
    slide_overview(prs, stats, month)
    slide_flag_stats(prs, stats, month)
    slide_top_cases(prs, top_cas, month)
    slide_risk_summary(prs, stats, month, gemini_text)

    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"audit_report_{month}.pptx"
    prs.save(str(out_path))
    print(f"[REPORT] OK: 報告已儲存: {out_path}")
    return out_path


def main() -> None:
    parser = argparse.ArgumentParser(description="月底核銷稽核報告產生器")
    parser.add_argument("--log",   type=Path, default=_DEFAULT_LOG,
                        help="audit_log.jsonl 路徑")
    parser.add_argument("--month", type=str,  default=None,
                        help="過濾月份 YYYY-MM（預設當月）")
    parser.add_argument("--out",   type=Path, default=_DEFAULT_OUT,
                        help="PPTX 輸出目錄（預設 reports/）")
    args = parser.parse_args()
    generate_report(args.log, args.month, args.out)


if __name__ == "__main__":
    main()
