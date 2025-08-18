import os
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from reportlab.lib.pagesizes import A4
from reportlab.lib import colors
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.enums import TA_CENTER, TA_LEFT
from gemini import generate_with_gemini



def build_gemini_prompt(jtl_data_list):
    prompt = "You are a test analyst. Compare the following KickLoad JTL test summaries. Start each section clearly with 'Test X - [name]':\n\n"
    for idx, data in enumerate(jtl_data_list, 1):
        prompt += f"Test {idx} - {data['label']}\n"
        prompt += f"Samples: {data['samples']}\n"
        prompt += f"Average Response Time: {data['avg_response_time']} ms\n"
        prompt += f"Minimum Response Time: {data['min_response_time']} ms\n"
        prompt += f"Maximum Response Time: {data['max_response_time']} ms\n"
        prompt += f"Errors: {data['error_count']}\n\n"

    prompt += (
        "Please include the following clear section titles (no numbering):\n"
        "- Summary of Each Test\n"
        "- Comparative Analysis\n"
        "- Observations and Bottlenecks\n"
        "- Best Performing Test\n"
        "- Recommendations for Improvement\n\n"
        "Use line breaks between paragraphs. Use bullets (•) for observations and recommendations. Avoid Markdown or HTML."
    )

    return prompt


def clean_output_text(raw_text):
    import re

    # Remove markdown artifacts
    text = raw_text.replace("**", "").replace("*", "").replace("##", "")
    text = re.sub(r"\n{3,}", "\n\n", text.strip())

    # Fix duplicate "Summary of Each Test"
    seen_summary = False
    lines = text.splitlines()
    cleaned_lines = []
    test_label_pattern = re.compile(r'^(Test\s+\d+)\s*-\s*(.+)$')

    for line in lines:
        line = line.strip()
        if not line:
            cleaned_lines.append("")
            continue

        if line.lower() == "summary of each test":
            if seen_summary:
                continue
            seen_summary = True
            cleaned_lines.append("Summary of Each Test")
            continue

        match = test_label_pattern.match(line)
        if match:
            test_num, label = match.groups()
            cleaned_lines.append(f"<b>{test_num}</b> - {label}")
        else:
            cleaned_lines.append(line)

    return "\n".join(cleaned_lines)




def format_label(label):
    return label.replace("_", " ").replace("Thread Group", "").strip()


def generate_comparison_pdf(summary_text, output_path, jtl_data_list, pdf_filename):
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.enums import TA_CENTER, TA_LEFT
    import re

    known_sections = [
        "Summary of Each Test",
        "Comparative Analysis",
        "Observations and Bottlenecks",
        "Best Performing Test",
        "Recommendations for Improvement"
    ]

    doc = SimpleDocTemplate(output_path, pagesize=A4,
                            rightMargin=50, leftMargin=50,
                            topMargin=60, bottomMargin=40, title=pdf_filename)

    styles = getSampleStyleSheet()
    styles.add(ParagraphStyle(name='TitleCustom', fontSize=20, alignment=TA_CENTER,
                              textColor=colors.HexColor("#003366"), spaceAfter=20, fontName="Helvetica-Bold"))
    styles.add(ParagraphStyle(name='SectionHeading', fontSize=16, leading=22, spaceBefore=18,
                              textColor=colors.HexColor("#003366"), fontName="Helvetica-Bold"))
    styles.add(ParagraphStyle(name='BodyCustom', fontSize=12, leading=16, fontName="Helvetica", alignment=TA_LEFT, spaceAfter=8))
    styles.add(ParagraphStyle(name='BoldLine', fontSize=12.5, leading=16, fontName="Helvetica-Bold", alignment=TA_LEFT, spaceAfter=6))
    styles.add(ParagraphStyle(name='BulletItem', fontSize=12, leading=16, fontName="Helvetica", leftIndent=12, bulletIndent=5, spaceAfter=4))
    styles.add(ParagraphStyle(name='TableHeader', fontSize=10, leading=12, fontName="Helvetica-Bold", textColor=colors.HexColor("#003366")))

    elements = [Paragraph("JTL Comparison Report", styles["TitleCustom"]), Spacer(1, 12)]

    lines = summary_text.strip().splitlines()
    if lines and lines[0].strip().lower().startswith("comparison report"):
        lines = lines[1:]
    summary_text = "\n".join(lines)

    # Table
    table_data = [[
    Paragraph("Test Name", styles["TableHeader"]),
    Paragraph("Samples", styles["TableHeader"]),
    Paragraph("Avg Time (ms)", styles["TableHeader"]),
    Paragraph("Min Time", styles["TableHeader"]),
    Paragraph("Max Time", styles["TableHeader"]),
    Paragraph("Errors", styles["TableHeader"]),
    ]]


    for item in jtl_data_list:
        table_data.append([
            Paragraph(format_label(item['label']), styles["BodyCustom"]),
            str(item['samples']), str(item['avg_response_time']), str(item['min_response_time']),
            str(item['max_response_time']), str(item['error_count'])
        ])

    table = Table(table_data, colWidths=[160, 60, 70, 70, 70, 50])
    table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor("#E8F0FE")),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.HexColor("#003366")),
        ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
        ('ALIGN', (1, 1), (-1, -1), 'CENTER'),
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('FONTSIZE', (0, 0), (-1, -1), 10),
        ('GRID', (0, 0), (-1, -1), 0.25, colors.grey),
        ('WORDWRAP', (0, 1), (0, -1), True),
    ]))
    elements.append(table)
    elements.append(Spacer(1, 18))

    current_section = ""
    summary_heading_added = False
    test_lines_buffer = []

    for para in summary_text.split("\n"):
        para = para.strip()
        if not para:
            continue

        matched_section = next((sec for sec in known_sections if para.lower().startswith(sec.lower())), None)
        if matched_section:
            current_section = matched_section.lower()

            elements.append(Paragraph(matched_section, styles["SectionHeading"]))
            elements.append(Spacer(1, 10))

            if current_section == "summary of each test" and test_lines_buffer:
                for line in test_lines_buffer:
                    elements.append(Paragraph(line, styles["BoldLine"]))
                test_lines_buffer.clear()
            continue

        if re.match(r"^Test\s\d+\s-", para):
            # Store for later if before summary
            if current_section != "summary of each test":
                test_lines_buffer.append(para)
                continue
            else:
                elements.append(Paragraph(para, styles["BoldLine"]))
                continue

        if current_section.startswith("observations") or current_section.startswith("recommendations"):
            if para.startswith("-") or para.startswith("•") or re.match(r"^\d+\.\s", para):
                elements.append(Paragraph("• " + para.lstrip("-•0123456789. ").strip(), styles["BulletItem"]))
                continue

        para = re.sub(r'\b(Test\s\d+)\b', r'<b>\1</b>', para)
        elements.append(Paragraph(para, styles["BodyCustom"]))

    doc.build(elements)





def build_html_report_from_plain_text(text, jtl_data_list):
    import re

    known_sections = [
        "Summary of Each Test",
        "Comparative Analysis",
        "Observations and Bottlenecks",
        "Best Performing Test",
        "Recommendations for Improvement"
    ]

    html = ["<h2 style='color:#003366;text-align:center;font-size:22px;font-family:sans-serif;'>JTL Comparison Report</h2>"]

    lines = text.strip().splitlines()
    if lines and lines[0].strip().lower().startswith("comparison report"):
        lines = lines[1:]
    text = "\n".join(lines)

    # Table
    html.append("""<table style='width:100%;border-collapse:collapse;margin:16px 0;font-size:14px;font-family:sans-serif;'>
      <thead style='background:#E8F0FE;color:#003366;'>
        <tr>
          <th style='padding:8px;border:1px solid #ccc;text-align:left;'>Test Name</th>
          <th style='padding:8px;border:1px solid #ccc;'>Samples</th>
          <th style='padding:8px;border:1px solid #ccc;'>Avg Time (ms)</th>
          <th style='padding:8px;border:1px solid #ccc;'>Min Time</th>
          <th style='padding:8px;border:1px solid #ccc;'>Max Time</th>
          <th style='padding:8px;border:1px solid #ccc;'>Errors</th>
        </tr>
      </thead>
      <tbody>""")

    for item in jtl_data_list:
        html.append(f"""
        <tr>
          <td style='padding:8px;border:1px solid #ccc;'>{format_label(item['label'])}</td>
          <td style='padding:8px;border:1px solid #ccc;'>{item['samples']}</td>
          <td style='padding:8px;border:1px solid #ccc;'>{item['avg_response_time']}</td>
          <td style='padding:8px;border:1px solid #ccc;'>{item['min_response_time']}</td>
          <td style='padding:8px;border:1px solid #ccc;'>{item['max_response_time']}</td>
          <td style='padding:8px;border:1px solid #ccc;'>{item['error_count']}</td>
        </tr>""")
    html.append("</tbody></table>")

    # Enforce section heading manually
    html.append("<h3 style='color:#003366;font-size:18px;font-weight:600;margin-top:24px;margin-bottom:12px;font-family:sans-serif'>Summary of Each Test</h3>")
    summary_heading_added = True

    current_section = ""
    test_lines_buffer = []

    for line in text.strip().splitlines():
        line = line.strip()
        if not line:
            continue

        matched_section = next((sec for sec in known_sections if line.lower().startswith(sec.lower())), None)
        if matched_section:
            if matched_section.lower() == "summary of each test" and summary_heading_added:
                continue

            current_section = matched_section.lower()
            html.append(f"<h3 style='color:#003366;font-size:18px;font-weight:600;margin-top:24px;margin-bottom:12px;font-family:sans-serif'>{matched_section}</h3>")
            if current_section == "summary of each test":
                summary_heading_added = True
            continue

        if re.match(r"^Test\s\d+\s-", line):
            html.append(f"<p style='margin:12px 0 6px;font-weight:600;font-size:15px;font-family:sans-serif'>{line}</p>")
            continue

        if current_section.startswith("observations") or current_section.startswith("recommendations"):
            if line.startswith("-") or line.startswith("•") or re.match(r"^\d+\.\s", line):
                html.append(f"<p style='margin-left:14px;margin-bottom:6px;font-family:sans-serif'>• {line.lstrip('-•0123456789. ').strip()}</p>")
                continue

        line = re.sub(r'\b(Test\s\d+)\b', r'<b>\1</b>', line)
        html.append(f"<p style='margin:6px 0 12px;font-family:sans-serif'>{line}</p>")

    return "<div style='font-size:14px;color:#333;padding:16px;font-family:sans-serif;'>" + "\n".join(html) + "</div>"

