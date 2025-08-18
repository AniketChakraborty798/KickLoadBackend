import pandas as pd
import json
import os
import logging
import xml.etree.ElementTree as ET
from tasks.tasks import generate_gemini_analysis_async
from datetime import datetime, timezone
from reportlab.lib import colors
from reportlab.lib.pagesizes import A3
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, PageBreak, HRFlowable, ListFlowable, Table, TableStyle
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch
import time
import re
from jinja2 import Template

# Logging setup
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[logging.FileHandler('jmeter.log'), logging.StreamHandler()]
)
logger = logging.getLogger(__name__)


def build_html_report(text, summary_df):
    lines = text.strip().splitlines()
    html_sections = []
    current_section = ""
    endpoint_buffer = []
    summary_table_rendered = False

    for line in lines:
        line = line.strip()
        match = re.match(
            r"^(KickLoad Performance Test Results Analysis|Summary|Overall Summary|Detailed Analysis by Endpoint|Endpoint-wise Performance Analysis|Bottlenecks and Issues|Suggestions and Next Steps)[:]*$",
            line, re.IGNORECASE
        )
        if match:
            if endpoint_buffer:
                html_sections.append(render_endpoint_html_block(endpoint_buffer))
                endpoint_buffer = []
            current_section = match.group(1)
            html_sections.append(f"<h3 style='color:#003366;margin-top:24px;margin-bottom:12px'>{current_section}</h3>")
            continue

        if current_section in ["Detailed Analysis by Endpoint", "Endpoint-wise Performance Analysis"]:
            endpoint_buffer.append(line)
            continue

        if current_section in ["Summary", "Overall Summary"]:
            if line:
                html_sections.append(f"<p style='margin-bottom:10px'>{line}</p>")

            if not summary_table_rendered and summary_df is not None and not summary_df.empty:
                # Define table HTML as a Jinja template (no f-string)
                summary_table_template = """
                <div style='max-width:100%; overflow-x:auto;'>
                    <table style='border-collapse:collapse;table-layout:auto;min-width:1000px;width:auto;font-size:13px;' border='0'>
                        <thead style='background-color:#E0ECF8;color:#003366;text-align:left'>
                            <tr>
                                {% for col in columns %}
                                <th style='padding:8px;border:1px solid #ccc;white-space:nowrap;'>{{ col }}</th>
                                {% endfor %}
                            </tr>
                        </thead>
                        <tbody>
                            {% for row in data %}
                            <tr>
                                {% for val in row %}
                                <td style='padding:8px;border:1px solid #ccc;white-space:normal;word-break:break-word;'>{{ val }}</td>
                                {% endfor %}
                            </tr>
                            {% endfor %}
                        </tbody>
                    </table>
                </div>
                """
                template = Template(summary_table_template)
                columns = summary_df.columns.tolist()
                data = summary_df.values.tolist()
                rendered_table = template.render(columns=columns, data=data)
                html_sections.append(rendered_table)
                summary_table_rendered = True

            continue

        if current_section in ["Bottlenecks and Issues", "Suggestions and Next Steps"]:
            if re.match(r"^\d+\.\s+", line):
                html_sections.append(f"<p style='margin-left:14px;margin-bottom:6px;text-indent:-10px;'>{line}</p>")
            elif re.match(r"^[\*\•\-]\s+.*", line):
                clean_text = line.lstrip('-*• ').strip()
                html_sections.append(f"<p style='margin-left:14px;margin-bottom:6px;'>• {clean_text}</p>")
            else:
                html_sections.append(f"<p style='margin-bottom:8px'>{line}</p>")
            continue

        if current_section is None and line:
            html_sections.append(f"<p style='margin-bottom:10px'>{line}</p>")

    if endpoint_buffer:
        html_sections.append(render_endpoint_html_block(endpoint_buffer))

    final_html = "<div style='font-family:Helvetica,Arial,sans-serif;font-size:14px;color:#333;margin:16px;'>"
    final_html += "\n".join(html_sections)
    final_html += "</div>"

    return final_html




def render_endpoint_html_block(lines):
    html = ""
    current_title = ""
    analysis_line = ""

    for line in lines:
        line = line.strip()
        # Detect endpoint title
        if re.match(r"^[\*\•\-]\s+.+[:]{1}$", line):
            # Flush previous block
            if current_title and analysis_line:
                html += f"<h4 style='margin-top:16px;margin-bottom:6px'>{current_title}</h4>"
                html += f"<p style='margin-bottom:12px'>{analysis_line}</p>"
                analysis_line = ""

            current_title = line.lstrip('-*• ').strip().rstrip(":")
        elif "Analysis:" in line:
            analysis_line = line.split("Analysis:", 1)[-1].strip()

    # Final flush
    if current_title and analysis_line:
        html += f"<h4 style='margin-top:16px;margin-bottom:6px'>{current_title}</h4>"
        html += f"<p style='margin-bottom:12px'>{analysis_line}</p>"

    return html





def clean_ai_text(raw):
    # Unescape common markdown bold and italic
    clean = re.sub(r"\*\*(.*?)\*\*", r"\1", raw)
    clean = re.sub(r"\*(.*?)\*", r"\1", clean)
    # Fix numeric list issues with excess spaces/lines
    clean = re.sub(r"\b(\d)\s+(\d)\.", r"\1.\2.", clean)
    clean = re.sub(r"(?:\n|^)1\s+1\. Executive Summary", r"\g<0>1. Executive Summary", clean)
    clean = re.sub(r"^\s*[-•*]\s+", "- ", clean, flags=re.MULTILINE)
    clean = re.sub(r"^\s*(\d+)[\.\)]\s+", r"\1. ", clean, flags=re.MULTILINE)
    # Remove exact repeated lines
    clean = re.sub(r"(Name: .+?Analysis: .+?)(\n\1)+", r"\1", clean, flags=re.DOTALL)
    # Deduplicate lines ignoring case & whitespace
    lines = clean.strip().splitlines()
    seen = set()
    deduped = []
    for line in lines:
        norm = line.strip().lower()
        if norm not in seen:
            seen.add(norm)
            deduped.append(line)
    # Normalize excessive blank line sequences to max 2 newlines
    return re.sub(r"\n{3,}", "\n\n", "\n".join(deduped)).strip()



def add_footer(canvas_obj, doc):
    canvas_obj.saveState()
    footer_text = f"KickLoad Performance Report | Page {doc.page}"
    canvas_obj.setFont("Helvetica", 9)
    canvas_obj.setFillColor(colors.grey)
    canvas_obj.drawCentredString(A3[0] / 2.0, 0.5 * inch, footer_text)
    canvas_obj.restoreState()

def render_endpoint_analysis(lines, styles):
    elements = []
    current_title = ""
    buffer = []

    for line in lines:
        line = line.strip()

        # Detect start of an endpoint block like "- Create Order:"
        if re.match(r"^[\*\•\-]\s+.+[:]{1}$", line):  # allow full endpoint path

            if buffer and current_title:
                logger.info(f"🧩 Rendering endpoint block for: {current_title}")
                elements.extend(render_endpoint_block(current_title, buffer, styles))
                buffer = []
            current_title = re.sub(r"^[\*\•\-]\s+", "", line).strip().rstrip(":")
        else:
            buffer.append(line)

    # Final block flush
    if current_title and buffer:
        logger.info(f"🧩 Rendering final endpoint block for: {current_title}")
        elements.extend(render_endpoint_block(current_title, buffer, styles))
    else:
        if not current_title and buffer:
            logger.warning("⚠️ Skipped endpoint block due to missing title.")
        elif not buffer:
            logger.warning("⚠️ Skipped endpoint block due to missing content.")

    if not elements:
        logger.warning("❌ No endpoint analysis blocks were rendered. Ensure the AI output uses format like:")
        logger.warning('- Create Order:\n    - Avg Time: 123ms\n    - Errors: 0.0%\n    - Throughput: 20\n    - Users: 20\n    - Analysis: ...')

    return elements




def render_endpoint_block(title, block_lines, styles):
    elements = []
    for line in block_lines:
        if "Analysis:" in line:
            analysis = line.split("Analysis:", 1)[-1].strip()
            elements.append(Paragraph(title, styles['BoldLabel']))  # Show endpoint
            elements.append(Spacer(1, 0.05 * inch))
            elements.append(Paragraph(analysis, styles['BodyTextCustom']))
            elements.append(Spacer(1, 0.2 * inch))
            break
    return elements




def build_professional_pdf(text, output_path, summary_filtered, metrics_source, title=None):
    doc = SimpleDocTemplate(output_path, pagesize=A3,
                            rightMargin=50, leftMargin=50,
                            topMargin=72, bottomMargin=50, title=title)

    styles = getSampleStyleSheet()
    styles.add(ParagraphStyle(name='CoverTitle', fontSize=26, alignment=1,
                              spaceAfter=12, spaceBefore=12,
                              fontName="Helvetica-Bold", textColor=colors.HexColor("#003366")))
    styles.add(ParagraphStyle(name='SubTitleRight', fontSize=11.5, alignment=2,
                              fontName="Helvetica", textColor=colors.grey,
                              spaceAfter=20))
    styles.add(ParagraphStyle(name='SectionHeading', fontSize=16, leading=22,
                              spaceBefore=28, spaceAfter=14,
                              fontName="Helvetica-Bold", textColor=colors.HexColor("#003366")))
    styles.add(ParagraphStyle(name='BodyTextCustom', fontSize=11.5, leading=18,
                              spaceAfter=8, fontName="Helvetica"))
    styles.add(ParagraphStyle(name='BulletItem', fontSize=11.5, leading=18,
                              leftIndent=15, bulletIndent=8, fontName="Helvetica"))
    styles.add(ParagraphStyle(name='NumberedItem', fontSize=11.5, leading=18,
                              leftIndent=15, bulletIndent=8, fontName="Helvetica"))
    styles.add(ParagraphStyle(name='BoldLabel', fontSize=12, leading=18,
                              spaceAfter=4, fontName="Helvetica-Bold"))

    elements = []

    # Cover
    elements.append(Spacer(1, 2 * inch))
    elements.append(Paragraph("KickLoad Performance", styles['CoverTitle']))
    elements.append(Paragraph("Analysis Report", styles['CoverTitle']))
    elements.append(Spacer(1, 0.3 * inch))

    date_str = datetime.now().strftime("%B %d, %Y")
    date_table = Table([[Paragraph("", styles['BodyTextCustom']), Paragraph(date_str, styles['SubTitleRight'])]],
                       colWidths=[doc.width * 0.6, doc.width * 0.4])
    date_table.setStyle(TableStyle([('ALIGN', (1, 0), (1, 0), 'RIGHT')]))
    elements.append(date_table)
    elements.append(HRFlowable(width="100%", thickness=1, color=colors.grey))
    elements.append(PageBreak())

    current_section = None
    endpoint_buffer = []
    lines = text.strip().splitlines()
    summary_rendered = False

    for line in lines:
        line = line.strip().replace("##", "")

        # Section Headings
        match = re.match(r"^(KickLoad Performance Test Results Analysis|Summary|Overall Summary|Detailed Analysis by Endpoint|Endpoint-wise Performance Analysis|Bottlenecks and Issues|Suggestions and Next Steps)[:]*$", line, re.IGNORECASE)
        if match:
            if endpoint_buffer:
                elements.extend(render_endpoint_analysis(endpoint_buffer, styles))
                endpoint_buffer = []
            current_section = match.group(1).strip()
            elements.append(Paragraph(current_section, styles['SectionHeading']))
            continue

        # Endpoint block
        if current_section in ["Detailed Analysis by Endpoint", "Endpoint-wise Performance Analysis"]:
            if re.match(r"^[\*\•\-]\s+.*", line):
                endpoint_buffer.append(line)
            continue

        # Summary + Table
        if current_section in ["Summary", "Overall Summary"]:
            logger.info(f"📄 Processing Summary section...")

            if line:
                elements.append(Paragraph(line, styles['BodyTextCustom']))

            if not summary_rendered and not metrics_source.empty:
                logger.info(f"📋 Table is being rendered with {len(metrics_source)} rows")

                elements.append(Spacer(1, 0.3 * inch))
                columns = [
                    "label", "samples", "average_ms", "min_ms", "max_ms",
                    "stddev_ms", "error_pct", "throughput_rps", "received_kbps",
                    "sent_kbps", "avg_bytes"
                ]

                headers = {
                    "label": "Endpoint",
                    "samples": "Samples",
                    "average_ms": "Avg (ms)",
                    "min_ms": "Min (ms)",
                    "max_ms": "Max (ms)",
                    "stddev_ms": "Std Dev (ms)",
                    "error_pct": "Error (%)",
                    "throughput_rps": "Throughput (req/s)",
                    "received_kbps": "Received (KB/s)",
                    "sent_kbps": "Sent (KB/s)",
                    "avg_bytes": "Avg Bytes"
                }

                styles.add(ParagraphStyle(name='HeaderTextSmall', fontSize=9.5, leading=12, fontName="Helvetica-Bold"))

                table_data = [
                    [Paragraph(headers[col], styles['HeaderTextSmall']) for col in columns]
                ]

                logger.info("📊 Table Columns: %s", columns)

                for i, row in metrics_source.iterrows():
                    row_data = {col: row[col] for col in columns}
                    logger.info("📋 Row %d: %s", i, row_data)

                    table_data.append([
                        Paragraph(str(row["label"]), styles['BodyTextCustom'])
                    ] + [str(row[col]) for col in columns[1:]])

                col_widths = [
                    150, 55, 55, 55, 55, 60, 55, 70, 70, 55, 70
                ]

                t = Table(table_data, colWidths=col_widths, repeatRows=1)
                t.setStyle(TableStyle([
                    ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor("#E0ECF8")),
                    ('TEXTCOLOR', (0, 0), (-1, 0), colors.HexColor("#003366")),
                    ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
                    ('FONTSIZE', (0, 1), (-1, -1), 9),
                    ('ALIGN', (1, 1), (-1, -1), 'CENTER'),
                    ('ALIGN', (0, 0), (0, -1), 'LEFT'),
                    ('VALIGN', (0, 0), (-1, -1), 'TOP'),
                    ('GRID', (0, 0), (-1, -1), 0.25, colors.grey),
                    ('SPLITROWS', (0, 0), (-1, -1), 1),
                ]))
                elements.append(t)
                summary_rendered = True

            continue

        if current_section in ["Bottlenecks and Issues", "Suggestions and Next Steps"]:
            if re.match(r"^\d+\.\s+", line):
                elements.append(Paragraph(line, styles['NumberedItem']))
            elif re.match(r"^[\*\•\-]\s+.*", line):
                elements.append(Paragraph("• " + line.lstrip("-*• ").strip(), styles['BulletItem']))
            else:
                elements.append(Paragraph(line, styles['BodyTextCustom']))
            continue

        if current_section is None and line:
            elements.append(Paragraph(line, styles['BodyTextCustom']))

    if endpoint_buffer:
        elements.extend(render_endpoint_analysis(endpoint_buffer, styles))

    doc.build(elements, onFirstPage=add_footer, onLaterPages=add_footer)




def analyze_jtl_to_pdf(file_path, output_folder):
    from jmeter_core import parse_jtl_summary
    try:
        logger.info(f"📊 Starting PDF analysis for {file_path}")
        raw_summary = parse_jtl_summary(file_path)
        df = pd.DataFrame(raw_summary)

        if df is None or df.empty:
            return {"error": "Parsed JTL has no results. Possibly the test never ran."}

        # Filter out static resources (noise)
        df = df[~df["label"].str.contains(r"\.(?:css|js|png|jpg|gif|ico|svg)$", case=False, na=False)]


        required = {"label", "average_ms", "min_ms", "max_ms", "stddev_ms", "error_pct"}
        if not required.issubset(df.columns):
            return {"error": f"Missing required summary columns: {required - set(df.columns)}"}

        # === CASE 1: Diagnostic ===
        if df.shape[0] < 2 or df["samples"].sum() < 5:
            logger.warning("⚠️ Insufficient meaningful data. Switching to diagnostic mode.")

            diagnostic_text = (
                "KickLoad Performance Test Results Analysis\n\n"
                "Summary:\n\n"
                "The test executed but produced very limited results (fewer than 5 requests or only one meaningful endpoint).\n"
                "This usually means the test plan (JMX) is not properly configured to generate sustained load.\n\n"
                "Possible Reasons:\n"
                "* Very low Thread Group settings (e.g. 1 thread, 1 loop).\n"
                "* Test duration too short.\n"
                "* Only static resources (CSS/JS/images) were recorded instead of real transactions.\n\n"
                "Suggestions and Next Steps:\n"
                "1. Increase Thread Group threads and loop count.\n"
                "2. Add Timers and realistic scenarios.\n"
                "3. Validate that important business transactions (login, register, checkout) are included.\n"
            )

            # Save diagnostic only
            os.makedirs(output_folder, exist_ok=True)
            ts = datetime.now(timezone.utc).strftime("%d-%m-%Y_%H-%M-%S")
            filename = f"analysis_{ts}.pdf"
            output_path = os.path.join(output_folder, filename)

            build_professional_pdf(diagnostic_text, output_path, df, df, title=filename)
            html_preview = build_html_report(diagnostic_text, df)
            return {"message": "Diagnostic PDF generated (insufficient data).", "filename": filename, "html_report": html_preview}

        # === CASE 2: Full Analysis ===
        summary = df
        summary_filtered = summary[(summary["error_pct"] > 0) | (summary["average_ms"] > 2000)]
        metrics_source = summary_filtered if not summary_filtered.empty else summary

        # Build test metrics sentence for AI
        summary_text = "\n".join([
            f"- {row['label']}: Avg Time={row['average_ms']} ms, Min={row['min_ms']} ms, "
            f"Max={row['max_ms']} ms, StdDev={row['stddev_ms']} ms, Errors={row['error_pct']}%, "
            f"Throughput={row['throughput_rps']} tps, KB/s: Rx={row['received_kbps']}, Tx={row['sent_kbps']}"
            for _, row in metrics_source.iterrows()
        ])

        prompt = (
            "You are a performance engineering expert. Generate a professional test analysis.\n\n"
            f"Test Summary:\n{summary_text}\n\n"
            "Follow this exact structure:\n\n"
            "KickLoad Performance Test Results Analysis\n\n"
            "Summary:\n\n"
            "<Overall findings>\n\n"
            "Detailed Analysis by Endpoint:\n\n"
            "* <Endpoint Name>:\n"
            "   * Avg Time: X ms\n"
            "   * Min Time: X ms\n"
            "   * Max Time: X ms\n"
            "   * StdDev: X ms\n"
            "   * Errors: X %\n"
            "   * Throughput: X\n"
            "   * Sent: X KB/s\n"
            "   * Received: X KB/s\n"
            "   * Avg Bytes: X\n"
            "   * Analysis: <short real analysis based only on data>\n\n"
            "Bottlenecks and Issues:\n\n"
            "* Key issues based ONLY on numbers above\n\n"
            "Suggestions and Next Steps:\n\n"
            "1. Action item...\n\n"
            "Important:\n"
            "- Do NOT invent metrics.\n"
            "- Only use endpoints in provided summary.\n"
            "- If nothing looks problematic, state clearly 'No major bottlenecks observed'.\n"
        )

        os.makedirs(output_folder, exist_ok=True)
        ts = datetime.now(timezone.utc).strftime("%d-%m-%Y_%H-%M-%S")
        filename = f"analysis_{ts}.pdf"
        output_path = os.path.join(output_folder, filename)

        # AI call
        raw = None
        for attempt in range(3):
            try:
                task = generate_gemini_analysis_async.delay(prompt)
                raw = task.get(timeout=300).strip()
                break
            except Exception as e:
                logger.warning(f"Attempt {attempt+1} failed: {e}")
                time.sleep(3)

        if not raw:
            return {"error": "AI did not return analysis."}

        report_text = clean_ai_text(json.loads(raw).get("analysis", raw) if raw.startswith("{") else raw)
        build_professional_pdf(report_text, output_path, summary_filtered, metrics_source, title=filename)
        html_preview = build_html_report(report_text, metrics_source)

        return {"message": "PDF generated successfully.", "filename": filename, "html_report": html_preview}

    except Exception as e:
        logger.error(f"❌ Analysis failed: {e}")
        return {"error": str(e)}


