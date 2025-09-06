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


from collections import defaultdict, Counter
from statistics import mean, stdev, quantiles



def parse_jtl_detailed(jtl_path):
    """
    Parse JTL results deeply for advanced analysis:
    - Response times (avg, min, max, stddev, percentiles)
    - Latency vs Connect time
    - Error breakdowns by response code
    - Throughput based on wall clock time
    - Bytes in/out
    - Grouping by endpoint label and thread group
    """

    tree = ET.parse(jtl_path)
    root = tree.getroot()

    summary = defaultdict(lambda: {
        "samples": [],
        "latency": [],
        "connect": [],
        "response_codes": Counter(),
        "total_bytes": 0,
        "sent_bytes": 0,
        "success_count": 0,
        "threads": Counter()
    })
    all_timestamps = []

    def normalize_label(label: str) -> str:
        # Clean up request names ("HTTP Request-0" → "HTTP Request")
        return re.sub(r'-\d+$', '', label)

    def add_sample_data(node):
        label = normalize_label(node.get("lb") or "Unknown")
        elapsed = int(node.get("t", 0))
        latency = int(node.get("lt", 0))
        connect = int(node.get("ct", 0))
        success = node.get("s") == "true"
        received = int(node.get("by", 0))
        sent = int(node.get("sby", 0))
        rc = node.get("rc", "N/A")
        thread = node.get("tn", "Unknown")
        timestamp = int(node.get("ts", 0))

        summary[label]["samples"].append(elapsed)
        summary[label]["latency"].append(latency)
        summary[label]["connect"].append(connect)
        summary[label]["total_bytes"] += received
        summary[label]["sent_bytes"] += sent
        summary[label]["response_codes"][rc] += 1
        summary[label]["threads"][thread] += 1
        if success:
            summary[label]["success_count"] += 1
        if timestamp > 0:
            all_timestamps.append(timestamp)

    # Only parent <sample>/<httpSample> nodes
    for sample_node in root:
        if 'sample' in sample_node.tag.lower():
            add_sample_data(sample_node)

    if not all_timestamps:
        duration_sec = 1
    else:
        min_ts, max_ts = min(all_timestamps), max(all_timestamps)
        duration_sec = max((max_ts - min_ts) / 1000.0, 1)

    results = []

    for label, data in summary.items():
        samples = data["samples"]
        count = len(samples)
        if count == 0:
            continue

        # Percentiles
        percentiles = quantiles(samples, n=100) if count >= 100 else []
        p50 = round(percentiles[49], 2) if len(percentiles) >= 50 else mean(samples)
        p90 = round(percentiles[89], 2) if len(percentiles) >= 90 else max(samples)
        p95 = round(percentiles[94], 2) if len(percentiles) >= 95 else max(samples)
        p99 = round(percentiles[98], 2) if len(percentiles) >= 99 else max(samples)

        results.append({
            "label": label,
            "samples": count,
            "average_ms": round(mean(samples), 2),
            "min_ms": min(samples),
            "max_ms": max(samples),
            "stddev_ms": round(stdev(samples), 2) if len(samples) > 1 else 0,
            "p50_ms": p50,
            "p90_ms": p90,
            "p95_ms": p95,
            "p99_ms": p99,
            "error_pct": round(100 * (count - data["success_count"]) / count, 2),
            "throughput_rps": round(count / duration_sec, 2),
            "received_kbps": round(data["total_bytes"] / 1024 / duration_sec, 2),
            "sent_kbps": round(data["sent_bytes"] / 1024 / duration_sec, 2),
            "avg_bytes": round(data["total_bytes"] / count, 2),
            "response_codes": dict(data["response_codes"]),
            "avg_latency_ms": round(mean(data["latency"]), 2) if data["latency"] else 0,
            "avg_connect_ms": round(mean(data["connect"]), 2) if data["connect"] else 0,
            "threads": dict(data["threads"])
        })

    return results



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
    
    try:
        logger.info(f"📊 Starting PDF analysis for {file_path}")
        raw_summary = parse_jtl_detailed(file_path)
        df = pd.DataFrame(raw_summary)

        if df is None or df.empty:
            return {"error": "Parsed JTL has no results. Possibly the test never ran."}

        # Filter out static resources (noise)
        df = df[~df["label"].str.contains(r"\.(?:css|js|png|jpg|gif|ico|svg)$", case=False, na=False)]


        required = {"label", "average_ms", "min_ms", "max_ms", "stddev_ms", "error_pct"}
        if not required.issubset(df.columns):
            return {"error": f"Missing required summary columns: {required - set(df.columns)}"}

        user_friendly_columns = [
            "label", "samples", "average_ms", "min_ms", "max_ms", "stddev_ms",
            "error_pct", "throughput_rps", "received_kbps", "sent_kbps", "avg_bytes"
        ]

        filtered_df_for_user = df[user_friendly_columns].copy()


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
        SLA_THRESHOLD = 2000  # ms, adjust globally/config
        summary_filtered = summary[
            (summary["error_pct"] > 0) |
            (summary["p90_ms"] > SLA_THRESHOLD) |
            (summary["p95_ms"] > SLA_THRESHOLD)  # new
        ]


        metrics_source = summary_filtered if not summary_filtered.empty else summary

        # Build test metrics sentence for AI
        summary_text = "\n".join([
        f"- {row['label']}: Avg={row['average_ms']} ms "
        f"(p90={row['p90_ms']} ms, p95={row['p95_ms']} ms, p99={row['p99_ms']} ms), "
        f"Min={row['min_ms']} ms, Max={row['max_ms']} ms, "
        f"Errors={row['error_pct']}%, Codes={row['response_codes']}, "
        f"Throughput={row['throughput_rps']} rps, "
        f"Latency={row['avg_latency_ms']} ms, Connect={row['avg_connect_ms']} ms"
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
            "   * Avg: X ms\n"
            "   * P90: X ms\n"
            "   * P95: X ms\n"
            "   * P99: X ms\n"
            "   * Min: X ms, Max: X ms, StdDev: X ms\n"
            "   * Errors: X % with codes {200: N, 500: M}\n"
            "   * Throughput: X rps\n"
            "   * Latency: X ms, Connect: Y ms\n"
            "   * Bytes: Avg=X\n"
            "   * Analysis: <explain trends>\n\n"
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
        build_professional_pdf(report_text, output_path, filtered_df_for_user, metrics_source, title=filename)
        html_preview = build_html_report(report_text, filtered_df_for_user)

        return {"message": "PDF generated successfully.", "filename": filename, "html_report": html_preview}

    except Exception as e:
        logger.error(f"❌ Analysis failed: {e}")
        return {"error": str(e)}


