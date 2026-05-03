import sys
import os
import time
import traceback
import tempfile
import pdfplumber

try:
    import docx
    DOCX_AVAILABLE = True
except ImportError:
    DOCX_AVAILABLE = False

from flask import request, jsonify

def register_doc_summarizer_routes(app, call_openai_api_fn):
    """
    Register Financial Document Summarizer Agent API routes.
    """

    @app.route("/agent/doc-summarizer/upload", methods=["POST"])
    def agent_doc_summarizer_upload():
        """Upload a PDF or Word document and return a financial summary."""
        try:
            if 'file' not in request.files:
                return jsonify({"error": "No file part in the request"}), 400
                
            file = request.files['file']
            if file.filename == '':
                return jsonify({"error": "No selected file"}), 400
                
            filename = file.filename.lower()
            if not (filename.endswith('.pdf') or filename.endswith('.docx') or filename.endswith('.doc')):
                return jsonify({"error": "Only PDF and Word (.docx) files are supported"}), 400

            # Save uploaded file to temp file
            _, temp_path = tempfile.mkstemp(suffix=os.path.splitext(filename)[1])
            file.save(temp_path)
            
            text_content = ""
            try:
                if filename.endswith('.pdf'):
                    with pdfplumber.open(temp_path) as pdf:
                        for page in pdf.pages:
                            page_text = page.extract_text()
                            if page_text:
                                text_content += page_text + "\n"
                elif filename.endswith('.docx') or filename.endswith('.doc'):
                    if not DOCX_AVAILABLE:
                        return jsonify({"error": "python-docx library is not installed, cannot read word files"}), 500
                    doc = docx.Document(temp_path)
                    for para in doc.paragraphs:
                        text_content += para.text + "\n"
            except Exception as read_err:
                print(f"Error reading document: {read_err}")
                return jsonify({"error": f"Failed to extract text from document: {str(read_err)}"}), 500
            finally:
                try:
                    os.remove(temp_path)
                except:
                    pass

            if not text_content.strip():
                return jsonify({"error": "Extracted text is empty. The document might be scanned or image-based."}), 400

            # Truncate text if too long (gpt-5.4-mini has ~128k context, but let's be safe and avoid huge payloads)
            # Roughly 4 chars per token, let's limit to 400,000 chars (approx 100k tokens max)
            MAX_CHARS = 400000
            if len(text_content) > MAX_CHARS:
                text_content = text_content[:MAX_CHARS] + "\n\n...[TRUNCATED FOR LENGTH]..."

            # Summarize with gpt-5.4-mini
            prompt = f"""You are an elite financial analyst. The user has provided the text extracted from a document. 
The document is expected to be a financial document such as an investor presentation, analyst report, brokerage report, credit report, quarterly results, annual report, etc.
Please provide a comprehensive and structured summary of the document keeping its financial nature in mind.

Extract key metrics, forward-looking statements, risk factors, and overall sentiment.
Format your output cleanly in Markdown with appropriate headings, bullet points, and tables if applicable.

Here is the document text:
==========================
{text_content}
==========================
"""

            messages = [{"role": "user", "content": prompt}]
            
            print(f"DOC_SUMMARIZER_AGENT: Calling gpt-5.4-mini for {filename} ({len(text_content)} chars)", file=sys.stderr)
            
            summary = call_openai_api_fn(messages, model="gpt-5.4-mini", temperature=0.7)
            
            return jsonify({
                "status": "success",
                "summary": summary
            })

        except Exception as e:
            print(f"DOC_SUMMARIZER_AGENT ERROR: {e}", file=sys.stderr)
            traceback.print_exc(file=sys.stderr)
            return jsonify({"error": str(e)}), 500
