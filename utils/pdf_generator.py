import os
import tempfile
from reportlab.platypus import (
    SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer, Image, Flowable, PageBreak, BaseDocTemplate, Frame, PageTemplate
)
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.pagesizes import A3, landscape
from reportlab.pdfbase.pdfmetrics import stringWidth

from reportlab.lib import colors
import matplotlib.pyplot as plt
import matplotlib
matplotlib.use("Agg")
import requests

# Keep these aligned with the frontend `RunTestPage.jsx`
GRAFANA_SYSTEM_DASHBOARD_UID = "kickload-system"
GRAFANA_SYSTEM_DASHBOARD_SLUG = "kickload-system-metrics"
GRAFANA_SYSTEM_PANELS = [
    {"id": 1, "title": "CPU Usage"},
    {"id": 2, "title": "Memory Used"},
    {"id": 3, "title": "Total Processes"},
    {"id": 4, "title": "Disk Usage"},
    {"id": 5, "title": "Network Throughput"},
]


def _build_grafana_render_url(
    base_url: str,
    dashboard_uid: str,
    dashboard_slug: str,
    panel_id: int,
    from_ms: int,
    to_ms: int,
    org_id: str = "1",
    theme: str = "light",
    width: int = 1200,
    height: int = 500,
):
    # Grafana render endpoint requires image renderer to be configured.
    # Works with base URLs that may already include subpath (e.g. /grafana).
    base = (base_url or "").rstrip("/")
    params = {
        "orgId": str(org_id),
        "panelId": str(panel_id),
        "from": str(int(from_ms)),
        "to": str(int(to_ms)),
        "theme": theme,
        "width": str(int(width)),
        "height": str(int(height)),
        "tz": "UTC",
        "kiosk": "tv",
    }
    query = "&".join([f"{k}={requests.utils.quote(str(v), safe='')}" for k, v in params.items()])
    return f"{base}/render/d-solo/{dashboard_uid}/{dashboard_slug}?{query}"


def _download_grafana_panel_png(url: str, out_path: str, token: str | None = None, timeout_s: int = 20) -> bool:
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    try:
        resp = requests.get(url, headers=headers, timeout=timeout_s)
        if resp.status_code != 200:
            return False
        content_type = resp.headers.get("Content-Type", "")
        if "image/png" not in content_type.lower():
            return False
        with open(out_path, "wb") as f:
            f.write(resp.content)
        return True
    except Exception:
        return False


def _append_grafana_panels_section(
    elements,
    sub_title_style,
    *,
    from_ms: int,
    to_ms: int,
    grafana_base_url: str,
    grafana_token: str | None,
):
    if not grafana_base_url or not from_ms or not to_ms or from_ms >= to_ms:
        return

    # Attempt to render/download each panel; if nothing downloads, omit the section.
    downloaded = []
    temp_paths = []

    for panel in GRAFANA_SYSTEM_PANELS:
        tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
        tmp.close()
        temp_paths.append(tmp.name)

        url = _build_grafana_render_url(
            grafana_base_url,
            GRAFANA_SYSTEM_DASHBOARD_UID,
            GRAFANA_SYSTEM_DASHBOARD_SLUG,
            panel_id=panel["id"],
            from_ms=from_ms,
            to_ms=to_ms,
        )

        ok = _download_grafana_panel_png(url, tmp.name, token=grafana_token)
        if ok:
            downloaded.append((panel, tmp.name))

    if not downloaded:
        # Cleanup and skip
        for p in temp_paths:
            try:
                if os.path.exists(p):
                    os.remove(p)
            except Exception:
                pass
        return

    elements.append(Spacer(1, 30))
    elements.append(Paragraph("Infrastructure Metrics (Grafana)", sub_title_style))
    elements.append(Spacer(1, 10))

    # Layout: 2-column grid. Each cell is a vertical stack: title (row 1) + image (row 2).
    panel_title_style = ParagraphStyle(
        name="GrafanaPanelTitle",
        fontSize=12,
        textColor=colors.HexColor("#333333"),
        spaceAfter=6,
    )

    def panel_cell(panel_title: str, img_path: str):
        title = Paragraph(panel_title, panel_title_style)
        img = Image(img_path, width=420, height=210)
        cell_table = Table(
            [[title], [img]],
            colWidths=[420],
            hAlign="CENTER",
        )
        cell_table.setStyle(TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LEFTPADDING", (0, 0), (-1, -1), 0),
            ("RIGHTPADDING", (0, 0), (-1, -1), 0),
            ("TOPPADDING", (0, 0), (-1, -1), 0),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ]))
        return cell_table

    grid_rows = []
    i = 0
    while i < len(downloaded):
        left_panel, left_img = downloaded[i]
        left_cell = panel_cell(left_panel["title"], left_img)

        right_cell = Spacer(1, 1)
        if i + 1 < len(downloaded):
            right_panel, right_img = downloaded[i + 1]
            right_cell = panel_cell(right_panel["title"], right_img)

        grid_rows.append([left_cell, right_cell])
        i += 2

    t = Table(grid_rows, colWidths=[440, 440], hAlign="CENTER")
    t.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 8),
        ("RIGHTPADDING", (0, 0), (-1, -1), 8),
        ("TOPPADDING", (0, 0), (-1, -1), 8),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 12),
    ]))
    elements.append(t)

    # Return paths so caller can clean up
    return [p for _, p in downloaded]

class ChartImage(Flowable):
    def __init__(self, path, width=250, height=160):
        Flowable.__init__(self)
        self.path = path
        self.width = width
        self.height = height

    def draw(self):
        img = Image(self.path, width=self.width, height=self.height)
        img.wrapOn(self.canv, self.width, self.height)
        img.drawOn(self.canv, 0, 0)

FIELD_TITLES = {
    'samples': 'Requests',
    'average_ms': 'AvgResponse (ms)',
    'min_ms': 'MinResponse (ms)',
    'max_ms': 'MaxResponse (ms)',
    'stddev_ms': 'Stddev (ms)',
    'error_pct': 'ErrorRate (%)',
    'throughput_rps': 'Throughput (RPS)',
    'received_kbps': 'Received Kbps (KBps)',
    'sent_kbps': 'Sent Kbps (KBps)',
    'avg_bytes': 'Avg Bytes (bytes)',
}

DESIRED_KEYS = list(FIELD_TITLES.keys())


def get_performance_badge(row):
    error_pct = row.get("error_pct", 0)
    avg = row.get("average_ms", 0)

    if error_pct > 50:
        return "Critical", colors.HexColor("#F44336")
    elif error_pct > 10:
        return "Warning", colors.HexColor("#FF9800")
    elif avg > 1000:
        return "Slow", colors.HexColor("#FFC107")
    else:
        return "Good", colors.HexColor("#4CAF50")



class PerformanceBadgeFlowable(Flowable):
    def __init__(self, label, color, padding=4, height=18, radius=6):
        super().__init__()
        self.label = label
        self.color = color
        self.padding = padding
        self.height = height
        self.radius = radius

        # Dynamically calculate width based on label and font
        font_name = "Helvetica-Bold"
        font_size = 8
        text_width = stringWidth(label, font_name, font_size)
        self.width = text_width + 2 * self.padding

    def wrap(self, availWidth, availHeight):
        return (self.width, self.height)

    def draw(self):
        self.canv.setStrokeColor(self.color)  # Optional, not needed unless keeping border
        self.canv.setLineWidth(0)  # 👈 Ensures no border
        self.canv.setFillColor(self.color)
        self.canv.roundRect(0, 0, self.width, self.height, radius=self.radius, fill=1, stroke=0)  # 👈 stroke=0 disables border
        self.canv.setFillColor(colors.white)
        self.canv.setFont("Helvetica-Bold", 8)
        self.canv.drawCentredString(self.width / 2, self.height / 2 - 3, self.label)




def generate_chart(summary_data, field, output_path, color):
    # Instead of full label list
    # labels = [row.get('label', f'Row {i}') for i, row in enumerate(summary_data)]
    labels = [str(i+1) for i in range(len(summary_data))]  # ✅ Numbers only

    values = [row.get(field, 0) for row in summary_data]

    plt.figure(figsize=(8, 10))
    bars = plt.bar(labels, values, color=color, edgecolor='black')

    max_val = max(values) if values else 0
    for bar in bars:
        height = bar.get_height()
        plt.text(
            bar.get_x() + bar.get_width() / 2.0,
            height + 0.015 * max_val,
            f'{height:.1f}',
            ha='center', va='bottom',
            fontsize=13, color='black'
        )

    plt.title(FIELD_TITLES.get(field, field), fontsize=16, pad=20)
    plt.xticks(rotation=0, fontsize=12)  # These will now just be numbers
    plt.yticks(fontsize=12)
    plt.ylim(bottom=0)
    plt.grid(axis='y', linestyle='--', linewidth=0.5, alpha=0.6)

    plt.savefig(output_path, dpi=250)
    plt.close()





def set_pdf_title(title):
    def on_page(canvas, doc):
        canvas.setTitle(title)
    return on_page


def generate_pdf_report(
    summary_data,
    output_pdf_path,
    title,
    *,
    grafana_from_ms: int | None = None,
    grafana_to_ms: int | None = None,
):

    import os

    # Ensure output directory exists
    os.makedirs(os.path.dirname(output_pdf_path), exist_ok=True)

    styles = getSampleStyleSheet()
    elements = []
    temp_images = []

    title_style = styles["Heading1"]
    title_style.alignment = 1
    title_style.textColor = colors.HexColor("#FF6D00")
    elements.append(Paragraph("Test Results", title_style))
    elements.append(Spacer(1, 20))

    # Adjust left-indent to align with table start (~30 left margin + padding)
    sub_title_style = ParagraphStyle(
        'subTitle',
        parent=styles['Heading2'],
        textColor=colors.HexColor("#FF6D00"),
        fontSize=14,
        leftIndent=100,  # ⬅️ Increased for better alignment
        spaceAfter=12
    )


    elements.append(Paragraph("Test Summary", sub_title_style))
    elements.append(Spacer(1, 10))

    def wrap_header(text):
        return Paragraph(text.replace(" ", "&nbsp;").replace("(", "<br/>("), ParagraphStyle(
            name='HeaderStyle',
            fontSize=11,
            alignment=1,
            leading=12
        ))

    table_headers = [wrap_header("Label")] + [
        wrap_header(FIELD_TITLES[k]) for k in DESIRED_KEYS
    ] + [wrap_header("Performance Status")]

    data_rows = [table_headers]

    for row in summary_data:
        badge_label, badge_color = get_performance_badge(row)
        badge_paragraph = PerformanceBadgeFlowable(badge_label, badge_color)
        label_para = Paragraph(row.get("label", ""), ParagraphStyle(name='RowLabel', fontSize=10))
        data_rows.append(
            [label_para] + [Paragraph(str(row.get(k, "")), ParagraphStyle(name='RowVal', fontSize=10)) for k in DESIRED_KEYS] + [badge_paragraph]
        )

    col_widths = [100] + [70]*len(DESIRED_KEYS) + [100]
    table = Table(data_rows, repeatRows=1, colWidths=col_widths, hAlign='CENTER')
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#FF6D00")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.whitesmoke),
        ("ALIGN", (0, 0), (-1, -1), "CENTER"),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 11),  # Increased all font sizes
        ("BOTTOMPADDING", (0, 0), (-1, 0), 10),
        ("TOPPADDING", (0, 1), (-1, -1), 6),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.whitesmoke, colors.HexColor("#FFF8F1")]),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),


    ]))
    elements.append(table)
    elements.append(Spacer(1, 40))

    # ✅ After Summary Table and before Performance Analytics
    elements.append(Spacer(1, 20))


    # --- Label Mapping Section ---
    label_map_title = Paragraph(
        "Label Mapping",
        sub_title_style  # ✅ Use same style, so leftIndent matches
    )
    elements.append(label_map_title)

    mapping_data = [["#", "Label"]]  # table headers
    for idx, row in enumerate(summary_data, start=1):
        mapping_data.append([str(idx), row.get("label", "")])

    mapping_table = Table(
        mapping_data,
        colWidths=[30, 400],
        hAlign='CENTER'  # keep table aligned left, but will follow heading indent visually
    )
    mapping_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#FF6D00")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.whitesmoke),
        ("ALIGN", (0, 0), (0, -1), "CENTER"),
        ("ALIGN", (1, 0), (1, -1), "LEFT"),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 10),
        ("BOTTOMPADDING", (0, 0), (-1, 0), 6),
        ("TOPPADDING", (0, 1), (-1, -1), 4),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.whitesmoke, colors.HexColor("#FFF8F1")]),
        ("GRID", (0, 0), (-1, -1), 0.25, colors.grey),
    ]))
    # ✅ Wrap table in a list with a Spacer if needed to keep consistent layout
    elements.append(mapping_table)
    elements.append(Spacer(1, 30))



    elements.append(Paragraph("Performance Analytics", sub_title_style))
    elements.append(Spacer(1, 10))

    colors_list = [
        '#FF6D00', '#2196F3', '#4CAF50', '#F44336', '#FFC107',
        '#9C27B0', '#009688', '#FF9800', '#607D8B', '#E91E63'
    ]
    chart_flowables = []
    for i, key in enumerate(DESIRED_KEYS):
        tmp_chart = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
        tmp_chart.close()
        generate_chart(summary_data, key, tmp_chart.name, color=colors_list[i % len(colors_list)])
        chart_flowables.append(ChartImage(tmp_chart.name, width=420, height=300))  # ⬅️ Larger chart image
        temp_images.append(tmp_chart.name)

    for i in range(0, len(chart_flowables), 2):
        row = []
        row = [chart_flowables[i]]
        if i + 1 < len(chart_flowables):
            row.append(chart_flowables[i + 1])

        t = Table([row], hAlign='CENTER')

        t.setStyle(TableStyle([
            ("ALIGN", (0, 0), (-1, -1), "CENTER"),
            ("LEFTPADDING", (0, 0), (-1, -1), 2),
            ("RIGHTPADDING", (0, 0), (-1, -1), 2),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 10),  # ⬅️ Add bottom padding
            ("TOPPADDING", (0, 0), (-1, -1), 10),     # ⬅️ Add top padding
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE")    # ⬅️ Ensure centered vertically
        ]))
        elements.append(t)
        elements.append(Spacer(1, 16))  # ⬅️ more space between chart rows


    # --- Grafana panels (optional) ---
    # Config:
    # - `GRAFANA_RENDER_BASE_URL` should point to Grafana base (may include subpath, e.g. http://grafana:3000/grafana)
    # - `GRAFANA_API_TOKEN` (optional) if anonymous access is disabled
    grafana_base_url = os.getenv("GRAFANA_RENDER_BASE_URL") or os.getenv("GRAFANA_BASE_URL") or ""
    grafana_token = os.getenv("GRAFANA_API_TOKEN") or None

    try:
        grafana_temp = _append_grafana_panels_section(
            elements,
            sub_title_style,
            from_ms=int(grafana_from_ms) if grafana_from_ms else 0,
            to_ms=int(grafana_to_ms) if grafana_to_ms else 0,
            grafana_base_url=grafana_base_url,
            grafana_token=grafana_token,
        )
        if grafana_temp:
            temp_images.extend(grafana_temp)
    except Exception:
        # Never fail PDF generation due to Grafana rendering issues
        pass

    # Estimate total height
    dummy_doc = BaseDocTemplate(
        output_pdf_path,
        pagesize=(landscape(A3)[0], 10000),
        leftMargin=30, rightMargin=30, topMargin=30, bottomMargin=30
    )
    dummy_frame = Frame(dummy_doc.leftMargin, dummy_doc.bottomMargin, dummy_doc.width, dummy_doc.height)
    dummy_doc.addPageTemplates([PageTemplate(id='Temp', frames=[dummy_frame])])
    total_height = sum(e.wrap(dummy_doc.width, 1000)[1] for e in elements) + 200



    doc = BaseDocTemplate(
        output_pdf_path,
        pagesize=(landscape(A3)[0], total_height),
        leftMargin=30, rightMargin=30, topMargin=30, bottomMargin=5
    )

    frame = Frame(doc.leftMargin, doc.bottomMargin, doc.width, doc.height, id='normal')
    doc.addPageTemplates([
        PageTemplate(id='OnePage', frames=[frame], onPage=set_pdf_title(title))
    ])

    doc.build(elements)

    for path in temp_images:
        if os.path.exists(path):
            os.remove(path)

